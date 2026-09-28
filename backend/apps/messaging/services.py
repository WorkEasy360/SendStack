import logging
from dataclasses import dataclass
from datetime import timedelta

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.whatsapp.connections.manager import ConnectionManager
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult
from core.buttons import as_numbered_options
from core.failpoints import crash_at
from core.media import resolve_media_items, spec_from_object

from .leasing import advance_attempt, claim_for_send, renew_lease
from .models import Message, MessageSendAttempt
from .signals import message_status_changed

logger = logging.getLogger("sendstack")


def account_is_sendable(account) -> bool:
    """
    Whether this number can send at all right now.

    Connected *and* not paused. Pausing is deliberately folded in here rather
    than left to the send gate alone, so that an automatic campaign routes
    around a number that has paused itself the same way it routes around one
    that dropped off -- by giving the recipient to a different number, not by
    queueing behind a device that isn't sending.
    """
    if account.status != WhatsAppAccount.Status.CONNECTED:
        return False

    from apps.whatsapp.health import pause_state

    paused, _ = pause_state(account)
    return not paused


def ensure_sendable_account(message: Message) -> WhatsAppAccount | None:
    """
    Resolve the account this message should actually go out on, right now.

    Returns the account to send from, or None when the message cannot be
    attempted at all. If the assigned account is no longer connected and the
    campaign assigns accounts automatically, a different eligible account is
    chosen and persisted, so one device dropping mid-campaign costs at most
    the messages already in flight rather than every remaining recipient.

    Concurrency: the reassignment is a conditional UPDATE guarded on the
    message still being QUEUED. A worker that has already claimed the message
    (flipped it to SENDING in attempt_send) therefore can't have the account
    swapped underneath it, and two workers racing here converge -- whichever
    UPDATE lands second simply overwrites with an equally eligible account,
    and only one of them goes on to claim the send.
    """
    account = message.whatsapp_account
    if account_is_sendable(account):
        return account

    campaign = message.campaign if message.campaign_id else None
    if campaign is None:
        # Inbox replies and automation sends have no account pool to fall
        # back to. Hand the account back as-is rather than reporting "no
        # account": the provider call then fails the way it always has, and
        # keeps its existing retry-then-FAILED path, which gives a briefly
        # disconnected device time to come back.
        return account

    from apps.campaigns.assignment import reselect_account

    replacement = reselect_account(campaign, account.pk)
    if replacement is None:
        # A disconnected account with nowhere to move to still means "no
        # account available". A merely paused one is handed back instead, so
        # the send gate can hold the message until the pause lifts -- a number
        # that paused itself for an hour must not cost the campaign every
        # recipient it had left.
        return account if account.status == WhatsAppAccount.Status.CONNECTED else None

    reassigned = Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(
        whatsapp_account=replacement
    )
    if not reassigned:
        return None  # no longer ours to reassign; another worker owns it

    message.whatsapp_account = replacement
    message.whatsapp_account_id = replacement.pk
    logger.warning(
        "Campaign %s | recipient %s | account %s (id=%s) is %s -- reassigned to %s (id=%s)",
        campaign.pk, message.to_phone, account.name, account.pk, account.status,
        replacement.name, replacement.pk,
    )
    return replacement


def mark_skipped(message: Message, reason: str, code: str = "") -> bool:
    """
    Record that a recipient was never attempted, without losing it.

    Returns True if this call is the one that skipped the message. Guarded on
    QUEUED so a message another worker is actively sending is never skipped
    out from under it.

    `code` is one of Message.FailureReason, and is what a report groups by.
    It is optional so that a caller which genuinely has no better answer than
    the sentence leaves it blank rather than inventing one -- a wrong code is
    worse than none, because a wrong one gets counted.
    """
    skipped = Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(
        status=Message.Status.SKIPPED,
        error_message=reason[:500],
        failure_reason=code or "",
        failed_at=timezone.now(),
    )
    if not skipped:
        return False

    logger.warning(
        "Campaign %s | recipient %s | account: %s (id=%s) | result: SKIPPED | reason: %s",
        message.campaign_id, message.to_phone, message.whatsapp_account.name,
        message.whatsapp_account_id, reason,
    )

    # Announced because a skip is a terminal outcome for that recipient, and an
    # integration waiting on this message would otherwise wait forever. The row
    # above is written with `.update()`, so nothing else here fires.
    message.status = Message.Status.SKIPPED
    message_status_changed.send(sender=Message, message=message, status=Message.Status.SKIPPED)
    return True


