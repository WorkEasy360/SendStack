"""
Turning a tap into an attributable conversation.

The hard part of lead capture on WhatsApp is that the click happens on one
side of a wall and the message arrives on the other, with nothing carrying
across. WhatsApp offers exactly two bridges: the text a wa.me link prefills,
and the `referral` block Meta attaches to a message that came from an ad.
Both are used here; neither is invented.
"""

import logging
import re
import secrets
from urllib.parse import quote

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from .models import ClickToChatLink, LeadEvent, LinkVisit

logger = logging.getLogger("multisender")

_CODE_BYTES = 4  # -> a 6-character code, short enough to read off a poster
_REFERENCE_RE = re.compile(r"#([A-Za-z0-9_-]{4,16})")


def generate_code() -> str:
    return secrets.token_urlsafe(_CODE_BYTES)[:6]


def public_base_url() -> str:
    base = getattr(settings, "LINK_TRACKING_BASE_URL", "") or getattr(settings, "MEDIA_PUBLIC_BASE_URL", "")
    return (base or "").rstrip("/")


def short_url(link) -> str:
    """The URL that goes on the poster, in the bio, behind the button."""
    return f"{public_base_url()}/c/{link.code}"


def prefilled_text(link) -> str:
    """
    What the customer's message box is filled with.

    The reference goes on its own line at the end so that the sentence the
    tenant wrote reads normally -- somebody about to send this sees their own
    words first and a short code underneath, not a message beginning with one.
    """
    text = (link.prefilled_message or "").strip()
    if not link.include_reference:
        return text
    return f"{text}\n\n{link.reference}".strip()


def whatsapp_url(link) -> str:
    """
    The wa.me destination.

    Built from the account's own number, so a link keeps working when the
    tenant reconnects the device -- and stops working loudly, rather than
    sending customers to a stranger, if the number is not known yet.
    """
    number = (link.whatsapp_account.phone_number or "").lstrip("+").replace(" ", "")
    if not number:
        return ""
    text = prefilled_text(link)
    return f"https://wa.me/{number}" + (f"?text={quote(text)}" if text else "")


def record_visit(link, *, ip_address=None, user_agent="", referer="") -> None:
    """Best-effort, like every other click record: the redirect matters more."""
    LinkVisit.objects.create(
        link=link, ip_address=ip_address, user_agent=(user_agent or "")[:1000], referer=(referer or "")[:1000],
    )
    ClickToChatLink.objects.filter(pk=link.pk).update(click_count=F("click_count") + 1, updated_at=timezone.now())


# ---------------------------------------------------------------------------
# Attribution on the way back
# ---------------------------------------------------------------------------

def find_link_reference(tenant_id, text: str):
    """The click-to-chat link a message's text refers back to, if any."""
    for match in _REFERENCE_RE.finditer(text or ""):
        link = ClickToChatLink.objects.filter(tenant_id=tenant_id, code=match.group(1)).first()
        if link is not None:
            return link
    return None


def set_first_touch(contact, source: str, detail: dict) -> None:
    """
    Record where a contact came from, once.

    First touch wins: a customer who arrived from a QR poster in March and
    clicked an ad in June was won by the poster, and overwriting that would
    quietly credit every channel to whichever was used most recently.
    """
    if contact is None or contact.source:
        return
    contact.source = source
    contact.source_detail = detail
    contact.save(update_fields=["source", "source_detail", "updated_at"])


def attribute_inbound(message) -> LeadEvent | None:
    """
    Work out where this inbound message came from, and write it down.

    Runs for every inbound message and finds something for very few of them,
    which is the expected shape: most messages are the middle of a
    conversation, not the start of one.
    """
    contact = message.contact
    tenant_id = message.tenant_id

    referral = (getattr(message, "referral_payload", None) or {})
    if referral:
        event = LeadEvent.objects.create(
            tenant_id=tenant_id, contact=contact, message=message,
            kind=LeadEvent.Kind.AD_REFERRAL, payload=referral,
        )
        set_first_touch(contact, LeadEvent.Kind.AD_REFERRAL, referral)
        logger.info("Contact %s attributed to a click-to-WhatsApp ad.", contact.pk if contact else None)
        return event

    link = find_link_reference(tenant_id, message.body or "")
    if link is None:
        return None

    # Counted once per contact: somebody who taps the poster three times is
    # one lead, and a counter that says three makes the poster look better
    # than it was.
    already = LeadEvent.objects.filter(link=link, contact=contact).exists()
    event = LeadEvent.objects.create(
        tenant_id=tenant_id, contact=contact, message=message, link=link,
        kind=LeadEvent.Kind.CLICK_TO_CHAT,
        payload={
            "utm_source": link.utm_source, "utm_medium": link.utm_medium, "utm_campaign": link.utm_campaign,
        },
    )
    if not already:
        ClickToChatLink.objects.filter(pk=link.pk).update(lead_count=F("lead_count") + 1)
    set_first_touch(contact, LeadEvent.Kind.CLICK_TO_CHAT, {
        "link": link.pk, "name": link.name,
        "utm_source": link.utm_source, "utm_medium": link.utm_medium, "utm_campaign": link.utm_campaign,
    })
    return event


def qr_svg(link, *, scale=8, dark="#111827") -> str:
    """
    The link as a printable QR code.

    SVG rather than a raster image: a poster is printed, and a QR that pixelates
    at A3 is a QR that does not scan. Error correction is set high so the code
    survives being printed small, laminated badly, or partly covered.
    """
    import io

    import segno

    code = segno.make(short_url(link), error="h")
    # segno writes bytes even for SVG, so this is a BytesIO decoded once at
    # the end rather than a StringIO.
    buffer = io.BytesIO()
    code.save(buffer, kind="svg", scale=scale, dark=dark, light=None, xmldecl=False, svgns=True)
    return buffer.getvalue().decode("utf-8")
