import logging

from django.db import transaction
from django.utils import timezone

from apps.contacts.models import Contact
from apps.contacts.segments import resolve_segment_queryset
from apps.credits.services import InsufficientCreditsError, release_credits, reserve_credits
from apps.messaging.models import Message
from apps.scheduling import services as scheduling
from apps.scheduling.models import ScheduledJob
from apps.subscriptions.services import check_quota
from apps.templates_app.services import (
    build_render_context,
    render_buttons,
    render_interactive,
    render_text,
)
from apps.whatsapp.models import WhatsAppAccount

from .assignment import assign_accounts, describe_distribution, eligible_accounts
from .models import Campaign
from .signals import campaign_status_changed

logger = logging.getLogger("sendstack")


class CampaignError(Exception):
    """Raised for campaign lifecycle actions that don't apply given the campaign's current state."""


def resolve_audience(campaign: Campaign):
    """Only ever returns subscribed contacts -- unsubscribed contacts can never be targeted."""
    base = Contact.objects.filter(tenant=campaign.tenant, subscribed=True)

    if campaign.audience_type == Campaign.AudienceType.ALL:
        return base
    if campaign.audience_type == Campaign.AudienceType.GROUP:
        return base.filter(groups=campaign.audience_group) if campaign.audience_group_id else base.none()
    if campaign.audience_type == Campaign.AudienceType.TAG:
        return base.filter(tags=campaign.audience_tag) if campaign.audience_tag_id else base.none()
    if campaign.audience_type == Campaign.AudienceType.SEGMENT:
        if not campaign.audience_segment_id:
            return base.none()
        return resolve_segment_queryset(campaign.tenant, campaign.audience_segment.filters).filter(subscribed=True)
    if campaign.audience_type == Campaign.AudienceType.CONTACT_IDS:
        return base.filter(id__in=campaign.audience_contact_ids)
    return base.none()


def estimate_recipients(campaign: Campaign) -> int:
    return resolve_audience(campaign).distinct().count()


def _supports_buttons(account, cache: dict) -> bool:
    """Whether this account can send tappable buttons, asked once per account."""
    if account.pk not in cache:
        from apps.whatsapp.connections.manager import ConnectionManager

        cache[account.pk] = ConnectionManager.supports_interactive_buttons(account)
    return cache[account.pk]


def _build_message(campaign: Campaign, contact, account, message_spec, *, policy=None, cache=None, utm=None,
                   track_links=None):
    """
    One recipient's message, rendered and ready to queue, plus the tracked
    links it needs.

    The opt-out and the link rewrite are both applied here rather than at send
    time so that the row is a faithful record of what the contact was actually
    offered -- if a campaign is ever questioned, the answer has to come from
    the message that was sent, not from a setting that may have been changed
    since.

    The links come back unsaved because the message has no primary key yet;
    `_build_messages` attaches them once the whole batch is inserted.
    """
    context = build_render_context(contact)
    variables = message_spec["variables"]

    fields = {
        "body": render_text(message_spec["body"], context, variables),
        "caption": render_text(message_spec.get("caption") or "", context, variables),
        # Button labels are personalized the same way the body is -- see
        # templates_app.services. Ids are deliberately left alone: they are
        # what automation matches on, and a per-recipient id would point every
        # rule at nothing.
        "buttons": render_buttons(message_spec.get("buttons") or [], context, variables),
    }
    # Kept out of `fields` because `apply_opt_out_affordance` below takes only
    # the three it can change, and a menu or a poll is not one of them.
    interactive = {
        "list_content": render_interactive(
            message_spec.get("list_content") or {}, context, variables, kind="list",
        ),
        "poll_content": render_interactive(
            message_spec.get("poll_content") or {}, context, variables, kind="poll",
        ),
    }

    links = []
    if track_links if track_links is not None else campaign.track_links:
        from apps.tracking.services import rewrite

        for field in ("body", "caption"):
            fields[field], field_links = rewrite(
                fields[field], tenant_id=campaign.tenant_id, campaign=campaign, contact=contact, utm=utm,
                # Answered once for the whole campaign by _build_messages; a
                # plan lookup per recipient would be one extra query per
                # message on the one path that builds them by the thousand.
                allowed=True,
            )
            links.extend(field_links)

    if campaign.append_opt_out:
        from apps.compliance.services import apply_opt_out_affordance

        # A menu or a poll cannot also carry an unsubscribe *button*: the
        # three are different kinds of message and there is no envelope that
        # holds two. Saying "buttons are not supported here" is what makes
        # apply_opt_out_affordance fall back to the footer line, so the
        # recipient still gets a way out -- which is the one thing that must
        # not be lost to a content-type choice.
        supports_buttons = not any(interactive.values()) and _supports_buttons(
            account, cache if cache is not None else {}
        )
        fields = apply_opt_out_affordance(policy, supports_buttons=supports_buttons, **fields)

    message = Message(
        tenant=campaign.tenant,
        campaign=campaign,
        template_id=campaign.template_id,
        billed_to_id=campaign.created_by_id,
        whatsapp_account=account,
        contact=contact,
        to_phone=contact.phone,
        kind=Message.Kind.MARKETING,
        message_type=message_spec["message_type"],
        media_url=message_spec.get("media_url") or "",
        media_items=message_spec.get("media_items") or [],
        filename=message_spec.get("filename") or "",
        **fields,
        **interactive,
    )
    return message, links