@dataclass
class SendOutcome:
    """
    What one run of `attempt_send` did, for the task to act on.

    `disposition` is one of:

    * ``"stale"``      -- our lease was reclaimed mid-send (we were too slow);
                          the recovery worker owns the outcome now, do nothing.
    * ``"sent"``        -- the message is SENT and the attempt CONFIRMED; the
                          task settles the credit and fires the send signals.
    * ``"recovery_required"`` -- the outcome is genuinely unknown; the message
                          is RECOVERY_REQUIRED, the credit is left reserved
                          (neither charged nor refunded), nothing is retried.
    * ``"provider_rejected"`` -- the provider was reached and definitively said
                          no; `result` carries the SendResult and the message
                          is still SENDING under our lease so the task can
                          decide retry vs. terminal via `release_for_retry`
                          / `finalize_failure`.

    (`attempt_send` returns ``None`` for the "another worker already claimed
    this" case, matching its historical contract.)

    The ``success`` / ``error`` / ``provider_message_id`` / ``ambiguous`` /
    ``code`` properties mirror the old ``SendResult`` return so callers that
    only wanted "did it send?" keep working unchanged.
    """

    disposition: str
    result: SendResult | None = None
    attempt: MessageSendAttempt | None = None

    @property
    def success(self) -> bool:
        return self.disposition == "sent"

    @property
    def error(self) -> str | None:
        return self.result.error if self.result else None

    @property
    def provider_message_id(self) -> str | None:
        return self.result.provider_message_id if self.result else None

    @property
    def ambiguous(self) -> bool:
        return bool(self.result and self.result.ambiguous) or self.disposition == "recovery_required"

    @property
    def code(self) -> str:
        return (self.result.code if self.result else "") or ""


def attempt_send(message: Message, account=None, *, task_id: str = "") -> SendOutcome:
    """
    Claim one QUEUED message under a lease, hand it to the provider once, and
    record what happened -- crash-safely.

    ``account`` defaults to the message's own assigned account; the task passes
    the (possibly reassigned) one from ``ensure_sendable_account``.

    The dangerous stretch is between the provider accepting the message and
    this process recording that it did. A worker that dies there leaves the
    message SENDING with an expired lease and an attempt row that says how far
    it got; `apps/messaging/recovery.py` reconciles it. This function never
    resets SENDING -> QUEUED on an uncertain outcome, and never retries an
    ambiguous send.
    """
    if account is None:
        account = message.whatsapp_account

    attempt = claim_for_send(message, task_id=task_id)
    if attempt is None:
        return None  # another worker already claimed this message

    # The worker is now gone, if a test armed this. Message: SENDING. Attempt:
    # CLAIMED, submission_started_at NULL -> the sweep classifies this as
    # "never submitted" and safely re-queues it.
    crash_at("AFTER_CLAIM")

    now = timezone.now()
    advance_attempt(
        attempt,
        status=MessageSendAttempt.Status.SENDING,
        last_heartbeat_at=now,
    )

    logger.info(
        "PROVIDER_REQUEST_STARTED",
        extra={
            "event": "message.provider_request_started",
            "message_id": message.pk, "attempt_id": attempt.pk,
            "operation_id": attempt.idempotency_key, "provider": attempt.provider,
            "account_id": account.pk, "campaign_id": message.campaign_id,
            "tenant_id": message.tenant_id,
        },
    )
    crash_at("BEFORE_PROVIDER_CALL")

    # Stamped immediately before the provider is actually called, NOT at the
    # claim: this is the line that divides "provably never submitted, safe to
    # re-queue" from "submission may have happened, must not blindly resend".
    advance_attempt(attempt, submission_started_at=timezone.now())

    try:
        result = _dispatch_to_provider(message, account, attempt)
    except SoftTimeLimitExceeded:
        # The task ran past its soft limit -- almost always a provider call
        # that hung. We transmitted something and never heard back: unknown,
        # not failed. Mark it and let the task finish tidily rather than
        # being hard-killed with the row still SENDING.
        _persist_recovery_required(
            message, attempt, "The send exceeded its time limit before the provider replied.",
        )
        return SendOutcome(disposition="recovery_required", attempt=attempt)
    except Exception as exc:  # noqa: BLE001 -- an unexpected error mid-send is ambiguous, not a clean failure
        logger.exception("Unexpected error sending message %s; treating as unknown.", message.pk)
        _persist_recovery_required(message, attempt, f"Unexpected send error: {exc}"[:480])
        return SendOutcome(disposition="recovery_required", attempt=attempt)

    crash_at("AFTER_PROVIDER_CALL")
    if result.success:
        crash_at("AFTER_PROVIDER_ACCEPT")

    return _persist_send_result(message, account, attempt, result)


