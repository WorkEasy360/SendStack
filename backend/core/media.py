"""
Shared handling for the ``media_items`` list that carries multi-attachment
message content.

Every place a message can be composed (templates, campaigns, automation rules,
inbox replies) historically held exactly one attachment across three columns:
``media_url`` / ``message_type`` / ``filename``. Those columns still exist and
still mean what they always did -- the *first* attachment -- so every existing
row, report, export and provider call keeps working untouched. ``media_items``
sits alongside them and holds the full ordered list.

One item is ``{"url": str, "type": "IMAGE"|"VIDEO"|"DOCUMENT", "filename": str,
"caption": str}``. ``filename`` and ``caption`` are optional; ``filename`` is
required for DOCUMENT because that is what the provider labels the download
with.
"""

from django.core.exceptions import ValidationError

# Kept as plain strings rather than importing one model's TextChoices: five
# different models declare an identical MessageType and none of them owns this.
IMAGE = "IMAGE"
VIDEO = "VIDEO"
DOCUMENT = "DOCUMENT"
TEXT = "TEXT"

MEDIA_TYPES = (IMAGE, VIDEO, DOCUMENT)

MAX_MEDIA_ITEMS = 30

_ALLOWED_KEYS = ("url", "type", "filename", "caption")

# An attachment URL is rendered as a link in the inbox thread and handed to
# the provider to fetch, so the scheme is checked rather than trusted --
# "javascript:..." must never reach an href. Deliberately looser than
# Django's URLValidator, which rejects perfectly good internal hostnames
# that have no TLD (http://minio/bucket/file.png).
_ALLOWED_SCHEMES = ("http://", "https://")


def clean_media_items(value, *, max_items=MAX_MEDIA_ITEMS):
    """
    Validate and normalise a raw ``media_items`` payload.

    Returns a new list of dicts with exactly the four known keys, unknown keys
    dropped and every value coerced to a string. Raises ``ValidationError``
    with a human-readable message on anything malformed -- serializers turn
    that into a 400 rather than letting junk reach the provider.
    """
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValidationError("Attachments must be a list.")
    if len(value) > max_items:
        raise ValidationError(f"At most {max_items} attachments are allowed ({len(value)} given).")

    cleaned = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"Attachment {index} must be an object.")

        url = str(raw.get("url") or "").strip()
        if not url:
            raise ValidationError(f"Attachment {index} is missing a url.")
        if not url.lower().startswith(_ALLOWED_SCHEMES):
            raise ValidationError(f"Attachment {index} must have an http:// or https:// url.")

        kind = str(raw.get("type") or "").strip().upper()
        if kind not in MEDIA_TYPES:
            raise ValidationError(
                f"Attachment {index} has an unsupported type {kind or '(blank)'!r}; "
                f"expected one of {', '.join(MEDIA_TYPES)}."
            )

        filename = str(raw.get("filename") or "").strip()
        if kind == DOCUMENT and not filename:
            raise ValidationError(f"Attachment {index} is a document and needs a filename.")

        cleaned.append({
            "url": url,
            "type": kind,
            "filename": filename,
            "caption": str(raw.get("caption") or ""),
        })
    return cleaned


def legacy_fields(items):
    """
    The single-attachment columns that represent ``items``.

    Returns ``{}`` for an empty list so callers can tell "no attachments were
    supplied" (leave the legacy columns alone) apart from "the first attachment
    is this one".
    """
    if not items:
        return {}
    first = items[0]
    return {
        "message_type": first["type"],
        "media_url": first["url"],
        "filename": first["filename"],
        # Mirrored too, so the record's own caption column never disagrees with
        # the first attachment's once per-attachment captions are in use.
        "caption": first["caption"],
    }


def resolve_media_items(spec):
    """
    The attachments a message should actually send, newest schema first.

    ``spec`` is any mapping with the message-content keys -- an
    ``effective_message()`` dict, or a ``Message``-shaped object via
    ``spec_from_object``. When ``media_items`` is empty this falls back to the
    single ``media_url``/``message_type``/``filename`` triple, which is what
    every row written before multi-attachment support looks like.
    """
    items = spec.get("media_items") or []
    if items:
        return items

    kind = (spec.get("message_type") or TEXT).upper()
    url = spec.get("media_url") or ""
    if kind == TEXT or not url:
        return []
    return [{
        "url": url,
        "type": kind,
        "filename": spec.get("filename") or "",
        "caption": spec.get("caption") or "",
    }]


def spec_from_object(obj):
    """``resolve_media_items``-compatible view of a model instance."""
    return {
        "media_items": getattr(obj, "media_items", None),
        "message_type": getattr(obj, "message_type", TEXT),
        "media_url": getattr(obj, "media_url", ""),
        "filename": getattr(obj, "filename", ""),
        "caption": getattr(obj, "caption", ""),
    }
