"""
Deleting the rows that are safe to delete, and nothing else.

docs/DATA_RETENTION.md has described this deployment's retention policy since
Phase 3. Nothing implemented it: every table named there as transient grew for
ever, and ``BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS`` -- a setting that says
ninety days in so many words -- was read by nothing at all.

The volumes are not marginal. A provider sends a webhook per *status
transition*, so ``whatsapp_whatsappwebhookevent`` grows at roughly three rows
per outbound message (sent, delivered, read), each carrying the provider's
whole JSON body. A deployment sending a million messages a month accumulates
three million rows and several gigabytes a month in a table whose only job is
to answer "have I already seen this event id?" for events that arrived minutes
ago.

    WHAT THIS MODULE WILL NEVER TOUCH

Everything a customer or a regulator could need later, whatever its size:

* messages, contacts, campaigns, conversations -- the customer's own business
  data, theirs until they delete it;
* consent events -- the evidence an opt-out was honoured, which deliberately
  outlives the contact it was about;
* audit events and impersonation sessions -- the record of who did what;
* the credit ledger, payments and checkouts -- financial records;
* legal acceptances.

Those are listed in ``docs/DATA_RETENTION.md`` as never pruned by the
application, and this module keeps that promise by simply having no rule for
them. A rule is not a switch that defaults off; it is code that does not exist.

Every rule is expressed as a horizon in days and **0 disables it**, so an
operator with a different legal position turns any of them off without a code
change. Deletion is batched: a single ``DELETE`` covering three million rows
takes a lock long enough to stall the send pipeline behind it, which would
make the cleanup worse than the growth.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("sendstack")

#: Rows removed per statement. Small enough that each transaction is short and
#: the table stays available to the workers sending through it; large enough
#: that a month's backlog clears in minutes rather than hours.
DELETE_BATCH_SIZE = 5000

#: A ceiling on one pass, per rule. A first run against a table that has been
#: growing untouched for a year would otherwise run for hours and hold a
#: connection the whole time; the next scheduled pass picks up where this one
#: stopped, so the backlog still drains, just over several days.
MAX_ROWS_PER_PASS = 200_000


def _horizon(setting_name, default_days):
    """
    The cutoff for one rule, or None if the operator switched it off.

    0 means "keep for ever" rather than "delete everything", which is the same
    convention the plan limits use throughout this codebase -- and is the
    reading that fails safe if somebody sets it by mistake.
    """
    days = int(getattr(settings, setting_name, default_days) or 0)
    if days <= 0:
        return None
    return timezone.now() - timezone.timedelta(days=days)


def _delete_in_batches(queryset, label):
    """
    Delete `queryset` a batch at a time, returning how many rows went.

    Sliced by primary key rather than by ``.delete()`` on the whole queryset:
    Django's bulk delete would issue one statement for everything matched and
    hold the resulting locks until it committed.
    """
    model = queryset.model
    removed = 0
    while removed < MAX_ROWS_PER_PASS:
        batch = list(queryset.values_list("pk", flat=True)[:DELETE_BATCH_SIZE])
        if not batch:
            break
        count, _ = model.objects.filter(pk__in=batch).delete()
        # `count` is every row the cascade touched; the batch length is the
        # number of rows of *this* table, which is what the horizon is about.
        removed += len(batch)
        if len(batch) < DELETE_BATCH_SIZE:
            break

    if removed:
        logger.info(
            "retention: removed %s rows",
            removed,
            extra={"event": "retention.pruned", "rule": label, "rows": removed},
        )
    return removed


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def prune_whatsapp_webhook_events():
    """
    Raw provider events, once they can no longer be redelivered.

    The table exists to make webhook processing idempotent: a provider that
    retries an event finds the row and does nothing. Providers retry over
    minutes and hours, not months, so a horizon measured in weeks keeps the
    guarantee the table was built for and drops the rest.

    The *effects* of these events -- a message's status, an inbound message in
    the inbox -- are rows in other tables and are not touched.
    """
    horizon = _horizon("RETENTION_WEBHOOK_EVENT_DAYS", 30)
    if horizon is None:
        return 0

    from apps.whatsapp.models import WhatsAppWebhookEvent

    return _delete_in_batches(
        WhatsAppWebhookEvent.objects.filter(received_at__lt=horizon).order_by("pk"),
        "whatsapp_webhook_events",
    )


def purge_billing_webhook_payloads():
    """
    Strip the stored body from old payment webhooks, keeping the event row.

    Deliberately not a delete. The row is the record that this provider event
    was received and what was done about it, and that belongs with the
    financial records this module never removes. What ages out is the
    third-party *payload* -- a customer's payment details as the gateway
    described them -- which nothing needs once the window for disputing an
    activation has passed.

    This is what ``BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS`` has always meant.
    """
    days = int(getattr(settings, "BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS", 90) or 0)
    if days <= 0:
        return 0
    horizon = timezone.now() - timezone.timedelta(days=days)

    from apps.billing.models import WebhookEvent

    return WebhookEvent.objects.filter(
        received_at__lt=horizon, payload_purged_at__isnull=True
    ).update(payload={}, payload_purged_at=timezone.now())


def prune_finished_scheduled_jobs():
    """
    Scheduled jobs that have already run.

    A COMPLETED or CANCELLED job is a receipt, and the thing it acted on --
    the campaign -- keeps its own history. FAILED jobs are kept far longer,
    because a failed scheduled job is the thing an operator is looking for
    when a customer says their Friday campaign never went out.
    """
    horizon = _horizon("RETENTION_SCHEDULED_JOB_DAYS", 30)
    if horizon is None:
        return 0

    from apps.scheduling.models import ScheduledJob

    return _delete_in_batches(
        ScheduledJob.objects.filter(
            status__in=[ScheduledJob.Status.COMPLETED, ScheduledJob.Status.CANCELLED],
            updated_at__lt=horizon,
        ).order_by("pk"),
        "scheduled_jobs",
    )


def prune_expired_button_prompts():
    """
    Numbered-list questions nobody can answer any more.

    A prompt is answerable for BUTTON_PROMPT_TIMEOUT_MINUTES and then it is
    inert -- see apps.messaging.models.PendingButtonPrompt. The message that
    asked the question stays in the conversation either way; this is only the
    lookup table that mapped "2" back to a button id.
    """
    horizon = _horizon("RETENTION_BUTTON_PROMPT_DAYS", 7)
    if horizon is None:
        return 0

    from apps.messaging.models import PendingButtonPrompt

    return _delete_in_batches(
        PendingButtonPrompt.objects.filter(expires_at__lt=horizon).order_by("pk"),
        "button_prompts",
    )


def prune_expired_refresh_tokens():
    """
    Blacklisted refresh tokens whose own lifetime has run out.

    Rotation blacklists a token on every refresh, so this table grows once per
    refresh per session for ever. A token past its expiry is refused by its
    signature alone, so the blacklist entry protects nothing after that date.
    """
    horizon = timezone.now()

    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    return _delete_in_batches(
        OutstandingToken.objects.filter(expires_at__lt=horizon).order_by("pk"),
        "expired_refresh_tokens",
    )


#: Every rule, in the order a pass runs them. Named so a report can say which
#: one did what, and so a single rule can be run on its own from the command.
RULES = (
    ("whatsapp_webhook_events", prune_whatsapp_webhook_events),
    ("billing_webhook_payloads", purge_billing_webhook_payloads),
    ("scheduled_jobs", prune_finished_scheduled_jobs),
    ("button_prompts", prune_expired_button_prompts),
    ("expired_refresh_tokens", prune_expired_refresh_tokens),
)


def run_all(only=None) -> dict:
    """
    Run every rule (or just `only`), returning {rule: rows affected}.

    One rule failing does not stop the others: these are independent
    housekeeping jobs, and a broken one should not mean the webhook table stops
    being cleaned for a month.
    """
    results = {}
    for name, rule in RULES:
        if only and name != only:
            continue
        try:
            results[name] = rule()
        except Exception:  # noqa: BLE001 - one rule must not take the pass down
            logger.exception("retention rule %s failed", name, extra={"event": "retention.failed"})
            results[name] = None
    return results
