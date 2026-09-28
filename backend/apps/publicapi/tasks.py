"""
Delivering webhooks, and sweeping up after the parts that can fail.

Delivery is at-least-once. A receiver that is slow, restarting, or briefly
5xx-ing gets retried with jittered exponential backoff; one that has been gone
for a long time gets the endpoint disabled so it stops costing worker time.
The one thing never done is give up quietly -- every terminal outcome is a row
in the delivery log with the HTTP status and the error on it.
"""

import logging

import requests
from django.conf import settings
from django.db.models import F
from django.utils import timezone

from config.celery import app
from core.backoff import retry_delay

from . import events as event_defs
from .models import ApiRequestLog, IdempotencyRecord, WebhookDelivery, WebhookEndpoint
from .webhook_urls import ensure_deliverable

logger = logging.getLogger("sendstack")

#: How many times one event is attempted before the delivery is marked FAILED.
#: Five attempts with jittered doubling from 10s covers roughly ten minutes of
#: outage, which is long enough for a deploy or a restart and short enough that
#: a dead endpoint does not hold a queue slot for an hour.
MAX_ATTEMPTS = 5

#: Consecutive *failed deliveries* -- not attempts -- before the endpoint is
#: switched off. Counted per endpoint rather than per delivery, so an endpoint
#: that has been gone all week is disabled rather than being retried five times
#: for every event that occurs.
FAILURES_BEFORE_DISABLE = 20

#: Deliberately short. A receiver is supposed to acknowledge and process
#: asynchronously; one that takes longer than this to answer is one that will
#: eventually take a worker down with it.
TIMEOUT_SECONDS = 10


def _timeout() -> int:
    return int(getattr(settings, "PUBLIC_API_WEBHOOK_TIMEOUT_SECONDS", TIMEOUT_SECONDS))


@app.task(bind=True, max_retries=MAX_ATTEMPTS)
def deliver_webhook(self, delivery_id):
    delivery = (
        WebhookDelivery.objects.select_related("endpoint")
        .filter(pk=delivery_id)
        .first()
    )
    if delivery is None:
        # Either it was pruned, or the transaction that created it has not
        # committed yet -- `events.emit` may be called from inside one. The
        # sweeper below picks up the second case, so returning quietly is
        # correct rather than merely tolerant.
        logger.info("Webhook delivery %s is not visible yet; leaving it to the sweeper.", delivery_id)
        return

    if delivery.status == WebhookDelivery.Status.SUCCEEDED:
        return

    endpoint = delivery.endpoint
    if not endpoint.is_active:
        _fail(delivery, error="The endpoint is disabled.", terminal=True)
        return

    # Re-checked immediately before the request, not only when the endpoint was
    # saved: a hostname that resolved to a public address last week can resolve
    # to 127.0.0.1 today, and this is the check that catches a DNS rebind.
    try:
        ensure_deliverable(endpoint.url)
    except Exception as exc:  # noqa: BLE001 - PublicAPIError or anything the guard raises
        _fail(delivery, error=f"Refusing to deliver: {exc}"[:300], terminal=True)
        _note_endpoint_failure(endpoint, str(exc)[:300])
        return

    secret = endpoint.get_secret()
    if not secret:
        # The Fernet key changed, or the row predates one. Not retryable: every
        # attempt will fail the same way, and a signature we cannot compute is
        # worse than no delivery -- the receiver would reject it anyway.
        _fail(delivery, error="The signing secret could not be decrypted. Rotate it.", terminal=True)
        _note_endpoint_failure(endpoint, "Signing secret could not be decrypted.")
        return

    import json

    body = json.dumps(delivery.payload, separators=(",", ":")).encode("utf-8")
    timestamp = int(timezone.now().timestamp())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "SendStack-Webhooks/1.0",
        event_defs.SIGNATURE_HEADER: event_defs.signature(body, secret, timestamp),
        event_defs.TIMESTAMP_HEADER: str(timestamp),
        event_defs.EVENT_HEADER: delivery.event_type,
        event_defs.DELIVERY_HEADER: delivery.event_id,
    }

    WebhookDelivery.objects.filter(pk=delivery.pk).update(attempt_count=F("attempt_count") + 1)
    started = timezone.now()

    try:
        response = requests.post(
            endpoint.url,
            data=body,
            headers=headers,
            timeout=_timeout(),
            # A receiver that redirects is a receiver whose real address we
            # never validated. Following one would step straight around the
            # SSRF guard above.
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        elapsed = int((timezone.now() - started).total_seconds() * 1000)
        _retry_or_fail(self, delivery, endpoint, error=str(exc)[:300], response_ms=elapsed)
        return

    elapsed = int((timezone.now() - started).total_seconds() * 1000)
    # Any 2xx is success. A receiver answering 204 with no body is the ideal
    # implementation, and demanding 200 would punish it.
    if 200 <= response.status_code < 300:
        WebhookDelivery.objects.filter(pk=delivery.pk).update(
            status=WebhookDelivery.Status.SUCCEEDED,
            http_status=response.status_code,
            response_ms=elapsed,
            response_snippet=(response.text or "")[:300],
            delivered_at=timezone.now(),
            next_attempt_at=None,
            error="",
        )
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            last_success_at=timezone.now(), consecutive_failures=0, last_error="",
        )
        return

    _retry_or_fail(
        self, delivery, endpoint,
        error=f"HTTP {response.status_code}",
        http_status=response.status_code,
        response_ms=elapsed,
        snippet=(response.text or "")[:300],
    )


