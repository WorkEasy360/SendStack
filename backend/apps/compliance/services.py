"""
Consent and quiet-hours enforcement.

Two jobs live here, both of which every WhatsApp sender is expected to get
right and neither of which the platform can infer on its own:

  * **Consent.** A contact who asks to be left alone must stop receiving
    messages immediately -- including from a campaign that was already
    launched and whose messages are sitting in the queue -- and the decision
    must be recorded with enough detail to answer for it later.
  * **Quiet hours.** Marketing that lands at 3am gets reported as spam, which
    is what actually gets a number banned. Messages outside the allowed window
    are held until it opens rather than dropped.

Nothing here decides *when* it runs. The send-side checks all funnel through
apps.messaging.gating, so there is exactly one place where a message can be
held or blocked, and the inbound side funnels through handle_inbound.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from core.buttons import MAX_REPLY_BUTTONS, REPLY, URL

from .keywords import OPT_OUT_BUTTON_ID, is_start, is_stop
from .models import CompliancePolicy, ConsentEvent
from .timezones import load_zone, zone_for_country

logger = logging.getLogger("sendstack")

#: Longest a message may be held for quiet hours. A window is at most a day
#: wide, so anything beyond this means a misconfigured policy (start == end
#: shifted by a DST change, say) and the message is better sent than lost.
MAX_QUIET_HOLD_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

def record_consent(contact, event, *, source, actor=None, message=None, detail="") -> bool:
    """
    Apply a consent decision to a contact and record why.

    Returns True when this call changed the contact's subscription state. The
    ConsentEvent row is written either way: "they asked again" and "they asked
    for the first time" are different facts, and only the append-only log can
    tell them apart afterwards.
    """
    opted_out = event == ConsentEvent.Event.OPT_OUT
    changed = contact.subscribed == opted_out

    if changed:
        contact.subscribed = not opted_out
        contact.unsubscribed_at = timezone.now() if opted_out else None
        contact.save(update_fields=["subscribed", "unsubscribed_at", "updated_at"])

    ConsentEvent.objects.create(
        tenant_id=contact.tenant_id, contact=contact, phone=contact.phone,
        event=event, source=source,
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        message=message, detail=detail[:255],
    )

    if changed:
        logger.info(
            "Contact %s (%s) %s via %s.",
            contact.pk, contact.phone, "opted out" if opted_out else "opted in", source,
        )
    return changed


def opt_out(contact, *, source, actor=None, message=None, detail="") -> bool:
    return record_consent(
        contact, ConsentEvent.Event.OPT_OUT, source=source, actor=actor, message=message, detail=detail,
    )


def opt_in(contact, *, source, actor=None, message=None, detail="") -> bool:
    return record_consent(
        contact, ConsentEvent.Event.OPT_IN, source=source, actor=actor, message=message, detail=detail,
    )


# ---------------------------------------------------------------------------
# The precedence rule
# ---------------------------------------------------------------------------

#: Returned by `may_receive_marketing` alongside the answer, so the caller can
#: put the real reason on the message row rather than inventing wording.
BLOCKED_UNSUBSCRIBED = "This contact has unsubscribed and was not messaged."
BLOCKED_NO_CONSENT = (
    "This contact has no recorded marketing consent, and this workspace only "
    "sends marketing to contacts who have given it."
)


def may_receive_marketing(contact, policy=None) -> tuple:
    """
    Whether a marketing message may go to this contact. Returns ``(ok, reason)``.

    **The one place the precedence rule lives:**

        explicit opt-out  >  marketing consent  >  campaign eligibility

    An opt-out is checked first and is absolute. Nothing outranks it: not a
    later import claiming consent, not a CRM sync, not a workflow, not the AI
    agent, not a plan that has paid for the feature. That ordering is the whole
    reason this is a function and not three checks in three call sites.

    The second rule is opt-in, and it only applies where the tenant has asked
    for it (CompliancePolicy.require_marketing_consent). A contact whose
    consent was never recorded is UNKNOWN, and UNKNOWN is refused under that
    setting -- refusing is the only safe reading of "we do not know".

    Called from `apps.messaging.gating.evaluate` and from nowhere else on the
    send path, so a new sending feature inherits it by construction.
    """
    from apps.contacts.models import Contact

    if contact is None:
        # No contact at all: a bare number typed into the composer. There is
        # nobody to have a recorded opt-out, and the caller (a person, in the
        # inbox) is the one making the decision.
        return True, ""

    if not contact.subscribed:
        return False, BLOCKED_UNSUBSCRIBED

    if policy is None:
        policy = CompliancePolicy.for_tenant(contact.tenant_id)

    if not policy.require_marketing_consent:
        return True, ""

    if contact.marketing_consent == Contact.MarketingConsent.GRANTED:
        return True, ""

    return False, BLOCKED_NO_CONSENT


def set_marketing_consent(contact, granted: bool, *, source, actor=None, reference="", detail="") -> bool:
    """
    Record a positive (or negative) marketing-consent decision, with its evidence.

    Returns whether anything changed.

    **Never clears an opt-out.** Granting consent sets the consent fields and
    nothing else: somebody who replied STOP stays unsubscribed until they
    themselves say otherwise, whatever a spreadsheet uploaded afterwards
    claims. Re-subscribing is `opt_in`, which is a separate, deliberate act.
    """
    from apps.contacts.models import Contact

    value = Contact.MarketingConsent.GRANTED if granted else Contact.MarketingConsent.DENIED
    if contact.marketing_consent == value and contact.consent_source == (source or ""):
        return False

    contact.marketing_consent = value
    contact.consent_source = source or ""
    contact.consent_at = timezone.now()
    contact.consent_reference = (reference or "")[:120]
    contact.save(update_fields=[
        "marketing_consent", "consent_source", "consent_at", "consent_reference", "updated_at",
    ])

    # The append-only log gets a row either way, because "we recorded consent
    # from the website on this date" is exactly the fact that has to survive
    # somebody later editing the contact.
    ConsentEvent.objects.create(
        tenant_id=contact.tenant_id,
        contact=contact,
        phone=contact.phone,
        event=ConsentEvent.Event.OPT_IN if granted else ConsentEvent.Event.OPT_OUT,
        source=_event_source_for(source),
        actor=actor if actor is not None and getattr(actor, "pk", None) else None,
        detail=(detail or f"Marketing consent {'recorded' if granted else 'refused'} ({source or 'unspecified'}).")[:255],
    )
    return True


def _event_source_for(consent_source: str) -> str:
    """Map a Contact.ConsentSource onto the ConsentEvent.Source that matches it."""
    mapping = {
        "MANUAL": ConsentEvent.Source.AGENT,
        "WEBSITE": ConsentEvent.Source.WEBSITE,
        "FORM": ConsentEvent.Source.FORM,
        "CRM": ConsentEvent.Source.CRM,
        "API": ConsentEvent.Source.API,
        "CSV": ConsentEvent.Source.IMPORT,
        "CUSTOMER_PROVIDED": ConsentEvent.Source.CUSTOMER_PROVIDED,
        "INBOUND": ConsentEvent.Source.INBOUND_KEYWORD,
        "OTHER": ConsentEvent.Source.OTHER,
    }
    return mapping.get(consent_source or "", ConsentEvent.Source.SYSTEM)


def handle_inbound(inbound_message) -> bool:
    """
    Act on an inbound message that asks to stop (or restart) messaging.

    Returns True when the message was such a request, which is the caller's
    signal to stand every other automation down: someone who just typed STOP
    must not also get a welcome message, a keyword auto-reply, or the next
    step of a workflow. The one message they may get back is the confirmation
    below, which is an acknowledgement of their request rather than a new one.
    """
    contact = inbound_message.contact
    if contact is None:
        return False

    policy = CompliancePolicy.for_tenant(inbound_message.tenant_id)

    # The unsubscribe button is honoured whether or not keyword detection is
    # switched on: we put that button on the message ourselves, so a tap is an
    # unambiguous answer to a question we asked.
    if inbound_message.replied_button_id == OPT_OUT_BUTTON_ID:
        if opt_out(
            contact, source=ConsentEvent.Source.INBOUND_BUTTON, message=inbound_message,
            detail="Tapped the unsubscribe button.",
        ):
            _send_confirmation(inbound_message, policy, policy.opt_out_confirmation
                               if policy.send_opt_out_confirmation else "")
        return True

    if not policy.stop_keywords_enabled:
        return False

    text = inbound_message.body or ""

    if is_stop(text, policy.extra_stop_keywords):
        if opt_out(
            contact, source=ConsentEvent.Source.INBOUND_KEYWORD, message=inbound_message,
            detail=f"Replied: {text.strip()[:200]}",
        ):
            _send_confirmation(inbound_message, policy, policy.opt_out_confirmation
                               if policy.send_opt_out_confirmation else "")
        return True

    # START only counts for someone who is currently unsubscribed. For anyone
    # else "start" is just a word, and swallowing it here would stop a keyword
    # rule that legitimately uses it from ever firing.
    if not contact.subscribed and is_start(text, policy.extra_start_keywords):
        if opt_in(
            contact, source=ConsentEvent.Source.INBOUND_KEYWORD, message=inbound_message,
            detail=f"Replied: {text.strip()[:200]}",
        ):
            _send_confirmation(inbound_message, policy, policy.opt_in_confirmation
                               if policy.send_opt_in_confirmation else "")
        return True

    return False


def _send_confirmation(inbound_message, policy, body) -> None:
    """
    Acknowledge a consent request.

    Sent policy-exempt: it goes to someone who has just unsubscribed, which
    every other outbound message to them is now forbidden, and it is owed to
    them at the time they asked rather than after the next quiet window.
    """
    if not (body or "").strip():
        return

    from apps.automation.services import send_automated_message

    send_automated_message(
        inbound_message.whatsapp_account,
        inbound_message.contact,
        {"message_type": "TEXT", "body": body, "variables": {}},
        conversation=inbound_message.conversation,
        reference=f"compliance:consent:{inbound_message.contact_id}",
        policy_exempt=True,
    )


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------

def tenant_zone(tenant_id):
    """
    The timezone a tenant's own clock runs on.

    Exposed because "what time is it for this customer" is not only a quiet
    hours question -- working hours ask it too, and two apps disagreeing about
    a tenant's timezone would be worse than either being wrong.
    """
    from .models import CompliancePolicy

    policy = CompliancePolicy.for_tenant(tenant_id)
    return load_zone(policy.timezone) or timezone.get_default_timezone()


def _resolve_zone(policy, contact):
    if policy.use_contact_timezone and contact is not None:
        zone = zone_for_country(getattr(contact, "country", ""))
        if zone is not None:
            return zone
    return load_zone(policy.timezone) or timezone.get_default_timezone()


def _is_quiet(moment, start, end) -> bool:
    """
    Whether a local time falls inside the quiet window.

    A window whose ends are equal is treated as no window at all rather than
    as a 24-hour one -- the latter would hold every message forever, which is
    never what someone setting two identical times meant.
    """
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end  # wraps past midnight


def quiet_hours_delay(policy, contact, now=None) -> float:
    """
    Seconds to hold a message for, or 0.0 when it may go out now.

    The delay is recomputed from scratch on every attempt, so a value that
    turns out to be slightly wrong (a DST shift moving the boundary between
    one attempt and the next) self-corrects: the message is simply held again
    for the remaining few minutes rather than sent early.
    """
    if not policy.quiet_hours_enabled:
        return 0.0

    now = now or timezone.now()
    local = now.astimezone(_resolve_zone(policy, contact))
    if not _is_quiet(local.time(), policy.quiet_hours_start, policy.quiet_hours_end):
        return 0.0

    opens = local.replace(
        hour=policy.quiet_hours_end.hour, minute=policy.quiet_hours_end.minute, second=0, microsecond=0,
    )
    if opens <= local:
        opens += timedelta(days=1)

    return min(max((opens - local).total_seconds(), 1.0), MAX_QUIET_HOLD_SECONDS)


# ---------------------------------------------------------------------------
# The opt-out affordance on outgoing campaign messages
# ---------------------------------------------------------------------------

def _has_opt_out(body, caption, buttons, footer) -> bool:
    if any((button or {}).get("id") == OPT_OUT_BUTTON_ID for button in buttons or []):
        return True
    needle = (footer or "").strip().casefold()
    return bool(needle) and needle in f"{body or ''}\n{caption or ''}".casefold()


def apply_opt_out_affordance(policy, *, body, caption, buttons, supports_buttons) -> dict:
    """
    Add a way out to one outgoing message.

    A tenant that has asked for the button style gets a tappable button
    wherever one fits, because a button is honoured exactly while a typed
    reply is only honoured as well as the keyword list reads it. It doesn't
    always fit: WhatsApp caps reply buttons at three, an interactive message
    needs body text to hang the buttons under, and not every connection can
    send one at all. Every one of those cases falls back to the footer line
    rather than silently shipping a campaign with no opt-out on it.

    The subtle one is a message whose only button is a link. Reply and link
    buttons can now sit in the same list, but only one kind survives as a
    button on the wire (see ``core.buttons.plan_delivery``) and reply buttons
    win -- so adding an unsubscribe button here would quietly demote the
    author's link to a line of text. The footer is used instead: it costs the
    tap on the opt-out, not the tap on the message's own call to action.

    Returns the fields to apply, so the caller stores exactly what will be
    sent -- the message row is the record of what the contact was offered.
    """
    buttons = list(buttons or [])
    if _has_opt_out(body, caption, buttons, policy.opt_out_footer):
        return {"body": body, "caption": caption, "buttons": buttons}

    replies = [button for button in buttons if (button or {}).get("type", REPLY) == REPLY]
    has_tappable_link = not replies and any((button or {}).get("type") == URL for button in buttons)

    can_add_button = (
        policy.opt_out_style == CompliancePolicy.OptOutStyle.BUTTON
        and supports_buttons
        and (body or "").strip()
        and not has_tappable_link
        and len(replies) < MAX_REPLY_BUTTONS
    )
    if can_add_button:
        # Placed after the last reply button rather than at the end of the
        # list, so the numbered fallback reads "1. Yes / 2. No / 3. Unsubscribe"
        # with the link line beneath, instead of the opt-out trailing after it.
        position = max(
            (index for index, button in enumerate(buttons)
             if (button or {}).get("type", REPLY) == REPLY),
            default=-1,
        ) + 1
        buttons.insert(position, {
            "type": REPLY,
            "id": OPT_OUT_BUTTON_ID,
            "title": (policy.opt_out_button_title or "Unsubscribe")[:20],
            "url": "",
            "phone": "",
        })
        return {"body": body, "caption": caption, "buttons": buttons}

    footer = (policy.opt_out_footer or "").strip()
    if not footer:
        return {"body": body, "caption": caption, "buttons": buttons}

    # Attach to whichever text the contact will actually read: a media-only
    # message has no body, and appending to an empty one would send the footer
    # as a second bubble of its own.
    if (body or "").strip():
        body = f"{body.rstrip()}\n\n{footer}"
    elif (caption or "").strip():
        caption = f"{caption.rstrip()}\n\n{footer}"
    else:
        body = footer

    return {"body": body, "caption": caption, "buttons": buttons}
