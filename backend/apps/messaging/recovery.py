"""
Reconciling messages whose sending worker died in the ambiguous window.

A message in SENDING whose ``lease_expires_at`` is in the past has no live
owner: the worker that claimed it is gone (OOM, container restart, deploy, a
hung provider call that blew the hard time limit). This module decides, for
each such message, which of four things actually happened -- and it never
guesses "it failed, resend it", because that is how one dead worker turns into
a thousand duplicate messages.

The four outcomes, from the recommended recovery ladder:

* **Never submitted.** No attempt row, or the attempt is still CLAIMED / has
  no ``submission_started_at``. The provider was never called. Safe: hand the
  message back to the queue (a *fresh* attempt row, new idempotency identity)
  and let it send normally.
* **Submitted and accepted.** The attempt carries a ``provider_message_id`` --
  the provider took it. Move the message to SENT, settle the credit, and wait
  for the delivery webhooks like any other sent message.
* **Submission started, outcome unclear.** The provider was called but we have
  no id and no webhook. For a provider that can be *asked* (the QR bridge,
  via its operation-id lookup) we ask: it either confirms the send, tells us
  it never saw the operation (safe to re-queue), or is unreachable. For a
  provider that cannot be asked and offers no idempotent submission (the Cloud
  API), the honest answer is UNKNOWN.
* **Unknown.** Mark the message RECOVERY_REQUIRED. It does not block the
  campaign, it does not get resent, the credit stays reserved, and it shows up
  in the campaign-health metrics and the operator's recovery list for a human
  or a later reconciliation to settle.
"""

import logging

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.credits.services import consume_credit, release_credits
from apps.subscriptions.services import release_message_allowance
from apps.whatsapp.connections.manager import ConnectionManager

from .leasing import advance_attempt
from .models import Message, MessageSendAttempt
from .signals import message_status_changed, outbound_message_sent

logger = logging.getLogger("sendstack")

#: How many stranded messages one sweep reconciles. A ceiling on the work one
#: run can do (and on the bridge queries it can make), not a throughput limit:
#: what is not handled now is handled on the next tick a minute later.
SWEEP_BATCH_SIZE = 200


def _latest_attempt(message_id: int) -> MessageSendAttempt | None:
    return (
        MessageSendAttempt.objects.filter(message_id=message_id)
        .order_by("-attempt_number")
        .first()
    )


def _mark_sent_from_recovery(message: Message, attempt: MessageSendAttempt | None,
                             provider_message_id: str | None, now) -> str:
    """Move a message the provider confirmed to SENT, settle its credit, announce it."""
    with transaction.atomic():
        moved = Message.objects.filter(pk=message.pk, status=Message.Status.SENDING).update(
            status=Message.Status.SENT,
            provider_message_id=provider_message_id or message.provider_message_id,
            sent_at=now,
            lease_owner="",
            lease_expires_at=None,
            updated_at=now,
        )
        if not moved:
            return "skipped"  # a returning worker beat us to it
        if attempt is not None and attempt.status not in MessageSendAttempt.TERMINAL_STATUSES:
            advance_attempt(
                attempt,
                status=MessageSendAttempt.Status.CONFIRMED,
                submission_completed_at=now,
                provider_message_id=provider_message_id or attempt.provider_message_id,
            )

    message.refresh_from_db()
    if message.billed_to_id:
        # Same key the send task uses, so this is a no-op if the dead worker
        # somehow got the consume in before it died.
        consume_credit(
            user_id=message.billed_to_id, reference=f"message:{message.pk}",
            operation_id=f"msg-consume:{message.pk}",
        )
    # The original send never got to fire these; the CRM's "last contacted"
    # and the webhook subscribers need them. Follow-up scheduling is
    # deliberately NOT re-run here -- a missed nudge on a recovered message is
    # better than a doubled one.
    if message.direction == Message.Direction.OUTBOUND and message.contact_id:
        outbound_message_sent.send(sender=Message, message=message)
    message_status_changed.send(sender=Message, message=message, status=Message.Status.SENT)
    logger.warning(
        "MESSAGE_RECONCILIATION_COMPLETED",
        extra={"event": "send.recovery.confirmed_sent", "message_id": message.pk,
               "attempt_id": getattr(attempt, "pk", None), "campaign_id": message.campaign_id,
               "tenant_id": message.tenant_id},
    )
    _refresh_campaign(message.campaign_id)
    return "recovered"


