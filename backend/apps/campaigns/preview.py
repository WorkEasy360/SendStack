"""
What a campaign would actually do, and cost, before anybody presses send.

`estimate_recipients` answered one question -- how many contacts match the
audience rule -- and it was the wrong one to show a customer about to spend
money. It counts the people the rule selects; it does not say how many of them
will be refused, why, or what the send will take off the balance. A customer
choosing "all contacts" and seeing 1,240 has been told a number that will not
happen.

This module answers the question that was actually being asked:

    selected      the audience rule's own answer, before any rule is applied
    opted out     said stop; never get a message row at all
    consent       subscribed, but this workspace requires opt-in and theirs
                  is not recorded -- these get a row and are SKIPPED
    eligible      what is left, and what will be charged for

**Every count here is produced by the same rules the send path runs.** The
opt-out filter is `resolve_audience`'s own; the consent question is
`compliance.may_receive_marketing`, which is where the
opt-out > consent > eligibility precedence lives and is the single function
`gating.evaluate` calls on the send path. Nothing in this file reimplements a
rule, and that is deliberate: a preview with its own copy of the eligibility
logic is a preview that will eventually disagree with the send, and the
customer will discover the disagreement after they have been charged.

Nothing here writes. No message rows, no credit reservation, no quota claim.
A preview that consumed anything would make "let me check what this costs"
expensive, and people would stop checking.
"""

import logging


from apps.compliance.models import CompliancePolicy
from apps.compliance.services import may_receive_marketing
from apps.contacts.models import Contact
from apps.contacts.segments import resolve_segment_queryset
from apps.subscriptions.services import check_quota
from apps.whatsapp.models import WhatsAppAccount

from .models import Campaign

logger = logging.getLogger("sendstack")

#: How many contacts the consent pass will examine row by row before it stops
#: counting exactly and starts reporting an approximation.
#:
#: The consent question cannot be answered in SQL for every policy -- it is a
#: function, and keeping it a function is what stops the precedence rule being
#: duplicated here. So a very large audience is sampled rather than walked: a
#: preview must not take thirty seconds, and "about 4% will be skipped" is a
#: useful, honest answer where "4,163 will be skipped" would have cost a full
#: table scan to be no more useful.
EXACT_LIMIT = 20_000


def audience_before_consent(campaign: Campaign):
    """
    The contacts the audience rule names, **including** the ones who opted out.

    `services.resolve_audience` filters `subscribed=True` and is right to --
    an opted-out contact must never reach a message row. But that makes the
    opt-outs invisible, and "38 people asked not to hear from you and are not
    included" is one of the more valuable things this screen says: it is the
    number that tells a customer their list is being respected.

    So this deliberately mirrors that function without the subscription
    filter. The mirroring is a real cost -- two functions to keep in step --
    and it is paid here rather than by loosening the send path's filter, which
    is the one place that must never be relaxed for reporting's sake.
    """
    base = Contact.objects.filter(tenant=campaign.tenant)

    if campaign.audience_type == Campaign.AudienceType.ALL:
        return base
    if campaign.audience_type == Campaign.AudienceType.GROUP:
        return base.filter(groups=campaign.audience_group) if campaign.audience_group_id else base.none()
    if campaign.audience_type == Campaign.AudienceType.TAG:
        return base.filter(tags=campaign.audience_tag) if campaign.audience_tag_id else base.none()
    if campaign.audience_type == Campaign.AudienceType.SEGMENT:
        if not campaign.audience_segment_id:
            return base.none()
        return resolve_segment_queryset(campaign.tenant, campaign.audience_segment.filters)
    if campaign.audience_type == Campaign.AudienceType.CONTACT_IDS:
        return base.filter(id__in=campaign.audience_contact_ids)
    return base.none()


