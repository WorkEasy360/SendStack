"""
Shared handling for the ``list_content`` and ``poll_content`` payloads that
turn a message into a menu or a poll.

Both sit alongside ``buttons`` (``core.buttons``) as the third and fourth ways
a message can ask the recipient something, and both follow that module's
pattern exactly: one JSON column on each content model, validated and
normalised here so a malformed payload is a 400 on save rather than a failed
send hours later.

    list      a tappable menu. ``{"header", "footer", "button", "sections":
              [{"title", "rows": [{"id", "title", "description"}]}]}``.
              The recipient taps the ``button``, picks a row, and the row's
              ``id`` comes back exactly as a reply button's does.

    poll      a question with fixed answers. ``{"question", "options": [str],
              "allow_multiple": bool}``.

**Neither is universally supported, and that is what the provider capability
layer exists for.** A list only reaches a phone on the Cloud API; a poll only
on a device-linked (Baileys) connection. Rather than refuse the send on the
other one, ``as_options`` below converts either into the same
``core.buttons`` item shape the numbered-list fallback already speaks -- so a
list on a QR number arrives as "1. Web development / 2. WhatsApp marketing",
the reply maps back to the original row id through the existing
``PendingButtonPrompt``, and no automation, workflow or report needs to know
which kind of connection the conversation was on.

The limits below are WhatsApp's own, enforced here rather than left to the
provider for the same reason ``core.buttons`` enforces its three-button cap:
a message that could never send should not be saveable.
"""

import re

from django.core.exceptions import ValidationError

# -- List limits (Cloud API interactive.type = "list") -----------------------
MAX_LIST_HEADER_LENGTH = 60
MAX_LIST_FOOTER_LENGTH = 60
MAX_LIST_BUTTON_LENGTH = 20
MAX_LIST_SECTIONS = 10
#: Rows across *every* section, not per section. WhatsApp counts the whole
#: menu, so a 3-section menu of 5 rows each is refused by Meta -- and would be
#: refused after the campaign had already been launched.
MAX_LIST_ROWS = 10
MAX_ROW_TITLE_LENGTH = 24
MAX_ROW_DESCRIPTION_LENGTH = 72
MAX_ROW_ID_LENGTH = 200
MAX_SECTION_TITLE_LENGTH = 24

# -- Poll limits -------------------------------------------------------------
MAX_POLL_QUESTION_LENGTH = 255
MAX_POLL_OPTIONS = 12
MIN_POLL_OPTIONS = 2
MAX_POLL_OPTION_LENGTH = 100

#: The default label on the button that opens a list. WhatsApp requires one --
#: a list with no call to action is a message the recipient cannot open.
DEFAULT_LIST_BUTTON = "View options"

_ID_SAFE_RE = re.compile(r"[^a-z0-9]+")


def _derive_id(title: str, taken: set) -> str:
    """
    A stable id for a row whose author did not supply one.

    Identical in spirit to ``core.buttons._derive_id``: the id is what comes
    back when the row is chosen and what automation matches on, so it is
    derived once from the label and never rewritten afterwards.
    """
    base = _ID_SAFE_RE.sub("_", (title or "").lower()).strip("_") or "option"
    base = base[:MAX_ROW_ID_LENGTH]
    candidate, suffix = base, 2
    while candidate in taken:
        candidate = f"{base[: MAX_ROW_ID_LENGTH - 3]}_{suffix}"
        suffix += 1
    return candidate


def _text(raw, field: str, subject: str, maximum: int, *, required=True) -> str:
    value = str(raw or "").strip()
    if required and not value:
        raise ValidationError(f"{subject} needs {field}.")
    if len(value) > maximum:
        raise ValidationError(
            f"{subject}'s {field} is {len(value)} characters; WhatsApp allows at most {maximum}."
        )
    return value