def _dispatch_to_provider(message: Message, account, attempt: MessageSendAttempt) -> SendResult:
    """Route to the right provider call for this message's content kind, threading the operation id."""
    op = attempt.idempotency_key
    if message.poll_content:
        return _send_poll(message, account, attempt)
    if message.list_content:
        return _send_list(message, account, attempt)
    if message.buttons:
        return _send_with_buttons(message, account, attempt)
    if message.message_type == Message.MessageType.TEXT:
        return ConnectionManager.send_text_message(account, message.to_phone, message.body, operation_id=op)
    return _send_attachments(message, account, attempt)


def _persist_send_result(message, account, attempt, result: SendResult) -> SendOutcome:
    """
    Second short transaction: write the outcome, guarded on still owning the lease.

    A worker whose lease was reclaimed by the sweep while it was in the
    provider call must not write message state -- the recovery worker has
    already decided. It records the outcome on its own attempt row and stops.
    """
    crash_at("BEFORE_DB_UPDATE")
    now = timezone.now()

    with transaction.atomic():
        owned = Message.objects.select_for_update().filter(
            pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
        ).exists()

        if not owned:
            advance_attempt(
                attempt,
                status=MessageSendAttempt.Status.EXPIRED,
                last_error="Lease reclaimed before the result could be recorded.",
                provider_message_id=result.provider_message_id or attempt.provider_message_id,
            )
            logger.warning(
                "LEASE_EXPIRED",
                extra={"event": "message.result_after_lease_lost", "message_id": message.pk,
                       "attempt_id": attempt.pk, "operation_id": attempt.idempotency_key},
            )
            return SendOutcome(disposition="stale", attempt=attempt)

        if result.success:
            Message.objects.filter(pk=message.pk).update(
                status=Message.Status.SENT,
                provider_message_id=result.provider_message_id,
                sent_at=now,
                lease_owner="",
                lease_expires_at=None,
                updated_at=now,
            )
            advance_attempt(
                attempt,
                status=MessageSendAttempt.Status.CONFIRMED,
                submission_completed_at=now,
                provider_message_id=result.provider_message_id,
            )
            # Consumed in the same transaction as the SENT write, keyed on the
            # message so it is a no-op if it already happened. This closes the
            # last leak: a worker that died after "SENT" but before the task's
            # bookkeeping used to hold the reservation for ever, and the sweep
            # never looks at a SENT row.
            if message.billed_to_id:
                from apps.credits.services import consume_credit

                consume_credit(
                    user_id=message.billed_to_id, reference=f"message:{message.pk}",
                    operation_id=f"msg-consume:{message.pk}",
                )
            message.status = Message.Status.SENT
            message.provider_message_id = result.provider_message_id
            message.sent_at = now
            message.lease_owner = ""
            logger.info(
                "PROVIDER_ACCEPTED",
                extra={"event": "message.provider_accepted", "message_id": message.pk,
                       "attempt_id": attempt.pk, "operation_id": attempt.idempotency_key,
                       "provider_message_id": result.provider_message_id},
            )
            disposition = "sent"

        elif result.ambiguous:
            _write_recovery_required(
                message, attempt, now,
                error=result.error or "The provider was reached but never confirmed the send.",
                code=result.code,
            )
            disposition = "recovery_required"

        else:
            # A definite rejection. Leave the message SENDING under our lease
            # and let the task choose retry vs. terminal; record this attempt
            # as failed.
            advance_attempt(
                attempt,
                status=MessageSendAttempt.Status.FAILED,
                submission_completed_at=now,
                last_error=(result.error or "")[:480],
                provider_status=(result.code or ""),
            )
            logger.info(
                "PROVIDER_REJECTED",
                extra={"event": "message.provider_rejected", "message_id": message.pk,
                       "attempt_id": attempt.pk, "operation_id": attempt.idempotency_key,
                       "code": result.code, "error": (result.error or "")[:200]},
            )
            disposition = "provider_rejected"

    # Transaction committed. A crash here has the SENT row and its credit
    # already durable, which is the safe state.
    if disposition == "sent":
        crash_at("AFTER_DB_UPDATE")
    return SendOutcome(disposition=disposition, result=result, attempt=attempt)