def _requeue(message: Message, attempt: MessageSendAttempt | None, now) -> str:
    """Hand a never-submitted message back to the queue for a clean fresh attempt."""
    with transaction.atomic():
        moved = Message.objects.filter(pk=message.pk, status=Message.Status.SENDING).update(
            status=Message.Status.QUEUED,
            lease_owner="",
            lease_expires_at=None,
            lease_heartbeat_at=None,
            updated_at=now,
        )
        if not moved:
            return "skipped"
        if attempt is not None and attempt.status not in MessageSendAttempt.TERMINAL_STATUSES:
            advance_attempt(
                attempt,
                status=MessageSendAttempt.Status.EXPIRED,
                last_error="Worker lost before the provider was called; message re-queued.",
            )

    from .tasks import send_message_task

    send_message_task.apply_async(args=[message.pk])
    logger.warning(
        "MESSAGE_RETRY_SCHEDULED",
        extra={"event": "send.recovery.requeued", "message_id": message.pk,
               "attempt_id": getattr(attempt, "pk", None), "campaign_id": message.campaign_id,
               "tenant_id": message.tenant_id},
    )
    _refresh_campaign(message.campaign_id)
    return "retried"


def _mark_unknown(message: Message, attempt: MessageSendAttempt | None, now, reason: str) -> str:
    with transaction.atomic():
        moved = Message.objects.filter(pk=message.pk, status=Message.Status.SENDING).update(
            status=Message.Status.RECOVERY_REQUIRED,
            failure_reason=Message.FailureReason.DELIVERY_UNCONFIRMED,
            error_message=reason[:500],
            lease_owner="",
            lease_expires_at=None,
            updated_at=now,
        )
        if not moved:
            return "skipped"
        if attempt is not None and attempt.status not in MessageSendAttempt.TERMINAL_STATUSES:
            advance_attempt(
                attempt, status=MessageSendAttempt.Status.UNKNOWN,
                submission_completed_at=now, last_error=reason[:480],
            )

    message.refresh_from_db()
    message_status_changed.send(
        sender=Message, message=message, status=Message.Status.RECOVERY_REQUIRED,
    )
    logger.error(
        "MESSAGE_MARKED_UNKNOWN",
        extra={"event": "send.recovery.unknown", "message_id": message.pk,
               "attempt_id": getattr(attempt, "pk", None), "campaign_id": message.campaign_id,
               "tenant_id": message.tenant_id, "reason": reason[:200]},
    )
    _refresh_campaign(message.campaign_id)
    return "unknown"


def _refresh_campaign(campaign_id) -> None:
    if not campaign_id:
        return
    from .services import refresh_campaign_counters

    refresh_campaign_counters(campaign_id)


