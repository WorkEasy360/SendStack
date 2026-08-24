"""
Shared handling for the ``buttons`` list that turns a plain message into an
interactive one.

A message with buttons is delivered as a WhatsApp *interactive* message rather
than a text message, so the recipient taps an answer instead of typing one.
Two shapes exist, and they are the two the Cloud API supports for free-form
(non-approved-template) messages -- they cannot be mixed in one message:

  - up to three ``REPLY`` buttons (``interactive.type = "button"``). Tapping one
    sends an inbound message back carrying the button's ``id``, which is what
    automation matches on -- see ``apps.automation.services.trigger_button_rules``.
  - exactly one ``URL`` button (``interactive.type = "cta_url"``), which opens a
    link. Nothing comes back when it is tapped, so it can't drive automation.

One item is ``{"type": "REPLY"|"URL", "id": str, "title": str, "url": str}``.
``id`` identifies a REPLY button for the rest of its life (automation rules and
workflow conditions store it), so it is derived from the title only when the
caller doesn't supply one, and never rewritten afterwards. ``url`` is only
meaningful for a URL button.

Call buttons are deliberately absent: the Cloud API only offers those inside an
approved message template, not on free-form sends, and this list is free-form
content.
"""

import re

from django.core.exceptions import ValidationError

REPLY = "REPLY"
URL = "URL"

BUTTON_TYPES = (REPLY, URL)

# Meta's limits for an interactive message. Enforced here rather than left to
# the provider so a bad button is a 400 on save, not a failed send hours later.
MAX_REPLY_BUTTONS = 3
MAX_TITLE_LENGTH = 20
MAX_ID_LENGTH = 256
# The interactive body is a separate field from a text message's body and has
# its own, shorter, cap.
MAX_BODY_LENGTH = 1024

_ALLOWED_SCHEMES = ("http://", "https://")

_ID_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _derive_id(title: str, taken: set) -> str:
    base = _ID_SAFE_RE.sub("_", title.lower()).strip("_") or "button"
    base = base[:MAX_ID_LENGTH]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: MAX_ID_LENGTH - 3]}_{suffix}"
        suffix += 1
    return candidate


def clean_buttons(value):
    """
    Validate and normalise a raw ``buttons`` payload.

    Returns a new list of dicts with exactly the three known keys, unknown keys
    dropped and every value coerced to a string. Raises ``ValidationError``
    with a human-readable message on anything malformed -- serializers turn
    that into a 400 rather than letting junk reach the provider.
    """
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValidationError("Buttons must be a list.")

    kinds = set()
    cleaned = []
    taken_ids = set()

    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"Button {index} must be an object.")

        kind = str(raw.get("type") or REPLY).strip().upper()
        if kind not in BUTTON_TYPES:
            raise ValidationError(
                f"Button {index} has an unsupported type {kind or '(blank)'!r}; "
                f"expected one of {', '.join(BUTTON_TYPES)}."
            )
        kinds.add(kind)

        title = str(raw.get("title") or "").strip()
        if not title:
            raise ValidationError(f"Button {index} needs a label.")
        if len(title) > MAX_TITLE_LENGTH:
            raise ValidationError(
                f"Button {index}'s label is {len(title)} characters; WhatsApp allows at most {MAX_TITLE_LENGTH}."
            )

        link = str(raw.get("url") or "").strip()
        if kind == URL:
            if not link:
                raise ValidationError(f"Button {index} is a link button and needs a url.")
            if not link.lower().startswith(_ALLOWED_SCHEMES):
                raise ValidationError(f"Button {index} must have an http:// or https:// url.")
        else:
            link = ""

        button_id = str(raw.get("id") or "").strip()
        if kind == REPLY:
            if len(button_id) > MAX_ID_LENGTH:
                raise ValidationError(f"Button {index}'s id is longer than {MAX_ID_LENGTH} characters.")
            if not button_id:
                button_id = _derive_id(title, taken_ids)
            if button_id in taken_ids:
                raise ValidationError(f"Button {index} reuses the id {button_id!r}; each button needs its own.")
            taken_ids.add(button_id)
        else:
            button_id = ""

        cleaned.append({"type": kind, "id": button_id, "title": title, "url": link})

    if len(kinds) > 1:
        raise ValidationError(
            "A message can have either reply buttons or one link button, not both -- "
            "WhatsApp sends them as different kinds of interactive message."
        )
    if REPLY in kinds and len(cleaned) > MAX_REPLY_BUTTONS:
        raise ValidationError(
            f"At most {MAX_REPLY_BUTTONS} reply buttons are allowed ({len(cleaned)} given)."
        )
    if URL in kinds and len(cleaned) > 1:
        raise ValidationError("Only one link button is allowed on a message.")

    return cleaned


def button_kind(buttons):
    """``REPLY``, ``URL``, or None for a message with no buttons."""
    if not buttons:
        return None
    return buttons[0].get("type") or REPLY


def reply_button_ids(buttons):
    """Every id a contact could send back by tapping one of these buttons."""
    return [button["id"] for button in (buttons or []) if button.get("type", REPLY) == REPLY and button.get("id")]


def find_button(buttons, button_id):
    for button in buttons or []:
        if button.get("id") and button["id"] == button_id:
            return button
    return None


def as_numbered_options(body: str, buttons) -> str:
    """
    The plain-text form of a button message, for a connection that can't send
    interactive messages (see ``WhatsAppProvider.supports_interactive_buttons``).

    Degrading to a numbered list keeps the send working and the choices legible
    instead of dropping them silently; a URL button becomes the link itself,
    since that is the whole of what tapping it would have done.
    """
    options = []
    for position, button in enumerate(buttons or [], start=1):
        if button.get("type") == URL:
            options.append(f"{button['title']}: {button['url']}")
        else:
            options.append(f"{position}. {button['title']}")

    parts = [part for part in [(body or "").strip(), "\n".join(options)] if part]
    return "\n\n".join(parts)