#: Appended to a RECOVERY_REQUIRED message's error_message. The provider
#: detail in front of it is trimmed to fit; this sentence is never trimmed,
#: because it is the part a person has to read.
_UNKNOWN_NOTE = (
    "Delivery could not be confirmed, so this message was not retried "
    "automatically -- it may or may not have been received."
)


def _unknown_error_message(error: str) -> str:
    detail = f"{error} " if error else ""
    return detail[: 500 - len(_UNKNOWN_NOTE)] + _UNKNOWN_NOTE


def _write_recovery_required(message, attempt, now, *, error: str, code: str = "") -> None:
    """Set the message to RECOVERY_REQUIRED and the attempt to UNKNOWN, in the current transaction."""
    Message.objects.filter(pk=message.pk).update(
        status=Message.Status.RECOVERY_REQUIRED,
        failure_reason=Message.FailureReason.DELIVERY_UNCONFIRMED,
        error_message=_unknown_error_message(error),
        lease_owner="",
        lease_expires_at=None,
        updated_at=now,
    )
    advance_attempt(
        attempt,
        status=MessageSendAttempt.Status.UNKNOWN,
        submission_completed_at=now,
        last_error=(error or "")[:480],
        provider_status=(code or ""),
    )
    message.status = Message.Status.RECOVERY_REQUIRED
    message.lease_owner = ""
    logger.error(
        "MESSAGE_MARKED_UNKNOWN",
        extra={"event": "send.recovery.unknown", "message_id": message.pk,
               "attempt_id": attempt.pk, "operation_id": attempt.idempotency_key,
               "campaign_id": message.campaign_id, "tenant_id": message.tenant_id,
               "provider": attempt.provider, "account_id": attempt.account_id},
    )


def _persist_recovery_required(message, attempt, error: str) -> None:
    """Standalone (own transaction) RECOVERY_REQUIRED write, for the exception paths."""
    with transaction.atomic():
        owned = Message.objects.select_for_update().filter(
            pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
        ).exists()
        if not owned:
            advance_attempt(attempt, status=MessageSendAttempt.Status.EXPIRED, last_error=error[:480])
            return
        _write_recovery_required(message, attempt, timezone.now(), error=error)


def release_for_retry(message: Message, attempt: MessageSendAttempt) -> bool:
    """
    Hand a rejected message back to the queue for another attempt.

    Only ever called for a *definite* provider rejection (never ambiguous,
    never a crash): the provider said no, so re-queuing and trying again from
    a fresh attempt row is safe. Returns False if the lease is no longer ours.
    """
    now = timezone.now()
    released = Message.objects.filter(
        pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
    ).update(
        status=Message.Status.QUEUED,
        lease_owner="",
        lease_expires_at=None,
        lease_heartbeat_at=None,
        updated_at=now,
    )
    if not released:
        return False
    message.status = Message.Status.QUEUED
    message.lease_owner = ""
    logger.info(
        "MESSAGE_RETRY_SCHEDULED",
        extra={"event": "message.retry_scheduled", "message_id": message.pk,
               "attempt_id": attempt.pk, "operation_id": attempt.idempotency_key},
    )
    return True