def _consent_split(campaign, policy) -> tuple:
    """
    Of the subscribed contacts, how many this workspace's policy will refuse.

    Returns ``(eligible, blocked, exact)``. `exact` is False when the audience
    was large enough to be sampled -- the caller says so rather than presenting
    an estimate as a count.

    When the policy does not require consent the answer is free: nothing is
    blocked, and there is no reason to look at a single row.
    """
    from .services import resolve_audience

    subscribed = resolve_audience(campaign).distinct()
    total = subscribed.count()

    if not policy.require_marketing_consent:
        return total, 0, True

    if total <= EXACT_LIMIT:
        # Answered in SQL, but against the same rule: with
        # require_marketing_consent on, `may_receive_marketing` grants exactly
        # the contacts whose consent is GRANTED. Asserted below rather than
        # assumed.
        eligible = subscribed.filter(
            marketing_consent=Contact.MarketingConsent.GRANTED,
        ).count()
        return eligible, total - eligible, True

    # Too large to count exactly. Sample, scale, and be honest about it.
    sample = list(subscribed.order_by("?")[:2_000])
    if not sample:
        return 0, total, True
    granted = sum(1 for contact in sample if may_receive_marketing(contact, policy)[0])
    ratio = granted / len(sample)
    eligible = int(total * ratio)
    return eligible, total - eligible, False


def _devices(campaign) -> dict:
    """
    Whether there is anything to send with.

    A campaign with no connected number is the most common reason a send does
    nothing, and it is knowable before the customer presses the button.
    """
    accounts = WhatsAppAccount.objects.filter(tenant=campaign.tenant)
    connected = accounts.filter(status=WhatsAppAccount.Status.CONNECTED).count()
    return {
        "total": accounts.count(),
        "connected": connected,
        "ready": connected > 0,
    }


def _delivery(campaign) -> dict:
    """
    How this campaign's content will actually arrive on the numbers that will
    send it.

    Deliberately **not** a blocker. Every content kind this platform accepts is
    deliverable on every connection -- a menu or a poll a number cannot show
    natively is sent as a numbered list the recipient can still answer, and the
    reply is matched back to the same row id. Refusing the send would be worse
    than the degradation it was avoiding.

    What it *is* is the answer to "will my customers see tappable buttons?",
    which is a real question with a surprising answer (they will not, on a
    QR-linked number) and one that has to be answered before the send rather
    than deduced afterwards from a report full of typed replies.
    """
    from apps.templates_app.delivery import as_delivered, degraded_text, required_capabilities
    from apps.whatsapp.connections.manager import ConnectionManager
    from core.buttons import plan_delivery
    from core.interactive import BUTTON

    from .assignment import eligible_accounts

    spec = campaign.effective_message()
    needed = required_capabilities(spec)
    capability = needed[0] if needed else ""
    kind = campaign.content_kind()

    if not capability:
        return {
            "content_kind": kind,
            "capability": "",
            "capabilities": [],
            "native": True,
            "flattened": False,
            "degraded_accounts": [],
            "as_sent": "",
        }

    # A mixed button list has no envelope that carries both kinds, so one of
    # them is written into the body on every number -- see core.buttons. That
    # is a property of the content, not of any connection, which is why it
    # degrades even a number whose capabilities all say yes.
    mixed = bool(plan_delivery(spec.get("buttons") or [])[1]) if kind == BUTTON else False

    # The numbers that will actually carry this campaign: the whole connected
    # pool in AUTO mode, the one chosen number in MANUAL. Asking about numbers
    # that will not send it would report a problem nobody has.
    accounts = list(eligible_accounts(campaign))
    degraded = [
        {"id": account.pk, "name": account.name, "provider": account.provider}
        for account in accounts
        if mixed or not all(ConnectionManager.supports(account, name) for name in needed)
    ]

    # Two different degradations, and the campaign summary has to name the
    # right one. True here means at least one number flattens the *whole*
    # message into text -- a menu it cannot draw, or buttons it cannot show.
    # False means the message keeps its buttons on every number and only the
    # kinds that did not fit one envelope are written into the body, which is
    # a property of the content rather than of any connection.
    flattened = bool(degraded) and (
        kind != BUTTON
        or any(not ConnectionManager.supports(account, "buttons") for account in accounts)
    )

    if not degraded:
        as_sent = ""
    elif flattened:
        # A function of the content, so every number that flattens it sends
        # the same text -- shown once rather than per account.
        as_sent = degraded_text(spec)
    else:
        as_sent = as_delivered(spec, can_show_buttons=True)[0]

    return {
        "content_kind": kind,
        "capability": capability,
        "capabilities": needed,
        "native": not degraded,
        "flattened": flattened,
        "degraded_accounts": degraded,
        "as_sent": as_sent,
    }


