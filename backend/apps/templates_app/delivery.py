"""
How a piece of message content will actually arrive, on a given connection.

The composer's job is to stop somebody building a message that cannot be
delivered as they imagine it. That question has two halves and only one of
them is about validation:

  * **Can this connection send this at all?** Answered by the provider
    capability layer -- ``ConnectionManager.capabilities``.
  * **If not, what does the recipient get instead?** Answered by the same
    functions the send path calls, so the text shown in the composer is
    character-for-character the text that goes on the wire.

That second half is the whole reason this module exists rather than the
browser guessing. A degraded list is not "the list, roughly": it is a specific
string produced by ``core.interactive.list_as_text``, and a preview that
approximated it would eventually be wrong in a way nobody could see until a
customer received it.

Nothing here writes, and nothing here is on the send path -- it only reads the
same rules the send path enforces.
"""

from core.buttons import (
    CALL,
    REPLY,
    URL,
    as_numbered_options,
    body_with_text_buttons,
    button_kinds,
    plan_delivery,
)
from core.interactive import BUTTON, LIST, POLL, content_kind, list_as_text, poll_as_text

#: What each content kind needs the connection to support. TEXT is absent
#: because it needs nothing beyond being able to send at all.
REQUIRED_CAPABILITY = {
    LIST: "lists",
    POLL: "polls",
}

#: Reply buttons and link buttons are different capabilities, and a call
#: button is a third that nothing supports yet.
BUTTON_CAPABILITY = {
    REPLY: "buttons",
    URL: "url_buttons",
    CALL: "call_buttons",
}


def required_capabilities(spec) -> list:
    """
    Every capability this content needs, in the order the kinds appear.

    A button message can need more than one now that the three kinds combine
    (see ``core.buttons``): reply buttons plus a call button needs both
    ``buttons`` and ``call_buttons``, and a connection delivers the message as
    built only if it has all of them.

    ``spec`` is anything ``core.interactive.content_kind`` accepts: a model
    instance, or the dict a campaign's ``effective_message()`` returns.
    """
    kind = content_kind(spec)
    if kind == BUTTON:
        kinds = button_kinds(_read(spec, "buttons", [])) or [REPLY]
        return [BUTTON_CAPABILITY[button] for button in kinds]
    needed = REQUIRED_CAPABILITY.get(kind, "")
    return [needed] if needed else []


def required_capability(spec) -> str:
    """
    The single capability that best names this content, or "" when it needs
    none -- the first of `required_capabilities`.

    Kept alongside the list because callers that only want to know "is there
    something a connection could refuse here" ask this, and because the
    composer labels one message type with one capability.
    """
    needed = required_capabilities(spec)
    return needed[0] if needed else ""


def _read(spec, name, default):
    return spec.get(name, default) if isinstance(spec, dict) else getattr(spec, name, default)


def degraded_text(spec) -> str:
    """
    Exactly what a connection with no native support will send instead.

    One function, called both here and -- through the same underlying
    helpers -- by ``apps.messaging.services``. A poll degrades to its whole
    question and options in one message; a list to its menu; buttons to a
    numbered set of choices.
    """
    kind = content_kind(spec)
    body = (_read(spec, "body", "") or _read(spec, "caption", "") or "").strip()

    if kind == POLL:
        return poll_as_text(body, _read(spec, "poll_content", {}))
    if kind == LIST:
        return list_as_text(body, _read(spec, "list_content", {}))
    if kind == BUTTON:
        return as_numbered_options(body, _read(spec, "buttons", []))
    return body


def as_delivered(spec, *, can_show_buttons: bool):
    """
    ``(text, tappable)`` -- the body that goes on the wire for a button
    message, and the buttons that stay tappable on top of it.

    Two different degradations, and conflating them is what this separates:

      * a connection that cannot show buttons at all writes *everything* out,
        as the numbered list `as_numbered_options` produces, and nothing is
        tappable.
      * a connection that can still cannot show two kinds at once, so
        `core.buttons.plan_delivery` keeps one kind tappable and spells the
        rest into the body -- the same call the Cloud API provider makes, so
        this is the text it will send rather than an approximation of it.
    """
    body = (_read(spec, "body", "") or _read(spec, "caption", "") or "").strip()
    buttons = _read(spec, "buttons", []) or []

    if not can_show_buttons:
        return as_numbered_options(body, buttons), []

    tappable, spelled_out = plan_delivery(buttons)
    return body_with_text_buttons(body, spelled_out), tappable