def _build_messages(campaign: Campaign) -> int:
    message_spec = campaign.effective_message()
    contacts = list(resolve_audience(campaign).distinct())

    # One account decision per recipient, made up front so the whole campaign
    # is auditable before a single message goes out. MANUAL campaigns get the
    # campaign's own account for every recipient (identical to the behaviour
    # before account assignment existed); AUTO campaigns get them dealt across
    # the connected pool. Either way the account is *re-checked* at send time
    # and can still be swapped if it drops -- see apps/messaging/services.py.
    assignments = assign_accounts(campaign, len(contacts))
    if contacts and not assignments:
        raise CampaignError("No connected WhatsApp account is available to send this campaign.")

    from apps.compliance.models import CompliancePolicy
    from apps.tracking.services import campaign_utm, save_links, tracking_allowed

    policy = CompliancePolicy.for_tenant(campaign.tenant_id)
    button_support: dict = {}
    # The plan's click-tracking switch, asked once for the campaign rather
    # than once per recipient. A campaign that asked for tracking on a plan
    # that does not include it simply sends the tenant's own URLs -- the
    # message still goes, and the links in it still work.
    track_links = bool(campaign.track_links) and tracking_allowed(campaign.tenant_id)
    utm = campaign_utm(campaign) if track_links else None

    messages, links_by_contact = [], {}
    for contact, account in zip(contacts, assignments):
        message, links = _build_message(
            campaign, contact, account, message_spec, policy=policy, cache=button_support, utm=utm,
            track_links=track_links,
        )
        messages.append(message)
        if links:
            links_by_contact[contact.pk] = links

    Message.objects.bulk_create(messages)

    # Only now do the messages have primary keys, so this is the first moment
    # a link can be tied to the exact message it was printed in. Attribution
    # to campaign + contact was already set when the link was built, so a
    # failure here costs the message-level detail and nothing else.
    if links_by_contact:
        all_links = []
        for message in messages:
            for link in links_by_contact.get(message.contact_id, ()):
                link.message_id = message.pk
                all_links.append(link)
        save_links(all_links)

    minimum, maximum = campaign.delay_bounds()
    logger.info(
        "Campaign %s | built %s message(s) | assignment=%s | accounts: %s | delay window: %.3fs-%.3fs",
        campaign.pk, len(messages), campaign.account_assignment_mode,
        describe_distribution(assignments) or "none", minimum, maximum,
    )
    return len(messages)


def launch(campaign: Campaign) -> Campaign:

    
    if campaign.status not in (Campaign.Status.DRAFT, Campaign.Status.SCHEDULED):
        raise CampaignError("Only draft or scheduled campaigns can be launched.")

    if campaign.account_assignment_mode == Campaign.AccountAssignmentMode.AUTO:
        if not eligible_accounts(campaign):
            raise CampaignError("No WhatsApp account is connected. Connect at least one device before launching.")
    elif campaign.whatsapp_account.status != WhatsAppAccount.Status.CONNECTED:
        raise CampaignError("The selected WhatsApp account is not connected.")
    elif not eligible_accounts(campaign):
        # Connected but paused. Said here rather than discovered one message at
        # a time: launching a campaign onto a number that is not going to send
        # is worse than being told up front why.
        raise CampaignError(
            "Sending from the selected WhatsApp number is paused. Resume it from the devices page, "
            "or switch this campaign to automatic account assignment."
        )

    message_spec = campaign.effective_message()
    # A poll is content on its own -- its question *is* the message, and a
    # poll-only campaign has no body by design. A list is not: it needs body
    # text to open the menu from, which the serializer already enforces, so it
    # is covered by the body check.
    if (
        not message_spec["body"]
        and not message_spec.get("media_items")
        and not message_spec.get("media_url")
        and not message_spec.get("poll_content")
    ):
        raise CampaignError("This campaign has no message content to send.")

    if not campaign.created_by_id:
        raise CampaignError("This campaign has no owner to charge credits to.")

    # Decided once, before the transaction, so the two branches below cannot
    # disagree about it if the clock ticks past `scheduled_at` mid-launch.
    scheduled = bool(campaign.scheduled_at and campaign.scheduled_at > timezone.now())

    with transaction.atomic():
        created = _build_messages(campaign)
        if created == 0:
            raise CampaignError("No subscribed contacts match this campaign's audience.")

        # Both gates, in the order that produces the more useful refusal.
        #
        # A customer must satisfy the plan's monthly allowance *and* their
        # credit balance -- credits never buy their way past the plan, and a
        # plan with room never sends without the credits to pay for it. The
        # allowance is checked first because "your plan allows 1,000 messages
        # a month" is a sentence somebody can act on, and being told about
        # credits first would just be answering the wrong question.
        #
        # This is a *pre-flight* check, not the authority: the send gate
        # claims each message's slot individually at send time (see
        # apps.subscriptions.services.claim_message_allowance), because a
        # campaign launched at 9am has no claim on the allowance an inbox
        # reply spends at 11. Refusing here means nobody launches ten
        # thousand messages that would be skipped one at a time.
        within_quota, quota_reason = check_quota(campaign.tenant_id, "messages_per_month", created)
        if not within_quota:
            raise CampaignError(quota_reason)

        try:
            reserve_credits(user_id=campaign.created_by_id, amount=created, reference=f"campaign:{campaign.pk}")
        except InsufficientCreditsError as exc:
            raise CampaignError(str(exc)) from exc

        campaign.total_recipients = created
        campaign.status = Campaign.Status.QUEUED
        campaign.started_at = timezone.now()
        campaign.save(update_fields=["total_recipients", "status", "started_at", "updated_at"])

        if scheduled:
            # **A durable row, not a broker ETA**, and written *inside* this
            # transaction on purpose.
            #
            # `apply_async(eta=...)` put the only copy of "send this on Friday"
            # in Redis, which in this deployment is a cache: no persistence
            # guarantee, flushed by an ordinary restart. That is the Phase 3
            # risk this closes.
            #
            # Committing it with the campaign closes the smaller gap that
            # replacing it could have left: a process that died between the
            # commit and a later scheduling call would have left a QUEUED
            # campaign that nothing was ever going to dispatch. Either both
            # rows exist or neither does. Safe to do in-transaction precisely
            # because it *is* a row -- the poller reads committed data, so
            # unlike a broker hand-off there is no worker that can see it
            # before it is real.
            scheduling.schedule(
                kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH,
                object_id=campaign.pk,
                tenant_id=campaign.tenant_id,
                run_at=campaign.scheduled_at,
            )

    if not scheduled:
        # Outside the transaction, and this one has to be: a worker can pick a
        # broker message up before the transaction it was published from has
        # committed, and would then find a campaign that is still DRAFT.
        from .tasks import dispatch_campaign

        dispatch_campaign.delay(campaign.pk)

    campaign_status_changed.send(sender=Campaign, campaign=campaign, status=campaign.status)
    return campaign


