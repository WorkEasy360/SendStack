"""
Lead scoring: 0-100 from behaviour, computed with plain arithmetic.

No model, no API, no training data. A score a user cannot explain is a score
they will not trust, and the whole value of "this lead is hot" is that the
seller believes it enough to call. Every point below traces to one sentence
you could say out loud: *they answered recently*, *they answer a lot*, *they
have money on the table*.

The band (HOT/WARM/COLD) is the only form of this a user is shown. The number
exists so the list can be sorted, and so a band boundary can move later
without every stored value becoming wrong.
"""

from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

HOT_THRESHOLD = 60
WARM_THRESHOLD = 30

#: Points for how recently they last replied. Recency dominates on purpose --
#: a lead who answered this morning is worth calling today whatever their
#: history, and one who has said nothing in a month is not, however chatty
#: they once were.
_RECENCY_BANDS = ((1, 35), (3, 28), (7, 18), (14, 10), (30, 4))

#: Points per inbound message, capped. Someone who has sent 40 messages is not
#: four times the lead of someone who sent 10; they are the same lead having a
#: longer conversation.
_PER_INBOUND = 2
_MAX_INBOUND_POINTS = 20

_MAX_RESPONSIVENESS_POINTS = 15
_OPEN_DEAL_POINTS = 20
_WON_DEAL_POINTS = 10


def band_for(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "HOT"
    if score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def _recency_points(last_inbound_at, now) -> int:
    if not last_inbound_at:
        return 0
    days = (now - last_inbound_at).total_seconds() / 86400
    for limit, points in _RECENCY_BANDS:
        if days <= limit:
            return points
    return 0


def score_from_signals(*, last_inbound_at, inbound_count, outbound_count, open_deals, won_deals,
                       subscribed=True, now=None) -> int:
    """
    The whole scoring rule, as a pure function.

    Separated from the queries that gather the signals so it can be reasoned
    about (and tested) without a database, and so the single-contact path and
    the nightly bulk sweep cannot drift into scoring things differently.
    """
    # Someone who asked us to stop is not a lead, whatever they did before.
    # Leaving them merely "cold" would keep them in a sorted list a seller
    # works down, which is exactly where they must not be.
    if not subscribed:
        return 0

    now = now or timezone.now()
    score = _recency_points(last_inbound_at, now)
    score += min(inbound_count * _PER_INBOUND, _MAX_INBOUND_POINTS)

    # Answering most of what we send is the clearest signal of interest there
    # is, but it only means something once we have actually sent a few things:
    # one message answered once is a 100% reply rate and tells us nothing.
    if outbound_count >= 3:
        ratio = min(inbound_count / outbound_count, 1.0)
        score += int(round(ratio * _MAX_RESPONSIVENESS_POINTS))

    if open_deals:
        score += _OPEN_DEAL_POINTS
    if won_deals:
        score += _WON_DEAL_POINTS

    return max(0, min(100, score))


def _signal_annotations(since):
    """The aggregate expressions both the single and bulk paths score from."""
    from apps.messaging.models import Message

    return {
        "sig_last_inbound": Max("messages__queued_at", filter=Q(messages__direction=Message.Direction.INBOUND)),
        "sig_inbound": Count(
            "messages",
            filter=Q(messages__direction=Message.Direction.INBOUND, messages__queued_at__gte=since),
            distinct=True,
        ),
        "sig_outbound": Count(
            "messages",
            filter=Q(messages__direction=Message.Direction.OUTBOUND, messages__queued_at__gte=since),
            distinct=True,
        ),
        "sig_open_deals": Count("deals", filter=Q(deals__status="OPEN"), distinct=True),
        "sig_won_deals": Count("deals", filter=Q(deals__status="WON"), distinct=True),
    }


#: Only behaviour inside this window counts toward volume and responsiveness.
#: Without it a contact who was busy last year keeps a high score forever.
WINDOW_DAYS = 90


def score_contact(contact, *, save=True) -> int:
    """Recompute one contact's score. One aggregate query."""
    from apps.contacts.models import Contact

    now = timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    row = (
        Contact.objects.filter(pk=contact.pk)
        .annotate(**_signal_annotations(since))
        .values("sig_last_inbound", "sig_inbound", "sig_outbound", "sig_open_deals", "sig_won_deals")
        .first()
    )
    if row is None:
        return contact.lead_score

    score = score_from_signals(
        last_inbound_at=row["sig_last_inbound"],
        inbound_count=row["sig_inbound"],
        outbound_count=row["sig_outbound"],
        open_deals=row["sig_open_deals"],
        won_deals=row["sig_won_deals"],
        subscribed=contact.subscribed,
        now=now,
    )
    if save and score != contact.lead_score:
        contact.lead_score = score
        contact.lead_score_updated_at = now
        contact.save(update_fields=["lead_score", "lead_score_updated_at"])
    elif save:
        # Still stamp the timestamp, so the sweep can tell "unchanged" apart
        # from "never looked at".
        contact.lead_score_updated_at = now
        contact.save(update_fields=["lead_score_updated_at"])
    return score


def rescore_tenant(tenant, *, batch_size=500) -> int:
    """
    Rescore every contact in one tenant, in batches.

    Run nightly rather than on every message: recency decay means a score goes
    stale on its own with no event to hang a recompute on, and a contact who
    went quiet three days ago has to cool down without anyone touching their
    row. Batched with bulk_update so a 100k-contact tenant is a few hundred
    statements instead of 100k saves.
    """
    from apps.contacts.models import Contact

    now = timezone.now()
    since = now - timedelta(days=WINDOW_DAYS)
    queryset = (
        Contact.objects.filter(tenant=tenant)
        .annotate(**_signal_annotations(since))
        .only("id", "lead_score", "subscribed")
        .order_by("pk")
    )

    updated = 0
    batch = []
    for contact in queryset.iterator(chunk_size=batch_size):
        score = score_from_signals(
            last_inbound_at=contact.sig_last_inbound,
            inbound_count=contact.sig_inbound,
            outbound_count=contact.sig_outbound,
            open_deals=contact.sig_open_deals,
            won_deals=contact.sig_won_deals,
            subscribed=contact.subscribed,
            now=now,
        )
        if score == contact.lead_score:
            continue
        contact.lead_score = score
        contact.lead_score_updated_at = now
        batch.append(contact)
        if len(batch) >= batch_size:
            Contact.objects.bulk_update(batch, ["lead_score", "lead_score_updated_at"])
            updated += len(batch)
            batch = []

    if batch:
        Contact.objects.bulk_update(batch, ["lead_score", "lead_score_updated_at"])
        updated += len(batch)
    return updated