#: Said to the author, in the composer, for each way content can fail to
#: arrive as built. Sentences rather than codes for the same reason the
#: campaign pre-flight uses sentences: this text is rendered as-is, and a
#: code-to-copy mapping in the browser would be a second place for the
#: wording to drift.
_UNSUPPORTED = {
    "buttons": (
        "This number cannot show tappable buttons -- WhatsApp only renders those on official "
        "API numbers. The choices will be sent as a numbered list instead, and replies like "
        "\"1\" trigger the same automation."
    ),
    "url_buttons": (
        "This number cannot show a link button. The link will be sent as part of the message "
        "text, which is what tapping the button would have opened."
    ),
    # Not about the connection at all -- this one is WhatsApp's message format,
    # so it reads the same on every number and cannot be fixed by choosing a
    # different one. Kept beside the capability notes because the composer
    # shows them in the same place, for the same reason.
    "mixed_url": (
        "A link button cannot sit beside reply buttons -- WhatsApp sends the two as different "
        "kinds of message. The reply buttons stay tappable and the link is sent as part of the "
        "message text, which is what tapping it would have opened."
    ),
    "call_buttons": (
        "WhatsApp offers a call button only inside an approved message template, so no "
        "connection can send one on an ordinary message. The number will be sent as text, "
        "which every WhatsApp client turns into a tappable dial link."
    ),
    "lists": (
        "This number cannot show a tappable menu. The options will be sent as a numbered list "
        "instead, and replies like \"1\" are matched back to the same menu row."
    ),
    "polls": (
        "This number cannot send a poll -- polls are not available on the official WhatsApp "
        "API. The question will be sent as a numbered list instead, and the replies are "
        "recorded as answers."
    ),
}


def _button_note(spelled_out, *, can_show_buttons: bool) -> str:
    """
    Why this button message will not arrive exactly as built, in the author's
    words.

    A connection that shows no buttons at all gets one sentence about the whole
    message, because that is one thing that happened to it. Otherwise there is
    a sentence per button that had to become text, since a link losing its tap
    and a call button never having had one are different facts with different
    remedies -- and the author can act on the first.
    """
    if not can_show_buttons:
        return _UNSUPPORTED["buttons"]

    kinds = button_kinds(spelled_out)
    notes = []
    if URL in kinds:
        notes.append(_UNSUPPORTED["mixed_url"])
    if CALL in kinds:
        notes.append(_UNSUPPORTED["call_buttons"])
    return " ".join(notes)


def delivery_preview(spec, *, tenant, account_id=None) -> dict:
    """
    Whether this content arrives as built on a given number, and what it
    becomes if not.

    ``account_id`` is optional: a composer that has not chosen a number yet
    still wants to know which of its workspace's connections *could* send
    this, which is what ``accounts`` answers. Nothing here is a refusal -- the
    content is always deliverable in some form -- so the shape is "here is
    what will happen", not "here is an error".
    """
    from apps.whatsapp.connections.manager import ConnectionManager
    from apps.whatsapp.models import WhatsAppAccount

    needed = required_capabilities(spec)
    capability = needed[0] if needed else ""
    kind = content_kind(spec)

    # What the message format itself forces into text, whatever the number is.
    # A mixed button list has no single envelope, so one kind is spelled out on
    # *every* connection -- which is why it is computed once here rather than
    # per account, and why it makes "supported" False even for a number whose
    # capabilities each say yes on their own.
    spelled_out = plan_delivery(_read(spec, "buttons", []))[1] if kind == BUTTON else []

    accounts = list(
        WhatsAppAccount.objects.filter(tenant=tenant).only("id", "name", "provider", "status")
    )
    per_account = []
    for account in accounts:
        supported = not spelled_out and all(
            ConnectionManager.supports(account, name) for name in needed
        )
        per_account.append({
            "id": account.pk,
            "name": account.name,
            "provider": account.provider,
            "status": account.status,
            "supported": supported,
        })

    chosen = next((row for row in per_account if row["id"] == account_id), None)

    result = {
        "content_kind": kind,
        "capability": capability,
        "capabilities": needed,
        "accounts": per_account,
        # True when at least one connected number can deliver this natively.
        # A composer uses it to say "no connected number can show this as a
        # menu" before the campaign is built rather than after.
        "any_native": any(
            row["supported"] for row in per_account
            if row["status"] == WhatsAppAccount.Status.CONNECTED
        ),
    }

    if chosen is None:
        result.update(
            {"account_id": None, "native": None, "note": "", "as_sent": "", "as_sent_buttons": []}
        )
        return result

    native = chosen["supported"]
    if kind == BUTTON and not native:
        account = next(row for row in accounts if row.pk == chosen["id"])
        can_show_buttons = ConnectionManager.supports(account, "buttons")
        as_sent, tappable = as_delivered(spec, can_show_buttons=can_show_buttons)
        # The capability note is the backstop: `_button_note` speaks only about
        # buttons that lost their tap, and a number could in principle be
        # missing a capability without any of them having done so. Saying
        # nothing while reporting "not native" is the one answer this endpoint
        # must never give.
        note = _button_note(spelled_out, can_show_buttons=can_show_buttons) or next(
            (
                _UNSUPPORTED[name]
                for name in needed
                if name in _UNSUPPORTED and not ConnectionManager.supports(account, name)
            ),
            "",
        )
    else:
        as_sent, tappable = ("" if native else degraded_text(spec)), []
        note = "" if native else _UNSUPPORTED.get(capability, "")

    result.update({
        "account_id": chosen["id"],
        "native": native,
        "note": note,
        # Only for the degraded case: when it sends natively, what the
        # recipient sees is the structured content the composer is already
        # drawing, and echoing a flattened version of it would be misleading.
        "as_sent": as_sent,
        # The buttons that survive as buttons on top of that text. Empty for a
        # connection that shows none at all, so a preview drawing them is
        # drawing what will actually be tappable rather than the whole list.
        "as_sent_buttons": tappable,
    })
    return result