def clean_list_content(value):
    """
    Validate and normalise a raw ``list_content`` payload.

    Returns ``{}`` for anything empty, which is what every row written before
    lists existed looks like and what "this message is not a list" means
    everywhere downstream. Otherwise returns a new dict with exactly the known
    keys and every value coerced to a string.
    """
    if value in (None, "", {}, []):
        return {}
    if not isinstance(value, dict):
        raise ValidationError("List content must be an object.")

    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValidationError("A list message needs at least one section.")
    if len(raw_sections) > MAX_LIST_SECTIONS:
        raise ValidationError(
            f"At most {MAX_LIST_SECTIONS} sections are allowed ({len(raw_sections)} given)."
        )

    sections = []
    taken_ids: set = set()
    total_rows = 0

    for section_index, raw_section in enumerate(raw_sections, start=1):
        if not isinstance(raw_section, dict):
            raise ValidationError(f"Section {section_index} must be an object.")

        # A section title is optional on a single-section menu: WhatsApp
        # renders one heading for one group, and making somebody invent a name
        # for the only group there is adds a field with no reader.
        title = _text(
            raw_section.get("title"), "a title", f"Section {section_index}",
            MAX_SECTION_TITLE_LENGTH, required=len(raw_sections) > 1,
        )

        raw_rows = raw_section.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValidationError(f"Section {section_index} needs at least one row.")

        rows = []
        for row_index, raw_row in enumerate(raw_rows, start=1):
            if not isinstance(raw_row, dict):
                raise ValidationError(f"Row {row_index} of section {section_index} must be an object.")

            subject = f"Row {row_index} of section {section_index}"
            row_title = _text(raw_row.get("title"), "a title", subject, MAX_ROW_TITLE_LENGTH)
            description = _text(
                raw_row.get("description"), "a description", subject,
                MAX_ROW_DESCRIPTION_LENGTH, required=False,
            )

            row_id = str(raw_row.get("id") or "").strip()
            if len(row_id) > MAX_ROW_ID_LENGTH:
                raise ValidationError(f"{subject}'s id is longer than {MAX_ROW_ID_LENGTH} characters.")
            if not row_id:
                row_id = _derive_id(row_title, taken_ids)
            if row_id in taken_ids:
                raise ValidationError(
                    f"{subject} reuses the id {row_id!r}; each row needs its own so a reply "
                    "identifies exactly one option."
                )
            taken_ids.add(row_id)

            rows.append({"id": row_id, "title": row_title, "description": description})
            total_rows += 1

        sections.append({"title": title, "rows": rows})

    if total_rows > MAX_LIST_ROWS:
        raise ValidationError(
            f"A list message can offer at most {MAX_LIST_ROWS} rows in total ({total_rows} given)."
        )

    return {
        "header": _text(value.get("header"), "a header", "The list", MAX_LIST_HEADER_LENGTH, required=False),
        "footer": _text(value.get("footer"), "a footer", "The list", MAX_LIST_FOOTER_LENGTH, required=False),
        "button": _text(
            value.get("button") or DEFAULT_LIST_BUTTON, "a button label", "The list",
            MAX_LIST_BUTTON_LENGTH,
        ),
        "sections": sections,
    }


def clean_poll_content(value):
    """
    Validate and normalise a raw ``poll_content`` payload.

    Duplicate options are rejected rather than silently de-duplicated: two
    identical answers make the result meaningless, and a poll that quietly
    drops one of them is worse than one that says so.
    """
    if value in (None, "", {}, []):
        return {}
    if not isinstance(value, dict):
        raise ValidationError("Poll content must be an object.")

    question = _text(value.get("question"), "a question", "The poll", MAX_POLL_QUESTION_LENGTH)

    raw_options = value.get("options")
    if not isinstance(raw_options, list):
        raise ValidationError("Poll options must be a list.")

    options, seen = [], set()
    for index, raw_option in enumerate(raw_options, start=1):
        # Both a bare string and {"title": ...} are accepted: the builder posts
        # the former, and a caller mirroring the list row shape posts the latter.
        text = raw_option.get("title") if isinstance(raw_option, dict) else raw_option
        option = _text(text, "text", f"Poll option {index}", MAX_POLL_OPTION_LENGTH)
        folded = option.casefold()
        if folded in seen:
            raise ValidationError(f"Poll option {index} repeats {option!r}; every option must be different.")
        seen.add(folded)
        options.append(option)

    if len(options) < MIN_POLL_OPTIONS:
        raise ValidationError(f"A poll needs at least {MIN_POLL_OPTIONS} options ({len(options)} given).")
    if len(options) > MAX_POLL_OPTIONS:
        raise ValidationError(f"A poll allows at most {MAX_POLL_OPTIONS} options ({len(options)} given).")

    return {
        "question": question,
        "options": options,
        "allow_multiple": bool(value.get("allow_multiple")),
    }


# ---------------------------------------------------------------------------
# Degrading to something every connection can send
# ---------------------------------------------------------------------------

def list_rows(content) -> list:
    """Every row in a list, flattened across its sections, in display order."""
    return [row for section in (content or {}).get("sections", []) for row in section.get("rows", [])]