def _retry_or_fail(task, delivery, endpoint, *, error, http_status=None, response_ms=None, snippet=""):
    """
    Decide whether this failure gets another attempt.

    4xx other than 408/429 is terminal. A receiver answering 400 or 404 is not
    going to answer differently in thirty seconds; retrying it just delivers
    the same rejection five times and delays the delivery log telling its owner
    that the URL is wrong.
    """
    terminal = False
    if http_status is not None and 400 <= http_status < 500 and http_status not in (408, 429):
        terminal = True

    attempts = delivery.attempt_count + 1
    if terminal or attempts >= MAX_ATTEMPTS:
        _fail(
            delivery, error=error, http_status=http_status,
            response_ms=response_ms, snippet=snippet, terminal=True,
        )
        _note_endpoint_failure(endpoint, error)
        return

    countdown = retry_delay(attempts - 1, base=10.0)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.Status.PENDING,
        http_status=http_status,
        response_ms=response_ms,
        response_snippet=snippet,
        error=error[:300],
        next_attempt_at=timezone.now() + timezone.timedelta(seconds=countdown),
    )
    raise task.retry(exc=RuntimeError(error), countdown=countdown)


def _fail(delivery, *, error, http_status=None, response_ms=None, snippet="", terminal=False):
    WebhookDelivery.objects.filter(pk=delivery.pk).update(
        status=WebhookDelivery.Status.FAILED if terminal else WebhookDelivery.Status.PENDING,
        http_status=http_status,
        response_ms=response_ms,
        response_snippet=snippet,
        error=error[:300],
        next_attempt_at=None if terminal else timezone.now(),
    )


def _note_endpoint_failure(endpoint, error: str) -> None:
    """Count the failure, and switch the endpoint off if it has been enough."""
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
        last_failure_at=timezone.now(),
        last_error=(error or "")[:300],
        consecutive_failures=F("consecutive_failures") + 1,
    )
    endpoint.refresh_from_db(fields=["consecutive_failures"])
    if endpoint.consecutive_failures >= FAILURES_BEFORE_DISABLE and endpoint.auto_disabled_at is None:
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            is_active=False, auto_disabled_at=timezone.now(),
        )
        logger.warning(
            "Disabled webhook endpoint %s after %s consecutive failures.",
            endpoint.pk, endpoint.consecutive_failures,
        )


@app.task
def sweep_pending_webhooks():
    """
    Re-queue deliveries that were written but never picked up.

    Two ways that happens, and neither is exotic: `events.emit` is called from
    a signal receiver that may be inside an open transaction, so the task can
    reach a worker before the row is visible; and a worker can die between
    accepting a task and finishing it. Both leave a PENDING row with a
    `next_attempt_at` in the past and nothing scheduled to look at it.

    This is why the delivery guarantee is at-least-once and not at-most-once.
    """
    cutoff = timezone.now()
    stale = WebhookDelivery.objects.filter(
        status=WebhookDelivery.Status.PENDING,
        next_attempt_at__lte=cutoff,
        attempt_count__lt=MAX_ATTEMPTS,
    ).values_list("pk", flat=True)[:500]

    count = 0
    for delivery_id in list(stale):
        deliver_webhook.delay(delivery_id)
        count += 1
    if count:
        logger.info("Re-queued %s pending webhook deliveries.", count)
    return count


@app.task
def prune_public_api_data():
    """
    Housekeeping for the three tables that grow with traffic rather than with
    customers. Nothing here touches a key, an endpoint, or anything a customer
    configured -- only the logs and the expired idempotency claims.
    """
    now = timezone.now()

    expired_idempotency, _ = IdempotencyRecord.objects.filter(expires_at__lt=now).delete()

    request_log_days = int(getattr(settings, "PUBLIC_API_REQUEST_LOG_RETENTION_DAYS", 30) or 0)
    deleted_logs = 0
    if request_log_days > 0:
        deleted_logs, _ = ApiRequestLog.objects.filter(
            created_at__lt=now - timezone.timedelta(days=request_log_days)
        ).delete()

    delivery_days = int(getattr(settings, "PUBLIC_API_WEBHOOK_LOG_RETENTION_DAYS", 30) or 0)
    deleted_deliveries = 0
    if delivery_days > 0:
        # Only terminal rows. A PENDING delivery older than the horizon is
        # still owed to somebody, and deleting it would silently drop an event.
        deleted_deliveries, _ = WebhookDelivery.objects.filter(
            created_at__lt=now - timezone.timedelta(days=delivery_days),
            status__in=[WebhookDelivery.Status.SUCCEEDED, WebhookDelivery.Status.FAILED],
        ).delete()

    logger.info(
        "Public API prune: %s idempotency, %s request logs, %s deliveries.",
        expired_idempotency, deleted_logs, deleted_deliveries,
    )
    return {
        "idempotency": expired_idempotency,
        "request_logs": deleted_logs,
        "deliveries": deleted_deliveries,
    }
