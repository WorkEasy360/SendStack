"""
Shared handling for the ``buttons`` list that turns a plain message into an
interactive one.

A message with buttons is delivered as a WhatsApp *interactive* message rather
than a text message, so the recipient taps an answer instead of typing one.
Three kinds exist, and one message may carry any combination of them:

  - up to three ``REPLY`` buttons (``interactive.type = "button"``). Tapping one
    sends an inbound message back carrying the button's ``id``, which is what
    automation matches on -- see ``apps.automation.services.trigger_button_rules``.
  - at most one ``URL`` button (``interactive.type = "cta_url"``), which opens a
    link. Nothing comes back when it is tapped, so it can't drive automation.
  - at most one ``CALL`` button, which dials a number.

**Mixing them is an authoring convenience, not a wire format.** The Cloud API
has no envelope carrying two kinds at once: ``button`` holds reply buttons and
``cta_url`` holds a single link, and there is no interactive type for a call at
all. So a mixed list is not rejected -- it is *planned*: `plan_delivery` picks
the one kind that goes out tappable and hands the rest back to be written into
the body as labelled lines, which is exactly what a call button has always
done. Rejecting the mixture instead would only push the same decision onto the
author, who would then paste the link into the body by hand and get a worse
version of the same message. The composer shows the planned result before
anything is saved, so nothing about this is discovered after a send.

One item is ``{"type": "REPLY"|"URL"|"CALL", "id": str, "title": str,
"url": str, "phone": str}``. ``id`` identifies a REPLY button for the rest of
its life (automation rules and workflow conditions store it), so it is derived
from the title only when the caller doesn't supply one, and never rewritten
afterwards. ``url`` is only meaningful for a URL button and ``phone`` only for
a CALL button.

**A CALL button is not natively deliverable on any connection this platform
has**, and that is a fact about WhatsApp rather than a gap here: Meta offers a
call button only inside an approved message template, and this list is
free-form content. It is nonetheless a first-class type because the alternative
-- making people paste a phone number into the body and hope it is recognised
-- is worse than one that degrades predictably. Every provider therefore leaves
``supports_call_buttons`` False, and the send path renders it as a labelled
number ("Call Now: +919876543210"), which every WhatsApp client turns into a
tappable dial link. Nothing pretends it went out as a button; the composer says
so before the message is saved. If Meta ever opens call buttons to free-form
sends, a provider flips the flag and the rest of this path is already in place.
"""

import re

from django.core.exceptions import ValidationError

REPLY = "REPLY"
URL = "URL"
CALL = "CALL"

BUTTON_TYPES = (REPLY, URL, CALL)

# Meta's limits for an interactive message. Enforced here rather than left to
# the provider so a bad button is a 400 on save, not a failed send hours later.
MAX_REPLY_BUTTONS = 3
#: One each, because each is a single slot in the payload it goes out in: a
#: `cta_url` names one link, and a spelled-out call line names one number.
#: Two of either is a message the author cannot have meant.
MAX_URL_BUTTONS = 1
MAX_CALL_BUTTONS = 1
MAX_TITLE_LENGTH = 20
MAX_ID_LENGTH = 256
# The interactive body is a separate field from a text message's body and has
# its own, shorter, cap.
MAX_BODY_LENGTH = 1024

_ALLOWED_SCHEMES = ("http://", "https://")

_ID_SAFE_RE = re.compile(r"[^a-z0-9]+")

#: A call button's number, in E.164. Matched rather than merely stripped of
#: punctuation because this number is printed in the message and dialled by
#: the recipient: "call us on 98765" wastes their tap and our credibility.
#: Deliberately the same shape apps.contacts.utils.normalize_phone produces,
#: so a number that can be a contact can be a call button.
_E164_RE = re.compile(r"\+[1-9]\d{7,14}")


def _derive_id(title: str, taken: set) -> str:
    base = _ID_SAFE_RE.sub("_", title.lower()).strip("_") or "button"
    base = base[:MAX_ID_LENGTH]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: MAX_ID_LENGTH - 3]}_{suffix}"
        suffix += 1
    return candidate