def finalize_failure(message: Message, attempt: MessageSendAttempt | None, *, error: str, reason: str) -> bool:
    """
    Terminal failure for a definite provider rejection. Returns False if the lease moved.

    The credit/allowance release and the health note stay in the task, exactly
    where they were -- this only owns the message row and the lease.
    """
    now = timezone.now()
    finalized = Message.objects.filter(
        pk=message.pk, status=Message.Status.SENDING, lease_owner=message.lease_owner,
    ).update(
        status=Message.Status.FAILED,
        failure_reason=reason or "",
        error_message=(error or "")[:500],
        failed_at=now,
        lease_owner="",
        lease_expires_at=None,
        updated_at=now,
    )
    if not finalized:
        return False
    message.status = Message.Status.FAILED
    message.lease_owner = ""
    if attempt is not None and attempt.status not in MessageSendAttempt.TERMINAL_STATUSES:
        advance_attempt(attempt, status=MessageSendAttempt.Status.FAILED, submission_completed_at=now)
    return True


def _op(attempt: MessageSendAttempt | None, slot: str) -> str:
    """
    Per-provider-call idempotency id.

    One message can be several provider calls (attachments, then an interactive
    bubble). Each needs its *own* operation id or the bridge's dedup store would
    treat call 2 as a replay of call 1 and skip it. `slot` is ``a1``..``aN``
    for attachments and ``main`` for the primary bubble.
    """
    if attempt is None:
        return ""
    return f"{attempt.idempotency_key}:{slot}"


def _lease_lost_result() -> SendResult:
    """The result to return when the lease was reclaimed mid-send: ambiguous, never retried."""
    return SendResult(
        success=False,
        error="The send lease expired while this message was in flight; outcome unknown.",
        ambiguous=True,
        code="lease_lost",
    )


def _send_attachments(message: Message, account, attempt: MessageSendAttempt | None = None) -> SendResult:
    """
    Deliver every attachment on this message, in order.

    WhatsApp has no multi-file message type, so "one message with three
    images" is three provider calls. The Message row stays a single row --
    one credit, one report line, one recipient -- and succeeds only if every
    attachment went out. A failure part-way through returns immediately rather
    than pressing on: the recipient has already received the earlier files, and
    continuing would only add more failures to explain.

    The lease is renewed between attachments (a five-image send over a slow
    link can outlast a single lease), and the loop aborts if the lease was
    reclaimed -- continuing would race the recovery worker.

    The provider id kept is the first attachment's, which is the id delivery
    and read webhooks for this send arrive against.
    """
    items = resolve_media_items(spec_from_object(message))
    if not items:
        return SendResult(
            success=False, error="This message has no attachment to send.", code="invalid_request",
        )

    first_result = None
    for position, item in enumerate(items, start=1):
        if position > 1 and attempt is not None and not renew_lease(message, attempt):
            return _lease_lost_result()
        # A per-attachment caption wins; otherwise the message-level caption
        # labels the first file only, so a three-image send doesn't repeat the
        # same line three times.
        caption = item.get("caption") or (message.caption if position == 1 else "")
        result = ConnectionManager.send_media_message(
            account,
            message.to_phone,
            item["type"].lower(),
            item["url"],
            caption or None,
            item.get("filename") or None,
            operation_id=_op(attempt, f"a{position}"),
        )
        if first_result is None:
            first_result = result
        if not result.success:
            if position > 1:
                logger.warning(
                    "Message %s | recipient %s | attachment %s of %s failed after %s already sent: %s",
                    message.pk, message.to_phone, position, len(items), position - 1, result.error,
                )
                return SendResult(
                    success=False,
                    error=f"Attachment {position} of {len(items)} failed: {result.error}",
                    ambiguous=result.ambiguous,
                    code=result.code,
                )
            return result

    return first_result