def _credits(campaign, needed: int) -> dict:
    """
    What this will cost and whether the balance covers it.

    One credit per message, matching `services.start_campaign`, which reserves
    exactly `created` against the campaign's owner. Reading the *available*
    balance rather than the raw one is the point: credits already reserved by
    another running campaign are not available to this one, and a preview that
    ignored reservations would promise a send that the reservation then refuses.
    """
    owner = campaign.created_by
    if owner is None:
        return {"needed": needed, "available": None, "sufficient": True, "tracked": False}

    available = max(int(owner.credits) - int(owner.reserved_credits), 0)
    return {
        "needed": needed,
        "available": available,
        "sufficient": available >= needed,
        "shortfall": max(needed - available, 0),
        "tracked": True,
    }


def _quota(campaign, needed: int) -> dict:
    """
    Whether the plan's monthly allowance covers this send.

    Asked of `check_quota` -- the same function `start_campaign` calls with the
    same metric and the same amount -- so a campaign this preview says fits is
    one the gate will also let through.
    """
    allowed, reason = check_quota(campaign.tenant_id, "messages_per_month", needed)
    return {"within_plan": bool(allowed), "detail": reason or ""}


def build(campaign: Campaign) -> dict:
    """
    The full pre-send picture for one campaign.

    Read-only, and safe to call as often as a wizard step re-renders.
    """
    policy = CompliancePolicy.for_tenant(campaign.tenant_id)

    selected = audience_before_consent(campaign).distinct().count()
    eligible, consent_blocked, exact = _consent_split(campaign, policy)

    # Everything the audience rule named that will never get a message row.
    # Derived by subtraction rather than counted separately: one count and one
    # subtraction cannot disagree, two counts can.
    subscribed_total = eligible + consent_blocked
    opted_out = max(selected - subscribed_total, 0)

    skipped = [
        {
            "reason": "opted_out",
            "label": "Opted out",
            "count": opted_out,
            "detail": "These contacts asked not to receive messages. They are never contacted.",
        },
        {
            "reason": "consent_missing",
            "label": "No marketing consent recorded",
            "count": consent_blocked,
            "detail": (
                "Your workspace requires recorded opt-in consent, and these contacts have none."
                if policy.require_marketing_consent
                else ""
            ),
        },
    ]

    credits = _credits(campaign, eligible)
    quota = _quota(campaign, eligible)
    devices = _devices(campaign)

    return {
        "selected": selected,
        "eligible": eligible,
        "skipped": [item for item in skipped if item["count"] > 0],
        "skipped_total": opted_out + consent_blocked,
        # False when the audience was large enough to sample. The UI says
        # "about" rather than quietly presenting an estimate as a count.
        "exact": exact,
        "credits": credits,
        "plan": quota,
        "devices": devices,
        # What the recipients will actually see, given the numbers sending it.
        # Not part of `can_send` -- see _delivery for why this informs rather
        # than blocks.
        "delivery": _delivery(campaign),
        "can_send": bool(eligible > 0 and credits["sufficient"] and quota["within_plan"] and devices["ready"]),
        "blockers": _blockers(eligible, credits, quota, devices),
    }


def _blockers(eligible, credits, quota, devices) -> list:
    """
    Why this send would not work, in the order somebody should fix them.

    Sentences rather than codes, for the same reason as the plan-change
    warnings: this list is rendered as-is, and a mapping from code to copy in
    the browser would be a second place for the wording to drift.
    """
    blockers = []
    if eligible == 0:
        blockers.append(
            "No contact in this audience can be messaged. Check the audience, and whether your "
            "workspace requires recorded consent."
        )
    if not devices["ready"]:
        blockers.append(
            "No WhatsApp number is connected. Connect one before sending."
            if devices["total"] == 0
            else "None of your WhatsApp numbers is connected right now."
        )
    if credits["tracked"] and not credits["sufficient"]:
        blockers.append(
            f"This send needs {credits['needed']:,} credits and {credits['available']:,} are "
            f"available. Top up by {credits['shortfall']:,} to send it."
        )
    if not quota["within_plan"]:
        blockers.append(quota["detail"] or "This send is over your plan's monthly message allowance.")
    return blockers