def classify_and_recover(message: Message, *, now=None, force: bool = False) -> str:
    """
    Reconcile one stranded message. Returns one of
    ``recovered`` / ``retried`` / ``unknown`` / ``skipped``.

    ``force`` runs the classification even if the lease has not expired -- used
    by the operator reconcile endpoint, never by the sweep.
    """
    now = now or timezone.now()

    if message.status != Message.Status.SENDING:
        return "skipped"
    if not force and (message.lease_expires_at is None or message.lease_expires_at >= now):
        return "skipped"

    attempt = _latest_attempt(message.pk)
    logger.info(
        "MESSAGE_RECONCILIATION_STARTED",
        extra={"event": "send.recovery.started", "message_id": message.pk,
               "attempt_id": getattr(attempt, "pk", None),
               "attempt_status": getattr(attempt, "status", None),
               "campaign_id": message.campaign_id, "tenant_id": message.tenant_id},
    )

    # --- Legacy row: no attempt to reason from --------------------------
    # New code always writes the attempt row in the same transaction as the
    # SENDING flip, so a SENDING message with no attempt can only be one
    # stranded before this machinery existed. We cannot know whether it was
    # delivered, so it is UNKNOWN -- never re-queued (that risks a duplicate).
    if attempt is None:
        return _mark_unknown(
            message, None, now,
            "This message was left in SENDING before crash-safe send tracking existed; "
            "its delivery cannot be verified.",
        )

    # --- Provably never submitted -------------------------------------------
    if (
        attempt.status == MessageSendAttempt.Status.CLAIMED
        or attempt.submission_started_at is None
    ):
        return _requeue(message, attempt, now)

    # --- Submitted and accepted (id in hand, or a webhook already tied one) --
    provider_message_id = attempt.provider_message_id or message.provider_message_id
    if provider_message_id:
        return _mark_sent_from_recovery(message, attempt, provider_message_id, now)

    # --- Submission started, no id: ask the provider if we can --------------
    account = message.whatsapp_account
    try:
        status = ConnectionManager.get_send_operation_status(account, attempt.idempotency_key)
    except Exception as exc:  # noqa: BLE001 -- an unreachable provider is "unknown", not a crash
        logger.warning("Recovery: could not query provider for message %s: %s", message.pk, exc)
        return _mark_unknown(message, attempt, now, f"Provider status query failed: {exc}")

    if status is None:
        # This provider offers no status lookup and no idempotent submission
        # (the Cloud API). We cannot prove what happened.
        return _mark_unknown(
            message, attempt, now,
            "The provider was called but never confirmed, and it offers no way to check.",
        )

    if status.get("unreachable"):
        return _mark_unknown(
            message, attempt, now, "The provider could not be reached to check this send.",
        )

    if status.get("known") and status.get("sent"):
        return _mark_sent_from_recovery(message, attempt, status.get("provider_message_id"), now)

    # The provider was reachable and either never saw this operation id, or
    # saw it and it did not go out. Either way a fresh attempt is safe -- the
    # bridge's dedup store only remembers sends that succeeded or are in
    # doubt, so re-sending will not double-deliver.
    return _requeue(message, attempt, now)


def sweep_stuck_sends(*, now=None, limit: int = SWEEP_BATCH_SIZE) -> dict:
    """
    Periodic. Reconcile every SENDING message whose lease has expired.

    Runs off the CELERY_BEAT_SCHEDULE. The id list is taken in one short query
    (indexed on ``status, lease_expires_at``); each message is then reconciled
    in its own short transaction, because the QR-bridge branch makes a network
    call and a transaction must never be held across one.
    """
    now = now or timezone.now()
    ids = list(
        Message.objects.filter(
            status=Message.Status.SENDING, lease_expires_at__lt=now,
        )
        .order_by("lease_expires_at")
        .values_list("id", flat=True)[:limit]
    )
    counts = {"recovered": 0, "retried": 0, "unknown": 0, "skipped": 0, "scanned": len(ids)}
    for message_id in ids:
        message = (
            Message.objects.select_related("whatsapp_account").filter(pk=message_id).first()
        )
        if message is None:
            continue
        try:
            counts[classify_and_recover(message, now=now)] += 1
        except Exception:  # noqa: BLE001 -- one bad row must not stall the sweep
            logger.exception("Recovery sweep failed on message %s.", message_id)

    if counts["unknown"] or counts["retried"] or counts["recovered"]:
        logger.warning(
            "Recovery sweep: %s recovered, %s re-queued, %s unknown (of %s stranded).",
            counts["recovered"], counts["retried"], counts["unknown"], counts["scanned"],
        )
    return counts


# ---------------------------------------------------------------------------
# Operator-facing: campaign health and manual resolution
# ---------------------------------------------------------------------------

class RecoveryActionError(Exception):
    """A requested recovery action is not allowed for this message's state."""


