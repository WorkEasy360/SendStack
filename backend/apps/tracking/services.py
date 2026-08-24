"""
Link shortening and click attribution.

Every URL in an outbound message is replaced with a per-recipient short link
so that "delivered" and "read" can become "clicked". The rewrite happens where
the message is *built*, not where it is sent, for the same reason the opt-out
footer does: the stored row has to be a faithful record of what the contact
was actually shown.
"""

import logging
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from django.utils.text import slugify

from .models import LinkClick, TrackedLink

logger = logging.getLogger("multisender")

# Deliberately conservative: a URL ends at the first whitespace or angle
# bracket, and common sentence punctuation is trimmed off the end afterwards.
# Over-matching is the dangerous direction -- swallowing the full stop after a
# link turns a working URL into a 404 for every recipient.
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""

_CODE_BYTES = 9  # -> a 12-character token; 64**12 possibilities, so codes are not guessable


def _public_base_url() -> str:
    base = getattr(settings, "LINK_TRACKING_BASE_URL", "") or getattr(settings, "MEDIA_PUBLIC_BASE_URL", "")
    return (base or "").rstrip("/")


def tracking_enabled() -> bool:
    """
    Whether shortening is possible at all in this deployment.

    Without a public base URL there is nowhere for the short link to point,
    and rewriting would replace a working URL with a broken one. Silently
    leaving the original in place is the only safe answer.
    """
    return bool(getattr(settings, "LINK_TRACKING_ENABLED", True)) and bool(_public_base_url())


def short_url(code: str) -> str:
    return f"{_public_base_url()}/r/{code}"


def _split_trailing_punctuation(url: str) -> tuple[str, str]:
    trimmed = url
    suffix = ""
    while trimmed and trimmed[-1] in _TRAILING_PUNCTUATION:
        # A closing bracket is only punctuation if it is unbalanced -- plenty
        # of real URLs (Wikipedia, in particular) end in one.
        if trimmed[-1] == ")" and trimmed.count("(") >= trimmed.count(")"):
            break
        suffix = trimmed[-1] + suffix
        trimmed = trimmed[:-1]
    return trimmed, suffix


def _merge_utm(url: str, utm: dict) -> str:
    """
    Add UTM parameters, without overwriting any the tenant set themselves.

    Someone who has written `?utm_source=newsletter` into their own link meant
    it, and quietly replacing it would misattribute their traffic in their own
    analytics -- a worse outcome than not tagging at all.
    """
    if not utm:
        return url

    parts = urlsplit(url)
    existing = parse_qsl(parts.query, keep_blank_values=True)
    present = {key for key, _ in existing}
    added = [(key, value) for key, value in utm.items() if value and key not in present]
    if not added:
        return url
    return urlunsplit(parts._replace(query=urlencode(existing + added)))


def default_utm(medium: str, campaign_name: str = "") -> dict:
    """UTM tags for one send, from deployment defaults plus what this send is."""
    tags = {
        "utm_source": getattr(settings, "LINK_TRACKING_UTM_SOURCE", "whatsapp"),
        "utm_medium": medium,
    }
    if campaign_name:
        tags["utm_campaign"] = campaign_name
    return tags


def campaign_utm(campaign) -> dict:
    return default_utm(
        getattr(settings, "LINK_TRACKING_UTM_MEDIUM", "broadcast"),
        slugify(campaign.name)[:100] or f"campaign-{campaign.pk}",
    )


def rewrite(text: str, *, tenant_id: int, campaign=None, contact=None, utm=None) -> tuple[str, list]:
    """
    Replace every URL in `text` with a short link for this recipient.

    Returns the rewritten text and the *unsaved* TrackedLink rows it needs.
    They are handed back rather than saved here so that a campaign building
    thousands of messages can insert them in one statement, and so that a
    caller that ends up not sending the message writes no rows at all.
    """
    if not text or not tracking_enabled():
        return text, []

    base = _public_base_url()
    links: list[TrackedLink] = []

    def replace(match):
        url, suffix = _split_trailing_punctuation(match.group(0))
        # Never shorten our own short links: a message forwarded back into a
        # campaign would otherwise gain a redirect hop on every pass.
        if base and url.startswith(f"{base}/r/"):
            return match.group(0)

        link = TrackedLink(
            tenant_id=tenant_id,
            campaign=campaign,
            contact=contact,
            code=secrets.token_urlsafe(_CODE_BYTES),
            original_url=url,
            target_url=_merge_utm(url, utm or {}),
        )
        links.append(link)
        return short_url(link.code) + suffix

    return _URL_RE.sub(replace, text), links


def save_links(links: list, *, message=None) -> None:
    if not links:
        return
    if message is not None:
        for link in links:
            link.message = message
    TrackedLink.objects.bulk_create(links)


def record_click(link: TrackedLink, *, ip_address=None, user_agent="", referer="") -> None:
    """
    Append the click and roll the counters forward.

    Deliberately best-effort: the redirect is what the person clicking is
    waiting for, and losing one row of analytics is far better than showing
    them an error page.
    """
    now = timezone.now()
    LinkClick.objects.create(
        link=link, ip_address=ip_address, user_agent=(user_agent or "")[:1000], referer=(referer or "")[:1000],
    )
    TrackedLink.objects.filter(pk=link.pk).update(
        click_count=F("click_count") + 1, last_clicked_at=now,
        # Only stamped on the way in, so "first clicked" keeps meaning the
        # first click rather than the most recent one.
        **({} if link.first_clicked_at else {"first_clicked_at": now}),
    )