def as_options(*, list_content=None, poll_content=None) -> list:
    """
    A list's rows, or a poll's options, in ``core.buttons`` item shape.

    This is the whole of what makes lists and polls work on a connection that
    cannot send them natively. The returned items are ordinary REPLY buttons,
    so ``core.buttons.as_numbered_options`` renders them, ``PendingButtonPrompt``
    stores them, and ``automation.services.resolve_option_reply`` maps the
    number the contact types back to the row/option id -- with no second code
    path, and nothing downstream needing to know which it started as.

    A poll's options carry no author-supplied ids (a poll is answered by
    voting, not by id), so they are derived here from the option text and
    derived deterministically: the same poll always produces the same ids,
    which is what lets an automation rule point at one.
    """
    from core.buttons import REPLY

    if list_content:
        return [
            {"type": REPLY, "id": row["id"], "title": row["title"], "url": ""}
            for row in list_rows(list_content)
        ]

    if poll_content:
        taken: set = set()
        options = []
        for option in poll_content.get("options", []):
            option_id = _derive_id(option, taken)
            taken.add(option_id)
            options.append({"type": REPLY, "id": option_id, "title": option, "url": ""})
        return options

    return []


def list_as_text(body: str, content) -> str:
    """
    The plain-text form of a list message, for a connection with no list support.

    The header and footer are kept -- they are content the author wrote, and
    dropping them because the transport changed would send a different message
    than the one that was composed. Row descriptions are kept too, indented
    under their row, because a menu of titles alone often reads as a set of
    unexplained nouns.
    """
    from core.buttons import REPLY_INSTRUCTION

    content = content or {}
    parts = []
    if content.get("header"):
        parts.append(content["header"])
    if (body or "").strip():
        parts.append(body.strip())

    position = 0
    for section in content.get("sections", []):
        block = []
        # A single unnamed section is the common case and needs no heading.
        if section.get("title"):
            block.append(f"*{section['title']}*")
        for row in section.get("rows", []):
            position += 1
            block.append(f"{position}. {row['title']}")
            if row.get("description"):
                block.append(f"   {row['description']}")
        parts.append("\n".join(block))

    parts.append(REPLY_INSTRUCTION)
    if content.get("footer"):
        parts.append(content["footer"])
    return "\n\n".join(part for part in parts if part)


def poll_as_text(body: str, content) -> str:
    """
    The plain-text form of a poll, for a connection with no poll support.

    The multi-answer instruction differs from the single-answer one because
    the difference is real to the person answering: told only "reply with the
    option number", somebody who wants two options has no idea whether they
    may say so.
    """
    content = content or {}
    parts = []
    if (body or "").strip():
        parts.append(body.strip())
    if content.get("question"):
        parts.append(content["question"])

    options = content.get("options", [])
    parts.append("\n".join(f"{position}. {option}" for position, option in enumerate(options, start=1)))
    parts.append(
        "Please reply with the option numbers you choose, separated by commas."
        if content.get("allow_multiple")
        else "Please reply with the option number."
    )
    return "\n\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# What kind of message this content adds up to
# ---------------------------------------------------------------------------

TEXT = "TEXT"
BUTTON = "BUTTON"
LIST = "LIST"
POLL = "POLL"

#: The four ways a message can be composed, independent of whether it also
#: carries attachments. This is deliberately *derived* rather than stored: a
#: template that has had buttons since before lists existed is already a
#: BUTTON message, with no migration and no column that could disagree with
#: the content beside it.
CONTENT_KINDS = (TEXT, BUTTON, LIST, POLL)


def content_kind(obj) -> str:
    """
    Which of the four content kinds this template/campaign/message is.

    Takes a model instance or a plain dict -- ``Campaign.effective_message()``
    returns the latter, and asking "what kind is this campaign" has to work on
    the resolved content rather than on the campaign's own columns, which are
    empty whenever a template is selected.

    Order matters only because the three interactive kinds are mutually
    exclusive: a serializer refuses a payload carrying more than one, so this
    never has to choose between two that are genuinely both present.
    """
    read = obj.get if isinstance(obj, dict) else lambda name, default=None: getattr(obj, name, default)

    if read("poll_content", None):
        return POLL
    if read("list_content", None):
        return LIST
    if read("buttons", None):
        return BUTTON
    return TEXT


def exclusive_content_error(*, buttons=None, list_content=None, poll_content=None) -> str:
    """
    The message to reject a payload that asks for two interactive kinds at once.

    Returns "" when the content is fine. WhatsApp sends buttons, lists and
    polls as three different kinds of message and there is no envelope that
    carries two, so this is a real constraint rather than a simplification --
    and catching it on save is what stops a campaign being launched against a
    message that can only ever be delivered as one of the two things asked for.
    """
    chosen = [
        name
        for name, value in (("buttons", buttons), ("a list", list_content), ("a poll", poll_content))
        if value
    ]
    if len(chosen) < 2:
        return ""
    return (
        f"A message can carry {chosen[0]} or {chosen[1]}, not both -- WhatsApp sends them as "
        "different kinds of message."
    )