def _send_with_buttons(message: Message, account, attempt: MessageSendAttempt | None = None) -> SendResult:
    """
    Deliver a message whose text carries interactive buttons.

    Attachments go out first and the interactive message last, so the buttons
    sit on the newest bubble in the thread where they stay tappable -- see
    WhatsAppProvider.send_interactive_message for why they aren't folded into
    the attachment as a header.

    On a connection that has no interactive support the buttons are written
    out as a numbered list instead of being dropped: the recipient still sees
    the choices and can type one back, which keyword rules already handle. The
    alternative -- failing the send -- would break a campaign the moment one of
    its accounts happened to be a non-Cloud-API connection.
    """
    body = (message.body or message.caption or "").strip()
    if not body:
        return SendResult(
            success=False,
            error="A message with buttons needs body text to show them under.",
            code="invalid_request",
        )

    attachment_result = None
    if resolve_media_items(spec_from_object(message)):
        attachment_result = _send_attachments(message, account, attempt)
        if not attachment_result.success:
            return attachment_result

    if attempt is not None and not renew_lease(message, attempt):
        return _lease_lost_result()

    numbered = not ConnectionManager.supports_interactive_buttons(account)
    if numbered:
        logger.info(
            "Message %s | account %s (id=%s) cannot send interactive buttons -- "
            "delivering them as a numbered list instead.",
            message.pk, account.name, account.pk,
        )
        result = ConnectionManager.send_text_message(
            account, message.to_phone, as_numbered_options(body, message.buttons),
            operation_id=_op(attempt, "main"),
        )
    else:
        result = ConnectionManager.send_interactive_message(
            account, message.to_phone, body, message.buttons, operation_id=_op(attempt, "main"),
        )

    if not result.success:
        if attachment_result is not None:
            return SendResult(
                success=False,
                error=f"The attachments were sent but the message with buttons was not: {result.error}",
                ambiguous=result.ambiguous,
                code=result.code,
            )
        return result

    # Only once the list is actually on its way. Recording the question before
    # the send could leave a conversation waiting on an answer to something the
    # customer was never asked, and the first number they typed for any other
    # reason would fire an automation.
    if numbered:
        record_pending_prompt(message)

    # Same rule as _send_attachments: the id kept is the first send's, because
    # that is the one this row's delivery/read webhooks arrive against.
    return attachment_result or result


def _send_list(message: Message, account, attempt: MessageSendAttempt | None = None) -> SendResult:
    """
    Deliver a message whose body carries a tappable menu.

    Structurally identical to `_send_with_buttons`, and deliberately so: the
    two differ only in which provider call is made when the connection can do
    it natively, and in which text is produced when it cannot. Attachments go
    first so the menu is the newest bubble; the fallback is the same numbered
    list, recorded as the same PendingButtonPrompt, resolved by the same
    automation code -- a contact who types "2" answers a menu on a QR number
    exactly as they answer a button message on one.
    """
    from core.interactive import as_options, list_as_text

    body = (message.body or message.caption or "").strip()
    if not body:
        return SendResult(
            success=False,
            error="A list message needs body text to show the menu under.",
            code="invalid_request",
        )

    attachment_result = None
    if resolve_media_items(spec_from_object(message)):
        attachment_result = _send_attachments(message, account, attempt)
        if not attachment_result.success:
            return attachment_result

    if attempt is not None and not renew_lease(message, attempt):
        return _lease_lost_result()

    numbered = not ConnectionManager.supports(account, "lists")
    if numbered:
        logger.info(
            "Message %s | account %s (id=%s) cannot send list messages -- "
            "delivering the menu as a numbered list instead.",
            message.pk, account.name, account.pk,
        )
        result = ConnectionManager.send_text_message(
            account, message.to_phone, list_as_text(body, message.list_content),
            operation_id=_op(attempt, "main"),
        )
    else:
        result = ConnectionManager.send_list_message(
            account, message.to_phone, body, message.list_content,
            operation_id=_op(attempt, "main"),
        )

    if not result.success:
        if attachment_result is not None:
            return SendResult(
                success=False,
                error=f"The attachments were sent but the menu was not: {result.error}",
                ambiguous=result.ambiguous,
                code=result.code,
            )
        return result

    if numbered:
        record_pending_prompt(message, options=as_options(list_content=message.list_content))

    return attachment_result or result