def campaign_health(campaign) -> dict:
    """Message counts by status for one campaign, with the recovery figures broken out."""
    tally = {
        row["status"]: row["total"]
        for row in campaign.messages.values("status").annotate(total=Count("id"))
    }
    S = Message.Status
    total = sum(tally.values())
    sending_stuck = campaign.messages.filter(
        status=S.SENDING, lease_expires_at__lt=timezone.now(),
    ).count()
    return {
        "campaign_id": campaign.pk,
        "status": campaign.status,
        "total": total,
        "queued": tally.get(S.QUEUED, 0),
        "sending": tally.get(S.SENDING, 0),
        "sending_lease_expired": sending_stuck,
        "sent": tally.get(S.SENT, 0),
        "delivered": tally.get(S.DELIVERED, 0),
        "read": tally.get(S.READ, 0),
        "failed": tally.get(S.FAILED, 0),
        "skipped": tally.get(S.SKIPPED, 0),
        "cancelled": tally.get(S.CANCELLED, 0),
        "recovery_required": tally.get(S.RECOVERY_REQUIRED, 0),
    }


def message_recovery_detail(message: Message) -> dict:
    """The attempt history and current recovery picture for one message."""
    attempts = list(
        MessageSendAttempt.objects.filter(message_id=message.pk).order_by("attempt_number")
    )
    latest = attempts[-1] if attempts else None
    return {
        "message_id": message.pk,
        "status": message.status,
        "provider_message_id": message.provider_message_id,
        "lease_owner": message.lease_owner,
        "lease_expires_at": message.lease_expires_at,
        "error_message": message.error_message,
        "failure_reason": message.failure_reason,
        "retry_safe": _retry_is_safe(message, latest),
        "attempts": [
            {
                "id": a.pk,
                "attempt_number": a.attempt_number,
                "status": a.status,
                "worker_id": a.worker_id,
                "task_id": a.task_id,
                "provider": a.provider,
                "account_id": a.account_id,
                "idempotency_key": a.idempotency_key,
                "started_at": a.started_at,
                "submission_started_at": a.submission_started_at,
                "submission_completed_at": a.submission_completed_at,
                "provider_message_id": a.provider_message_id,
                "provider_status": a.provider_status,
                "last_error": a.last_error,
                "last_heartbeat_at": a.last_heartbeat_at,
            }
            for a in attempts
        ],
    }


def _retry_is_safe(message: Message, attempt: MessageSendAttempt | None) -> bool:
    """
    Whether "retry" can be offered without risking a duplicate.

    Only when we can prove the provider was never called, or the provider
    guarantees idempotent submission for this operation id (the QR bridge, via
    its dedup store). Everything else is a genuine unknown and must not be
    blindly resent.
    """
    if message.status not in (Message.Status.RECOVERY_REQUIRED, Message.Status.FAILED):
        return False
    if attempt is None:
        return False
    if attempt.submission_started_at is None or attempt.status == MessageSendAttempt.Status.CLAIMED:
        return True
    return _provider_is_idempotent(message)


def _provider_is_idempotent(message: Message) -> bool:
    """
    True when this message's connection can be re-sent to safely by operation id.

    The QR bridge dedups on ``send_operation_id`` (a second POST returns the
    first result, never re-sends), so a retry that reuses the attempt's
    idempotency key cannot double-deliver. The Cloud API has no such guarantee.
    """
    try:
        from apps.whatsapp.providers import get_provider
        from apps.whatsapp.providers.qr_bridge import WhatsAppQRBridgeProvider

        return isinstance(get_provider(message.whatsapp_account), WhatsAppQRBridgeProvider)
    except Exception:  # noqa: BLE001
        return False