def pause(campaign: Campaign) -> Campaign:
    if campaign.status not in (Campaign.Status.QUEUED, Campaign.Status.RUNNING):
        raise CampaignError("Only queued or running campaigns can be paused.")
    campaign.status = Campaign.Status.PAUSED
    campaign.save(update_fields=["status", "updated_at"])
    return campaign


def resume(campaign: Campaign) -> Campaign:
    if campaign.status != Campaign.Status.PAUSED:
        raise CampaignError("Only paused campaigns can be resumed.")
    campaign.status = Campaign.Status.RUNNING
    campaign.save(update_fields=["status", "updated_at"])

    from .tasks import dispatch_campaign

    dispatch_campaign.delay(campaign.pk)
    return campaign


def cancel(campaign: Campaign) -> Campaign:
    if campaign.status in (Campaign.Status.COMPLETED, Campaign.Status.CANCELLED):
        raise CampaignError("This campaign has already finished.")

    campaign.status = Campaign.Status.CANCELLED
    campaign.completed_at = timezone.now()
    campaign.save(update_fields=["status", "completed_at", "updated_at"])

    # Retire the durable schedule too, so a cancelled campaign does not sit in
    # the operator's "due soon" list for ever. This is bookkeeping, not the
    # protection: what actually stops a cancelled campaign from sending is the
    # handler re-reading `campaign.status` at the moment it runs, which no
    # ordering of these two writes can defeat.
    scheduling.cancel(
        kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH,
        object_id=campaign.pk,
        reason="The campaign was cancelled.",
    )

    cancelled_count = campaign.messages.filter(status=Message.Status.QUEUED).update(status=Message.Status.CANCELLED)
    if cancelled_count and campaign.created_by_id:
        release_credits(user_id=campaign.created_by_id, amount=cancelled_count, reference=f"campaign:{campaign.pk}:cancel")

    campaign_status_changed.send(sender=Campaign, campaign=campaign, status=Campaign.Status.CANCELLED)
    return campaign


def retry_failed(campaign: Campaign) -> Campaign:
    failed_count = campaign.messages.filter(status=Message.Status.FAILED).count()
    if not failed_count:
        raise CampaignError("There are no failed messages to retry.")

    if not campaign.created_by_id:
        raise CampaignError("This campaign has no owner to charge credits to.")

    try:
        reserve_credits(user_id=campaign.created_by_id, amount=failed_count, reference=f"campaign:{campaign.pk}:retry")
    except InsufficientCreditsError as exc:
        raise CampaignError(str(exc)) from exc

    campaign.messages.filter(status=Message.Status.FAILED).update(
        status=Message.Status.QUEUED, retry_count=0, error_message="",
    )

    if campaign.status in (Campaign.Status.COMPLETED, Campaign.Status.FAILED):
        campaign.status = Campaign.Status.RUNNING
        campaign.save(update_fields=["status", "updated_at"])

    from .tasks import dispatch_campaign

    dispatch_campaign.delay(campaign.pk)
    return campaign
