"""
Smart segments: named, always-current answers to the questions a seller
actually asks.

These are not the same thing as `contacts.ContactSegment`, and neither
replaces the other. A ContactSegment is a filter a user built and saved
("Country = IN and Tag = Wholesale") -- arbitrary, personal, stored. A smart
segment is one of a fixed set of behavioural questions that every business
has ("who went quiet on me", "who is hot right now") and that nobody should
have to build a filter for.

They are computed, never stored: each is a queryset expression evaluated at
the moment it is asked, so a contact joins and leaves a segment the instant
their behaviour changes and there is no membership table to keep in sync.

Everything here is deterministic SQL. No model decides who is hot.
"""

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from apps.contacts.models import Contact

from .scoring import HOT_THRESHOLD, WARM_THRESHOLD


def _base(tenant):
    return Contact.objects.filter(tenant=tenant)


def _no_reply_since(tenant, days):
    """
    We spoke, `days` have passed, and they have not answered *that* message.

    The second half is what makes this correct rather than merely plausible:
    comparing `last_inbound_at` against `last_outbound_at` rather than against
    a clock means someone who replied yesterday to a message we sent a week
    ago is not in the list.
    """
    cutoff = timezone.now() - timedelta(days=days)
    return _base(tenant).filter(
        subscribed=True,
        last_outbound_at__isnull=False,
        last_outbound_at__lte=cutoff,
    ).filter(Q(last_inbound_at__isnull=True) | Q(last_inbound_at__lt=F("last_outbound_at")))


def _campaign_responders(tenant, days=30):
    from apps.messaging.models import Message

    since = timezone.now() - timedelta(days=days)
    campaign_send = Message.objects.filter(
        contact_id=OuterRef("pk"),
        campaign_id__isnull=False,
        direction=Message.Direction.OUTBOUND,
        queued_at__gte=since,
    )
    return (
        _base(tenant)
        .filter(last_inbound_at__isnull=False, last_inbound_at__gte=since)
        .filter(Exists(campaign_send))
    )


def _highly_engaged(tenant, days=30, minimum=5):
    from apps.messaging.models import Message

    since = timezone.now() - timedelta(days=days)
    return (
        _base(tenant)
        .annotate(
            recent_inbound=Count(
                "messages",
                filter=Q(messages__direction=Message.Direction.INBOUND, messages__queued_at__gte=since),
                distinct=True,
            )
        )
        .filter(recent_inbound__gte=minimum)
    )


def _follow_up_due(tenant):
    from .models import Task

    due = Task.objects.filter(contact_id=OuterRef("pk"), status=Task.Status.OPEN, due_at__lte=timezone.now())
    return _base(tenant).filter(Exists(due))


def _with_open_deals(tenant):
    from .models import Deal

    open_deal = Deal.objects.filter(contact_id=OuterRef("pk"), status=Deal.Status.OPEN)
    return _base(tenant).filter(Exists(open_deal))


def _inactive(tenant, days=60):
    cutoff = timezone.now() - timedelta(days=days)
    return _base(tenant).filter(
        Q(lifecycle_status=Contact.Lifecycle.INACTIVE)
        | Q(last_inbound_at__lt=cutoff, last_outbound_at__lt=cutoff)
    )


#: key -> (label, one-line description, resolver, tone)
#:
#: Order matters: this is the order the chips appear in the contact list, and
#: it runs from "act on this now" to "clean this up later".
SMART_SEGMENTS = {
    "follow_up_due": (
        "Follow-up due", "A task on this contact is due now or overdue.",
        _follow_up_due, "red",
    ),
    "hot_leads": (
        "Hot leads", "Replying recently and often, or with a deal open.",
        lambda tenant: _base(tenant).filter(subscribed=True, lead_score__gte=HOT_THRESHOLD), "red",
    ),
    "no_reply_3d": (
        "No reply for 3 days", "We messaged them, they have not answered in 3 days.",
        lambda tenant: _no_reply_since(tenant, 3), "amber",
    ),
    "no_reply_7d": (
        "No reply for 7 days", "Going cold: a week of silence since our last message.",
        lambda tenant: _no_reply_since(tenant, 7), "amber",
    ),
    "new_leads": (
        "New leads", "Added in the last 30 days and not yet engaged.",
        lambda tenant: _base(tenant).filter(
            lifecycle_status=Contact.Lifecycle.NEW,
            created_at__gte=timezone.now() - timedelta(days=30),
        ), "blue",
    ),
    "warm_leads": (
        "Warm leads", "Some engagement, not enough to call hot.",
        lambda tenant: _base(tenant).filter(
            subscribed=True, lead_score__gte=WARM_THRESHOLD, lead_score__lt=HOT_THRESHOLD,
        ), "amber",
    ),
    "campaign_responders": (
        "Campaign responders", "Replied after getting a campaign in the last 30 days.",
        _campaign_responders, "green",
    ),
    "highly_engaged": (
        "Highly engaged", "Five or more messages from them in the last 30 days.",
        _highly_engaged, "green",
    ),
    "open_deals": (
        "With open deals", "Money on the table right now.",
        _with_open_deals, "violet",
    ),
    "customers": (
        "Customers", "Someone who has bought.",
        lambda tenant: _base(tenant).filter(lifecycle_status=Contact.Lifecycle.CUSTOMER), "green",
    ),
    "unassigned": (
        "Unassigned", "Nobody owns this relationship yet.",
        lambda tenant: _base(tenant).filter(owner__isnull=True).exclude(
            lifecycle_status=Contact.Lifecycle.INACTIVE,
        ), "gray",
    ),
    "inactive": (
        "Inactive", "No contact either way for 60 days.",
        _inactive, "gray",
    ),
}


def resolve(tenant, key):
    """The queryset for one smart segment, or None if the key is unknown."""
    entry = SMART_SEGMENTS.get(key)
    if entry is None:
        return None
    return entry[2](tenant).distinct()


def catalog(tenant, *, with_counts=True) -> list:
    """
    Every smart segment with its live size.

    Counted behind a short cache: this is roughly a dozen COUNT queries and it
    is asked on every contact-list load, but the answers change slowly enough
    that a two-minute-old number is never misleading. Falls back to counting
    directly if the cache is unavailable -- it is configured to fail open.
    """
    cache_key = f"crm:segment-counts:{tenant.pk}"
    counts = cache.get(cache_key) if with_counts else {}
    if with_counts and counts is None:
        counts = {key: resolve(tenant, key).count() for key in SMART_SEGMENTS}
        cache.set(cache_key, counts, timeout=120)

    return [
        {
            "key": key,
            "label": label,
            "description": description,
            "tone": tone,
            "count": (counts or {}).get(key),
        }
        for key, (label, description, _resolver, tone) in SMART_SEGMENTS.items()
    ]


def invalidate_counts(tenant_id) -> None:
    try:
        cache.delete(f"crm:segment-counts:{tenant_id}")
    except Exception:  # noqa: BLE001 - cache is configured to fail open
        pass