def resolve_message(message: Message, action: str, *, actor=None) -> str:
    """
    Apply an operator's decision to a RECOVERY_REQUIRED (or FAILED) message.

    ``action`` is one of ``confirm_sent`` / ``mark_failed`` / ``retry_safe``.
    Returns the message's new status. Raises RecoveryActionError when the
    action is not permitted for the current state.
    """
    now = timezone.now()
    message.refresh_from_db()
    if message.status not in (Message.Status.RECOVERY_REQUIRED, Message.Status.FAILED):
        raise RecoveryActionError(
            f"This message is {message.get_status_display().lower()}; there is nothing to resolve.",
        )
    latest = MessageSendAttempt.objects.filter(message_id=message.pk).order_by("-attempt_number").first()

    if action == "confirm_sent":
        with transaction.atomic():
            Message.objects.filter(pk=message.pk).update(
                status=Message.Status.SENT, sent_at=message.sent_at or now, updated_at=now,
                lease_owner="", lease_expires_at=None,
            )
            if latest is not None and latest.status not in MessageSendAttempt.TERMINAL_STATUSES:
                advance_attempt(latest, status=MessageSendAttempt.Status.CONFIRMED, submission_completed_at=now)
        message.refresh_from_db()
        if message.billed_to_id:
            consume_credit(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-consume:{message.pk}",
            )
        message_status_changed.send(sender=Message, message=message, status=Message.Status.SENT)
        _log_resolution(message, action, actor)
        _refresh_campaign(message.campaign_id)
        return Message.Status.SENT

    if action == "mark_failed":
        with transaction.atomic():
            Message.objects.filter(pk=message.pk).update(
                status=Message.Status.FAILED, failed_at=message.failed_at or now, updated_at=now,
                lease_owner="", lease_expires_at=None,
            )
            if latest is not None and latest.status not in MessageSendAttempt.TERMINAL_STATUSES:
                advance_attempt(latest, status=MessageSendAttempt.Status.FAILED, submission_completed_at=now)
        message.refresh_from_db()
        release_message_allowance(message)
        if message.billed_to_id:
            release_credits(
                user_id=message.billed_to_id, reference=f"message:{message.pk}",
                operation_id=f"msg-release:{message.pk}",
            )
        message_status_changed.send(sender=Message, message=message, status=Message.Status.FAILED)
        _log_resolution(message, action, actor)
        _refresh_campaign(message.campaign_id)
        return Message.Status.FAILED

    if action == "retry_safe":
        # Never trust a stale flag here -- re-run the exact classification the
        # sweep uses (force=True so it runs regardless of lease age). It only
        # re-queues when it can *prove* nothing was delivered: the provider was
        # never called, or the bridge confirms it never sent this operation.
        # Anything short of proof comes back "unknown" and the operator is
        # told to confirm/fail it by hand instead.
        if message.status == Message.Status.FAILED:
            # A FAILED message has already released its credit; put it back in
            # QUEUED and let the normal path re-reserve nothing (campaign retry
            # semantics already cover this). Reuse the campaign retry instead.
            raise RecoveryActionError(
                "Use the campaign's 'retry failed' action for a failed message.",
            )
        Message.objects.filter(pk=message.pk).update(status=Message.Status.SENDING, updated_at=now)
        message.status = Message.Status.SENDING
        outcome = classify_and_recover(message, now=now, force=True)
        if outcome == "retried":
            _log_resolution(message, action, actor)
            return Message.Status.QUEUED
        if outcome == "recovered":
            _log_resolution(message, action, actor)
            return Message.Status.SENT
        # Put it back to RECOVERY_REQUIRED -- classify_and_recover moved it to
        # RECOVERY_REQUIRED already for the "unknown" outcome, so this is a
        # no-op there; the raise is what the caller sees.
        raise RecoveryActionError(
            "This send cannot be safely retried: the provider cannot confirm whether the "
            "message went out, and it offers no idempotent resend. Use 'confirm sent' or "
            "'mark failed' instead.",
        )

    raise RecoveryActionError(f"Unknown recovery action '{action}'.")


def _log_resolution(message: Message, action: str, actor) -> None:
    logger.warning(
        "MESSAGE_RECOVERY_RESOLVED",
        extra={"event": "send.recovery.resolved", "message_id": message.pk, "action": action,
               "actor_id": getattr(actor, "pk", None), "campaign_id": message.campaign_id,
               "tenant_id": message.tenant_id},
    )