def ensure_ids(buttons):
    """
    Fill in a missing id on every REPLY button, and change nothing else.

    ``clean_buttons`` already does this on the authoring path, so buttons that
    came through a serializer arrive here complete. This is the *delivery-time*
    guarantee for the ones that did not -- a row written by a fixture, a data
    migration, the admin, or a caller that built an action dict by hand --
    because a reply button's id is what comes back when the recipient taps it,
    and a button with no id is one no rule can ever match.

    Deliberately non-raising, and deliberately not a second validator: a
    malformed button is the send path's problem to reject or degrade, and
    turning one into a lost reply here would be worse than sending it. Ids that
    are already set are never rewritten, because rules store them.
    """
    if not buttons:
        return []

    taken = {
        str(button.get("id") or "").strip()
        for button in buttons
        if isinstance(button, dict) and str(button.get("id") or "").strip()
    }
    filled = []
    for button in buttons:
        if not isinstance(button, dict):
            filled.append(button)
            continue
        kind = str(button.get("type") or REPLY).strip().upper()
        button_id = str(button.get("id") or "").strip()
        if kind != REPLY or button_id:
            filled.append(button)
            continue
        derived = _derive_id(str(button.get("title") or ""), taken)
        taken.add(derived)
        filled.append({**button, "id": derived})
    return filled


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

    counts = {REPLY: 0, URL: 0, CALL: 0}
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
        counts[kind] += 1

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

        phone = str(raw.get("phone") or "").strip()
        if kind == CALL:
            # Normalised the same way a contact's number is, so "+91 98765
            # 43210" and "+919876543210" are the same button. Anything that
            # still isn't E.164 after that is rejected rather than dialled.
            phone = "+" + re.sub(r"\D", "", phone)
            if not _E164_RE.fullmatch(phone):
                raise ValidationError(
                    f"Button {index} is a call button and needs a phone number in international "
                    "format, e.g. +919876543210."
                )
        else:
            phone = ""

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

        cleaned.append({"type": kind, "id": button_id, "title": title, "url": link, "phone": phone})

    # Counted per kind rather than across the list: the three are combinable
    # (see the module docstring), so what matters is how many of each there
    # are, not how many there are in total.
    if counts[REPLY] > MAX_REPLY_BUTTONS:
        raise ValidationError(
            f"At most {MAX_REPLY_BUTTONS} reply buttons are allowed ({counts[REPLY]} given)."
        )
    if counts[URL] > MAX_URL_BUTTONS:
        raise ValidationError("Only one link button is allowed on a message.")
    if counts[CALL] > MAX_CALL_BUTTONS:
        raise ValidationError("Only one call button is allowed on a message.")

    return cleaned


def button_kinds(buttons) -> list:
    """Every kind on this message, in ``BUTTON_TYPES`` order and without repeats."""
    present = {(button.get("type") or REPLY) for button in buttons or []}
    return [kind for kind in BUTTON_TYPES if kind in present]


def button_kind(buttons):
    """
    The *primary* kind on this message -- ``REPLY``, ``URL``, ``CALL``, or None
    when there are no buttons.

    Primary in the sense `plan_delivery` means it: the kind that goes out
    tappable when the connection can show buttons at all. Reply buttons win
    over a link because they are the only kind an answer comes back from, and
    a link beats a call because no connection has ever delivered a call button.

    Read it as "what kind of message is this", not "what is in the list" -- for
    the latter use `button_kinds`.
    """
    for kind in BUTTON_TYPES:
        if any((button.get("type") or REPLY) == kind for button in buttons or []):
            return kind
    return None


def split_buttons(buttons):
    """``(replies, link_or_None, call_or_None)`` -- the list by kind."""
    replies, link, call = [], None, None
    for button in buttons or []:
        kind = button.get("type") or REPLY
        if kind == URL and link is None:
            link = button
        elif kind == CALL and call is None:
            call = button
        elif kind == REPLY:
            replies.append(button)
    return replies, link, call