def _send_poll(message: Message, account, attempt: MessageSendAttempt | None = None) -> SendResult:
    """
    Deliver a poll.

    A poll is its own bubble with no body text, so a message that has both
    sends the text first and the poll second -- the same "several provider
    calls, one Message row" shape `_send_attachments` already uses, and for
    the same reason: one credit, one report line, one recipient.

    On a connection with no poll support the whole thing collapses into a
    single numbered-list message, which is both cheaper and more legible than
    two bubbles that only make sense together.
    """
    from core.interactive import as_options, poll_as_text

    body = (message.body or message.caption or "").strip()

    attachment_result = None
    if resolve_media_items(spec_from_object(message)):
        attachment_result = _send_attachments(message, account, attempt)
        if not attachment_result.success:
            return attachment_result

    if attempt is not None and not renew_lease(message, attempt):
        return _lease_lost_result()

    if not ConnectionManager.supports(account, "polls"):
        logger.info(
            "Message %s | account %s (id=%s) cannot send polls -- "
            "delivering the question as a numbered list instead.",
            message.pk, account.name, account.pk,
        )
        result = ConnectionManager.send_text_message(
            account, message.to_phone, poll_as_text(body, message.poll_content),
            operation_id=_op(attempt, "main"),
        )
        if not result.success:
            return _partial_failure(attachment_result, result, "the poll")
        # A degraded poll is answerable by number, so it gets a prompt exactly
        # as a degraded list does. A *native* poll does not: WhatsApp reports
        # those as votes, not as messages, and recording a numbered question
        # nobody was asked would make the next stray digit fire an automation.
        record_pending_prompt(message, options=as_options(poll_content=message.poll_content))
        return attachment_result or result

    text_result = None
    if body:
        text_result = ConnectionManager.send_text_message(
            account, message.to_phone, body, operation_id=_op(attempt, "intro"),
        )
        if not text_result.success:
            return _partial_failure(attachment_result, text_result, "the poll's introduction")

    result = ConnectionManager.send_poll_message(
        account, message.to_phone, message.poll_content, operation_id=_op(attempt, "main"),
    )
    if not result.success:
        return _partial_failure(attachment_result or text_result, result, "the poll")

    # Same rule as everywhere else on this path: the id kept is the first
    # send's, because that is the one this row's delivery/read webhooks arrive
    # against.
    return attachment_result or text_result or result


def _partial_failure(earlier: SendResult | None, failed: SendResult, what: str) -> SendResult:
    """
    A failure that happened after something had already reached the recipient.

    Says so, rather than reporting a bare provider error: "it failed" and "the
    attachment arrived and then it failed" call for different actions, and
    only the row can tell somebody which happened.
    """
    if earlier is None:
        return failed
    return SendResult(
        success=False,
        error=f"Part of this message was sent but {what} was not: {failed.error}",
        ambiguous=failed.ambiguous,
        code=failed.code,
    )


#: How long a numbered question stays answerable. Long enough for somebody to
#: put their phone down and come back to it, short enough that a stray "2" the
#: next morning is not read as an answer to yesterday's question -- acting on
#: that would fire an automation nobody asked for.
DEFAULT_PROMPT_TIMEOUT_MINUTES = 30


