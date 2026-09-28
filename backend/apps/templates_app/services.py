from django.utils import timezone

from core.media import resolve_media_items, spec_from_object

from .models import VARIABLE_RE, MessageTemplate, variable_name

#: Contact columns that are variables by their own name. Everything else a
#: contact carries lives in `custom_fields` and becomes a variable too -- see
#: build_render_context -- so this list is the set of *built-in* fields, not
#: the set of usable variables.
_CONTACT_FIELDS = ("name", "phone", "email", "company", "country")

#: Variables that are not stored on the contact at all: they are computed at
#: render time from a field that is, or from the clock. Kept here rather than
#: hard-coded into the renderer so that `available_variables` and the renderer
#: cannot disagree about what exists.
#:
#: `first_name`/`last_name` split the one name field people actually have.
#: Nobody stores a family name separately and asking them to would be the
#: wrong fix; a single-word name yields a first name and an empty last name,
#: which is the honest answer and is what the fallback in `variables` is for.
COMPUTED_VARIABLES = ("first_name", "last_name", "number", "date", "time")

#: Contacts examined when working out which custom fields exist. Enough to
#: cover every column a real import produced without turning an autocomplete
#: into a table scan.
CUSTOM_FIELD_SAMPLE = 500


def _name_parts(full_name: str) -> tuple:
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def build_render_context(contact, *, now=None) -> dict:
    """
    Merge a contact's built-in fields, computed fields and custom fields into
    one lookup, case-insensitive by key.

    Precedence is deliberate and runs bottom-up: a custom field wins over a
    computed one, which wins over a built-in. Somebody who imported a CSV
    column called "date" meant *their* date, and silently substituting today's
    would be the platform overruling the customer's own data.

    `now` is injectable so a render is reproducible in a test; production
    callers leave it alone and get the tenant's local time, which is what
    `{{date}}` has to mean -- a date stamped in UTC is wrong by a day for
    every customer east of London for part of the day.
    """
    context = {}
    for field in _CONTACT_FIELDS:
        context[field.lower()] = getattr(contact, field, "") or ""

    moment = timezone.localtime(now) if now is not None else timezone.localtime()
    first, last = _name_parts(context.get("name", ""))
    context.update({
        "first_name": first,
        "last_name": last,
        # An alias, not a second field: "number" is what most people type for
        # a phone number, and a variable that silently renders empty because
        # the platform calls it something else is indistinguishable from a bug.
        "number": context.get("phone", ""),
        "date": moment.strftime("%d %b %Y"),
        "time": moment.strftime("%I:%M %p").lstrip("0"),
    })

    for key, value in (getattr(contact, "custom_fields", None) or {}).items():
        context[str(key).lower()] = value
    return context


def render_text(text: str, context: dict, fallbacks: dict) -> str:
    """
    Substitute every ``[Variable]`` and ``{{variable}}`` in one string.

    A name with no value in the context falls back to `fallbacks`, and then to
    the empty string. Rendering to empty rather than leaving the placeholder
    is the older behaviour and is kept: a customer receiving "Hello [Name]" is
    worse than one receiving "Hello", and the composer's preview is where an
    author sees which variables have no data behind them.
    """
    def replace(match):
        variable = variable_name(match)
        key = variable.lower()
        if key in context and context[key] not in (None, ""):
            return str(context[key])
        # Fallbacks are keyed as the author typed them, so both the exact
        # spelling and the folded one are tried -- a template written with
        # {{order_id}} and a fallback saved under "OrderID" is the same
        # variable to everyone except a dict lookup.
        fallbacks_or_empty = fallbacks or {}
        if variable in fallbacks_or_empty:
            return str(fallbacks_or_empty[variable])
        for name, value in fallbacks_or_empty.items():
            if str(name).lower() == key:
                return str(value)
        return ""

    return VARIABLE_RE.sub(replace, text or "")


def render_interactive(content: dict, context: dict, fallbacks: dict, *, kind: str) -> dict:
    """
    Personalize the strings inside a list or a poll.

    A menu row reading "Reorder {{last_product}}" is one of the more useful
    things a list can do, and a poll option carrying a variable is no stranger
    than a body carrying one. Ids are deliberately *not* rendered: an id is
    what automation matches on, and a per-recipient id would point every rule
    at nothing.
    """
    if not content:
        return {}

    def text(value):
        return render_text(value, context, fallbacks)

    if kind == "list":
        return {
            **content,
            "header": text(content.get("header", "")),
            "footer": text(content.get("footer", "")),
            "button": text(content.get("button", "")),
            "sections": [
                {
                    "title": text(section.get("title", "")),
                    "rows": [
                        {
                            # `.get` rather than `[...]`: the live composer
                            # renders content that has never been through
                            # `clean_list_content`, so a row being typed has
                            # no id yet. Saved content always does.
                            "id": row.get("id", ""),
                            "title": text(row.get("title", "")),
                            "description": text(row.get("description", "")),
                        }
                        for row in section.get("rows", [])
                    ],
                }
                for section in content.get("sections", [])
            ],
        }

    return {
        **content,
        "question": text(content.get("question", "")),
        "options": [text(option) for option in content.get("options", [])],
    }


def render_buttons(buttons, context: dict, fallbacks: dict) -> list:
    """
    Personalize button labels, leaving ids and urls alone.

    Same rule as the list rows above, and for the same reason: the id is the
    automation's handle on the button. A url is left alone because rewriting
    one per recipient is the link tracker's job, and it already does it on the
    body text -- see apps.tracking.services.rewrite.
    """
    return [
        {**button, "title": render_text(button.get("title", ""), context, fallbacks)}
        for button in (buttons or [])
    ]


def render_template(template: MessageTemplate, contact) -> dict:
    context = build_render_context(contact)
    return {
        "body": render_text(template.body, context, template.variables),
        "caption": render_text(template.caption, context, template.variables),
        "media_url": template.media_url,
        "media_items": resolve_media_items(spec_from_object(template)),
        "filename": template.filename,
        "message_type": template.message_type,
        "buttons": render_buttons(template.buttons, context, template.variables),
        "list_content": render_interactive(template.list_content, context, template.variables, kind="list"),
        "poll_content": render_interactive(template.poll_content, context, template.variables, kind="poll"),
    }


def available_variables(tenant_id) -> dict:
    """
    Every variable an author on this workspace can actually use.

    Two groups, and the second is the point of this function. The built-ins
    are the same everywhere. The custom ones are read from the workspace's own
    contacts -- whatever columns their imports happened to carry -- so a
    business whose CSV had `order_id` and `amount` is offered those by name
    instead of being handed a fixed list and left to guess whether their own
    data is reachable.

    Sampled rather than aggregated across the whole table: `custom_fields` is
    JSON, the keys are not indexed, and this answers a composer's autocomplete,
    where "the fields your recent contacts have" is both the useful answer and
    a cheap one.
    """
    from apps.contacts.models import Contact

    custom: list = []
    rows = (
        Contact.objects.filter(tenant_id=tenant_id)
        .exclude(custom_fields={})
        .order_by("-id")
        .values_list("custom_fields", flat=True)[:CUSTOM_FIELD_SAMPLE]
    )
    for fields in rows:
        for key in (fields or {}):
            name = str(key).strip()
            if name and name not in custom:
                custom.append(name)

    return {
        "builtin": list(_CONTACT_FIELDS) + list(COMPUTED_VARIABLES),
        "custom": sorted(custom, key=str.lower),
    }