def plan_delivery(buttons):
    """
    ``(tappable, spelled_out)`` for a connection that *can* send interactive
    buttons.

    The single decision behind a mixed button list, in one place because two
    callers have to agree on it exactly: the Cloud API provider, which builds
    the payload, and `apps.templates_app.delivery`, which shows the author what
    that payload will look like. A preview that reasoned about this separately
    would eventually disagree with the send, which is the one failure this
    whole layer exists to prevent.

    Reply buttons take the interactive envelope when there are any, because
    they are the only kind that sends something back -- losing their tap would
    unhook the automation attached to them, while a link written into the body
    is still a link the recipient can tap. Otherwise a lone link takes it as a
    ``cta_url``. A call button is never tappable on any connection (Meta offers
    one only inside an approved template), so it is always spelled out.

    For a connection with no interactive support at all this is not the right
    question -- everything degrades together via `as_numbered_options`.
    """
    replies, link, call = split_buttons(buttons)
    if replies:
        tappable = replies
    elif link is not None:
        tappable = [link]
    else:
        tappable = []

    chosen = {id(button) for button in tappable}
    return tappable, [button for button in buttons or [] if id(button) not in chosen]


def describe_button(button) -> str:
    """
    A link or call button written out as one line of text: ``"Shop now:
    https://..."``.

    The value is rendered bare so the recipient's own client links it -- every
    WhatsApp client turns a URL into a tap and a phone number into a dial
    link, which is as close to the button as text gets.
    """
    kind = button.get("type") or REPLY
    value = button.get("url", "") if kind == URL else button.get("phone", "")
    return f"{button.get('title', '')}: {value}"


def body_with_text_buttons(body: str, buttons) -> str:
    """
    ``body`` with one line per button that could not go out tappable.

    Appended as its own paragraph rather than run into the message so the
    author's own closing line is not silently glued to a URL.
    """
    lines = [describe_button(button) for button in buttons or []]
    return "\n\n".join(part for part in [(body or "").strip(), "\n".join(lines)] if part)


def reply_button_ids(buttons):
    """Every id a contact could send back by tapping one of these buttons."""
    return [button["id"] for button in (buttons or []) if button.get("type", REPLY) == REPLY and button.get("id")]


def find_button(buttons, button_id):
    for button in buttons or []:
        if button.get("id") and button["id"] == button_id:
            return button
    return None


#: What a recipient is told to do with a numbered list. Present because a bare
#: list is ambiguous -- people reply "the first one", or nothing at all -- and
#: `automation.services.resolve_option_reply` can only match a number or an
#: option's exact wording. Saying which is expected is what makes the fallback
#: usable rather than merely legible.
REPLY_INSTRUCTION = "Please reply with the option number."


def numbered_options(buttons) -> list:
    """
    ``["1. Yes please", ...]`` -- the offered options, in the order they are
    numbered.

    Split out from `as_numbered_options` because the number a recipient types
    has to mean the same thing when the message is built and when the reply is
    resolved. One function producing that ordering, read by both, is what stops
    those two drifting apart -- and a drift here silently fires the wrong
    automation, which is the worst failure this path has.
    """
    options = []
    position = 0
    for button in buttons or []:
        if button.get("type") in (URL, CALL):
            # Neither has anything to reply *with*: tapping one opened a page
            # or dialled a number. `describe_button` is the same line the
            # interactive path appends for a button it could not make
            # tappable, so a link reads identically either way.
            options.append(describe_button(button))
        else:
            # Numbered against the *repliable* options only. Counting a link
            # or call button would offer a "2" that answers nothing, and
            # resolve_option_reply -- which numbers the same list the same way
            # -- would then map every choice one place off.
            position += 1
            options.append(f"{position}. {button['title']}")
    return options


def as_numbered_options(body: str, buttons, *, instruction: bool = True) -> str:
    """
    The plain-text form of a button message, for a connection that can't send
    interactive messages (see ``WhatsAppProvider.supports_interactive_buttons``).

    Degrading to a numbered list keeps the send working and the choices legible
    instead of dropping them silently; a URL button becomes the link itself,
    since that is the whole of what tapping it would have done.

    The instruction line is suppressed for a link-only message, where there is
    no number to reply with, and by callers that supply their own wording.
    """
    options = numbered_options(buttons)
    has_reply_option = any((button.get("type") or REPLY) == REPLY for button in buttons or [])

    parts = [(body or "").strip(), "\n".join(options)]
    if instruction and has_reply_option:
        parts.append(REPLY_INSTRUCTION)
    return "\n\n".join(part for part in parts if part)
