import logging

from django.db.models import Count
from django.utils import timezone

from apps.whatsapp.connections.manager import ConnectionManager
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult
from core.buttons import as_numbered_options
from core.media import resolve_media_items, spec_from_object

from .models import Message

logger = logging.getLogger("multisender")


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


def mark_skipped(message: Message, reason: str) -> bool:
    """
    Record that a recipient was never attempted, without losing it.

    Returns True if this call is the one that skipped the message. Guarded on
    QUEUED so a message another worker is actively sending is never skipped
    out from under it.
    """
    skipped = Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(
        status=Message.Status.SKIPPED,
        error_message=reason[:500],
        failed_at=timezone.now(),
    )
    if not skipped:
        return False

    logger.warning(
        "Campaign %s | recipient %s | account: %s (id=%s) | result: SKIPPED | reason: %s",
        message.campaign_id, message.to_phone, message.whatsapp_account.name,
        message.whatsapp_account_id, reason,
    )
    return True


def attempt_send(message: Message) -> SendResult | None:
    """
    Try to send one QUEUED message exactly once.

    Returns None if another worker already claimed this message (or it's no
    longer QUEUED) -- the atomic status flip below is what makes concurrent
    / redelivered task execution safe.
    """
    claimed = Message.objects.filter(pk=message.pk, status=Message.Status.QUEUED).update(status=Message.Status.SENDING)
    if not claimed:
        return None
    message.refresh_from_db()

    account = message.whatsapp_account
    if message.buttons:
        result = _send_with_buttons(message, account)
    elif message.message_type == Message.MessageType.TEXT:
        result = ConnectionManager.send_text_message(account, message.to_phone, message.body)
    else:
        result = _send_attachments(message, account)

    if result.success:
        message.status = Message.Status.SENT
        message.provider_message_id = result.provider_message_id
        message.sent_at = timezone.now()
        message.save(update_fields=["status", "provider_message_id", "sent_at", "updated_at"])

    return result


def _send_attachments(message: Message, account) -> SendResult:
    """
    Deliver every attachment on this message, in order.

    WhatsApp has no multi-file message type, so "one message with three
    images" is three provider calls. The Message row stays a single row --
    one credit, one report line, one recipient -- and succeeds only if every
    attachment went out. A failure part-way through returns immediately rather
    than pressing on: the recipient has already received the earlier files, and
    continuing would only add more failures to explain.

    The provider id kept is the first attachment's, which is the id delivery
    and read webhooks for this send arrive against.
    """
    items = resolve_media_items(spec_from_object(message))
    if not items:
        return SendResult(success=False, error="This message has no attachment to send.")

    first_result = None
    for position, item in enumerate(items, start=1):
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
                )
            return result

    return first_result


def _send_with_buttons(message: Message, account) -> SendResult:
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
        return SendResult(success=False, error="A message with buttons needs body text to show them under.")

    attachment_result = None
    if resolve_media_items(spec_from_object(message)):
        attachment_result = _send_attachments(message, account)
        if not attachment_result.success:
            return attachment_result

    if ConnectionManager.supports_interactive_buttons(account):
        result = ConnectionManager.send_interactive_message(account, message.to_phone, body, message.buttons)
    else:
        logger.info(
            "Message %s | account %s (id=%s) cannot send interactive buttons -- "
            "delivering them as a numbered list instead.",
            message.pk, account.name, account.pk,
        )
        result = ConnectionManager.send_text_message(
            account, message.to_phone, as_numbered_options(body, message.buttons)
        )

    if not result.success:
        if attachment_result is not None:
            return SendResult(
                success=False,
                error=f"The attachments were sent but the message with buttons was not: {result.error}",
                ambiguous=result.ambiguous,
            )
        return result

    # Same rule as _send_attachments: the id kept is the first send's, because
    # that is the one this row's delivery/read webhooks arrive against.
    return attachment_result or result


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
    # SKIPPED is terminal, so it must not count as pending -- otherwise a
    # campaign whose last recipients had no account left would never reach
    # COMPLETED and would sit as RUNNING forever.
    pending = tally.get(Message.Status.QUEUED, 0) + tally.get(Message.Status.SENDING, 0)

    Campaign.objects.filter(pk=campaign_id).update(
        sent_count=sent, delivered_count=delivered, read_count=read, failed_count=failed,
        skipped_count=skipped,
    )

    if pending == 0:
        Campaign.objects.filter(pk=campaign_id, status=Campaign.Status.RUNNING).update(
            status=Campaign.Status.COMPLETED, completed_at=timezone.now(),
        )