def prompt_timeout_minutes() -> int:
    try:
        minutes = int(getattr(settings, "BUTTON_PROMPT_TIMEOUT_MINUTES", DEFAULT_PROMPT_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_PROMPT_TIMEOUT_MINUTES
    return max(minutes, 1)


def record_pending_prompt(message: Message, options=None):
    """
    Remember what the numbers in a just-sent list meant.

    Every owning id is read from the message row -- never from anything a
    contact sent -- so the question can only ever be answered by the
    conversation it was asked in. `automation.services.resolve_option_reply` is
    the only reader.

    `options` is the offered choices in ``core.buttons`` item shape, and
    defaults to the message's own buttons. A degraded list or poll passes its
    rows/options converted by ``core.interactive.as_options`` -- which is the
    whole trick that lets one prompt table, one matcher and one automation
    ladder serve all three kinds of question.

    Returns the row, or None when there is nothing to answer against: a
    link-only message has no numbers, and a message with no conversation or
    contact has nobody to attribute a reply to.
    """
    from core.buttons import reply_button_ids

    from .models import PendingButtonPrompt

    if options is None:
        options = message.buttons

    if not (message.conversation_id and message.contact_id):
        return None
    if not reply_button_ids(options):
        return None

    # A new question supersedes whatever was outstanding. Two open questions on
    # one conversation would make "2" ambiguous, and the honest reading of a
    # number is always the most recent thing the customer was asked.
    PendingButtonPrompt.objects.filter(
        conversation_id=message.conversation_id, answered_at__isnull=True,
    ).update(answered_at=timezone.now(), resolved_button_id="")

    return PendingButtonPrompt.objects.create(
        tenant_id=message.tenant_id,
        conversation_id=message.conversation_id,
        contact_id=message.contact_id,
        whatsapp_account_id=message.whatsapp_account_id,
        message=message,
        options=list(options or []),
        expires_at=timezone.now() + timedelta(minutes=prompt_timeout_minutes()),
    )


def refresh_campaign_counters(campaign_id) -> None:
    if not campaign_id:
        return

    from apps.campaigns.models import Campaign

    tally = {
        row["status"]: row["total"]
        for row in Message.objects.filter(campaign_id=campaign_id).values("status").annotate(total=Count("id"))
    }

    sent = tally.get(Message.Status.SENT, 0) + tally.get(Message.Status.DELIVERED, 0) + tally.get(Message.Status.READ, 0)
    delivered = tally.get(Message.Status.DELIVERED, 0) + tally.get(Message.Status.READ, 0)
    read = tally.get(Message.Status.READ, 0)
    failed = tally.get(Message.Status.FAILED, 0)
    skipped = tally.get(Message.Status.SKIPPED, 0)
    # RECOVERY_REQUIRED is a *recoverable-terminal* state: the outcome is
    # unknown and a human or the reconciliation sweep must settle it, but the
    # campaign is not still working on it. So it does not count as pending --
    # otherwise one dead worker would hold a campaign in RUNNING for ever --
    # and it is NOT folded into failed_count, because a failed message may be
    # retried and an unknown one must not be blindly resent.
    recovery_required = tally.get(Message.Status.RECOVERY_REQUIRED, 0)
    # SKIPPED is terminal, so it must not count as pending -- otherwise a
    # campaign whose last recipients had no account left would never reach
    # COMPLETED and would sit as RUNNING forever. SENDING still counts as
    # pending, but the recovery sweep (apps/messaging/recovery.py) guarantees
    # a dead-lease SENDING row leaves SENDING within one sweep interval, so it
    # can no longer wedge completion.
    pending = tally.get(Message.Status.QUEUED, 0) + tally.get(Message.Status.SENDING, 0)

    Campaign.objects.filter(pk=campaign_id).update(
        sent_count=sent, delivered_count=delivered, read_count=read, failed_count=failed,
        skipped_count=skipped, recovery_required_count=recovery_required,
    )

    if pending == 0:
        completed = Campaign.objects.filter(
            pk=campaign_id, status=Campaign.Status.RUNNING
        ).update(status=Campaign.Status.COMPLETED, completed_at=timezone.now())
        # Guarded on the update having actually landed, so the two workers
        # finishing the last two messages produce one completion event between
        # them rather than one each.
        if completed:
            from apps.campaigns.signals import campaign_status_changed

            campaign = Campaign.objects.filter(pk=campaign_id).first()
            if campaign is not None:
                campaign_status_changed.send(
                    sender=Campaign, campaign=campaign, status=Campaign.Status.COMPLETED,
                )
