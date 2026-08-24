"""
Turning "hot leads who haven't replied in 3 days" into a queryset.

This is pattern matching, not language understanding, and that is a deliberate
choice rather than a shortcut. The set of things a user can usefully ask about
a contact list is small and closed -- a band, a status, an owner, a silence, a
date window, a name -- so a table of phrases covers it exactly, answers in a
millisecond, costs nothing per query, works offline, and gives the same answer
twice. An LLM here would be slower, billable, and occasionally wrong about
whose leads it was showing.

What it cannot parse it hands back as plain text for the ordinary name/phone/
email/company search, so a query is never rejected -- at worst it degrades to
the search box the user already had. The response reports which phrases were
understood (`applied`) so the UI can show its work instead of silently
filtering something away.
"""

import re
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone

from apps.contacts.models import Contact

from .scoring import HOT_THRESHOLD, WARM_THRESHOLD

#: Phrase -> (label shown back to the user, Q object or callable(user) -> Q).
#: Longest phrases are matched first (see `parse`) so "no reply" inside
#: "no reply in 7 days" does not win over the more specific rule.
_PHRASES = {
    "hot leads": ("Hot leads", Q(lead_score__gte=HOT_THRESHOLD, subscribed=True)),
    "hot lead": ("Hot leads", Q(lead_score__gte=HOT_THRESHOLD, subscribed=True)),
    "warm leads": ("Warm leads", Q(lead_score__gte=WARM_THRESHOLD, lead_score__lt=HOT_THRESHOLD)),
    "cold leads": ("Cold leads", Q(lead_score__lt=WARM_THRESHOLD)),
    "new leads": ("New leads", Q(lifecycle_status=Contact.Lifecycle.NEW)),
    "new lead": ("New leads", Q(lifecycle_status=Contact.Lifecycle.NEW)),
    "customers": ("Customers", Q(lifecycle_status=Contact.Lifecycle.CUSTOMER)),
    "customer": ("Customers", Q(lifecycle_status=Contact.Lifecycle.CUSTOMER)),
    "qualified": ("Qualified", Q(lifecycle_status=Contact.Lifecycle.QUALIFIED)),
    "engaged": ("Engaged", Q(lifecycle_status=Contact.Lifecycle.ENGAGED)),
    "inactive": ("Inactive", Q(lifecycle_status=Contact.Lifecycle.INACTIVE)),
    "lost": ("Lost", Q(lifecycle_status=Contact.Lifecycle.LOST)),
    "unassigned": ("Unassigned", Q(owner__isnull=True)),
    "no owner": ("Unassigned", Q(owner__isnull=True)),
    "unsubscribed": ("Unsubscribed", Q(subscribed=False)),
    "subscribed": ("Subscribed", Q(subscribed=True)),
    "with deals": ("With open deals", Q(deals__status="OPEN")),
    "with a deal": ("With open deals", Q(deals__status="OPEN")),
    "open deals": ("With open deals", Q(deals__status="OPEN")),
    "won deals": ("Won a deal", Q(deals__status="WON")),
}

#: Phrases that need the requesting user, so they cannot be plain Q objects.
_USER_PHRASES = {
    "my leads": "Owned by me",
    "my contacts": "Owned by me",
    "mine": "Owned by me",
    "assigned to me": "Owned by me",
}

#: "no reply in 3 days", "haven't replied for 2 weeks", "not replied in a month"
_NO_REPLY_RE = re.compile(
    r"(?:no(?:t)?\s+repl(?:y|ied)|haven'?t\s+replied|hasn'?t\s+replied|no\s+response|silent)"
    # Longest unit first: regex alternation is ordered, so `day|days` would
    # match "day" inside "days" and leave a stray "s" behind for the text search.
    r"(?:\s+(?:in|for|since))?\s+(?:the\s+)?(\d+|a|an)\s*(days|day|weeks|week|months|month)",
)

#: "added this week", "created in the last 7 days"
_ADDED_RE = re.compile(
    r"(?:added|created|new)\s+(?:in\s+the\s+)?(?:last\s+)?(\d+|a|an|this)\s*(days|day|weeks|week|months|month)",
)

_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30}


def _amount(token: str) -> int:
    if token in ("a", "an", "this"):
        return 1
    try:
        return max(1, min(int(token), 3650))
    except ValueError:
        return 1


def parse(query: str, *, user=None) -> dict:
    """
    Split `query` into structured filters plus whatever text is left over.

    Returns ``{"filters": [Q, ...], "applied": [label, ...], "text": str}``.
    Every branch is additive (AND), which is how people read these: "hot leads
    who haven't replied in 3 days" means both, never either.
    """
    remaining = f" {(query or '').lower().strip()} "
    filters = []
    applied = []

    def consume(match_text, label, q):
        nonlocal remaining
        remaining = remaining.replace(match_text, " ", 1)
        filters.append(q)
        applied.append(label)

    # Duration patterns first: they contain words ("no reply") that the plain
    # phrase table would otherwise strip out from under them.
    match = _NO_REPLY_RE.search(remaining)
    if match:
        days = _amount(match.group(1)) * _UNIT_DAYS.get(match.group(2), 1)
        cutoff = timezone.now() - timedelta(days=days)
        consume(
            match.group(0),
            f"No reply in {days} days",
            Q(last_outbound_at__isnull=False, last_outbound_at__lte=cutoff)
            & (Q(last_inbound_at__isnull=True) | Q(last_inbound_at__lt=F("last_outbound_at"))),
        )

    match = _ADDED_RE.search(remaining)
    if match:
        days = _amount(match.group(1)) * _UNIT_DAYS.get(match.group(2), 1)
        consume(
            match.group(0),
            f"Added in the last {days} days",
            Q(created_at__gte=timezone.now() - timedelta(days=days)),
        )

    if user is not None and getattr(user, "pk", None):
        for phrase, label in sorted(_USER_PHRASES.items(), key=lambda item: -len(item[0])):
            if f" {phrase} " in remaining:
                consume(f" {phrase} ", label, Q(owner_id=user.pk))
                break

    for phrase, (label, q) in sorted(_PHRASES.items(), key=lambda item: -len(item[0])):
        if f" {phrase} " in remaining:
            consume(f" {phrase} ", label, q)

    # Connective words left behind by the phrases above are noise, not a name.
    leftover = " ".join(
        word for word in remaining.split()
        if word not in {"who", "that", "which", "and", "with", "have", "has", "in", "for", "the", "a", "an", "show", "me", "all"}
    ).strip()

    return {"filters": filters, "applied": applied, "text": leftover}


def apply(queryset, query: str, *, user=None) -> tuple:
    """Apply `parse`'s result to a contact queryset. Returns (queryset, applied)."""
    parsed = parse(query, user=user)
    for q in parsed["filters"]:
        queryset = queryset.filter(q)
    if parsed["text"]:
        text = parsed["text"]
        queryset = queryset.filter(
            Q(name__icontains=text)
            | Q(phone__icontains=text)
            | Q(email__icontains=text)
            | Q(company__icontains=text)
        )
    # A join through deals or tags can multiply rows; the caller counts these.
    return queryset.distinct(), parsed["applied"]
