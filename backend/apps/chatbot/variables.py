"""
What the bot knows about the person it is talking to, and how it says it back.

Two scopes, and the difference matters:

* **Conversation** -- ``ChatbotSession.variables``. Things true of this chat:
  the size they asked for, the city they named, which branch of a flow they
  took. Gone when the conversation is.
* **Customer** -- ``Contact.custom_fields``, which already exists and which
  campaigns and templates already render from. Things true of the person: their
  email, their company, their order id.

A ``SET_VARIABLE`` action names its scope. Storing everything on the contact
would leak one conversation's answers into the next one's greeting; storing
everything on the session would lose an email address the moment the chat
closed.

Rendering reuses ``templates_app.render_text`` -- the same substitution
campaigns and automation rules use -- so ``{{name}}`` means the same thing
everywhere in the product and there is one escaping story rather than two.
"""

import logging
import re

logger = logging.getLogger("sendstack")

#: Longest value a variable may hold. A bot that writes an entire model reply
#: into a variable and then renders it into the next three messages is a
#: prompt-injection amplifier as well as a storage problem.
MAX_VALUE_CHARS = 500

#: Most variables one session may hold. Reached, further sets are refused
#: rather than silently dropped, because a flow looping on a SET_VARIABLE node
#: would otherwise grow one JSON column without limit.
MAX_VARIABLES = 100

#: Names the platform owns. A flow cannot overwrite these, because they are
#: read from the contact and the conversation and are meant to be trustworthy:
#: a bot that could set ``{{phone}}`` could make a later message address
#: somebody else's number in a template that renders it.
RESERVED = frozenset({
    "name", "first_name", "last_name", "phone", "number", "email", "company",
    "date", "time", "language", "last_message",
})

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


#: Marks a session as a preview rather than a real conversation. Kept with the
#: variables, under the same reserved ``__`` prefix as ``__awaiting``, because
#: it travels with exactly the same thing and needs no column of its own --
#: ``normalise_name`` cannot produce a leading underscore, so no author can set
#: it and no message can render it.
SIMULATION_KEY = "__simulation"


def is_simulation(session) -> bool:
    """
    Is this session a preview in the builder rather than a customer?

    Read from the session rather than passed down the call chain because the
    things that must know -- the sender, two of the actions -- are reached from
    a dozen places each, and a flag threaded through every one of them is a
    flag somebody eventually forgets to pass. Defaulting to False means a real
    conversation is never mistaken for a preview, which is the direction that
    matters: the failure mode of the opposite default is a bot that silently
    stops talking to customers.
    """
    return bool((getattr(session, "variables", None) or {}).get(SIMULATION_KEY))


class VariableError(ValueError):
    """Raised for a variable name or value that cannot be stored."""


def normalise_name(name: str) -> str:
    """
    The stored form of a variable name: lowercase, underscores, no braces.

    People write ``{{Order ID}}`` in a form field and expect it to be the same
    thing as ``order_id``, so the normalisation happens once, here, rather than
    at each of the four places a name is read.
    """
    cleaned = (name or "").strip().strip("{}").strip()
    cleaned = re.sub(r"[\s\-.]+", "_", cleaned).lower()
    cleaned = re.sub(r"[^a-z0-9_]", "", cleaned)
    if not _NAME.match(cleaned):
        raise VariableError(
            "A variable name must start with a letter and use only letters, numbers and underscores."
        )
    return cleaned


def coerce_value(value) -> str:
    """Everything is stored as a bounded string -- a JSON column with mixed types is a rendering surprise."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value)
    return text[:MAX_VALUE_CHARS]


def set_session_variable(session, name: str, value) -> str:
    """
    Remember one thing for the rest of this conversation. Returns the stored name.

    Writes through ``.variables`` on the passed instance *and* persists, so the
    caller's in-memory session stays correct for the rest of the turn -- a flow
    that sets a variable and then renders it in the very next node is the
    common case, not the exception.
    """
    key = normalise_name(name)
    if key in RESERVED:
        raise VariableError(f"'{key}' is a built-in variable and cannot be overwritten.")

    variables = dict(session.variables or {})
    if key not in variables and len(variables) >= MAX_VARIABLES:
        raise VariableError(f"This conversation already holds {MAX_VARIABLES} variables.")

    variables[key] = coerce_value(value)
    session.variables = variables
    session.save(update_fields=["variables", "updated_at"])
    return key


def set_contact_variable(contact, name: str, value) -> str:
    """Remember one thing about the person, for every future conversation."""
    key = normalise_name(name)
    if key in RESERVED:
        raise VariableError(f"'{key}' is a built-in variable and cannot be overwritten.")

    fields = dict(contact.custom_fields or {})
    if key not in fields and len(fields) >= MAX_VARIABLES:
        raise VariableError(f"This contact already holds {MAX_VARIABLES} custom fields.")

    fields[key] = coerce_value(value)
    contact.custom_fields = fields
    contact.save(update_fields=["custom_fields", "updated_at"])
    return key


def build_context(contact, session=None, *, last_message: str = "") -> dict:
    """
    Everything ``{{...}}`` can resolve to, in precedence order.

    Bottom-up, most specific last: the contact's built-in and custom fields
    first (via the platform's own renderer, so ``{{first_name}}`` and
    ``{{date}}`` behave exactly as they do in a campaign), then this
    conversation's own variables, which win.

    A session variable winning over a contact field is deliberate. If the
    customer just said their city is Pune and their contact record says Mumbai,
    the thing they said thirty seconds ago is the one to answer with.
    """
    from apps.templates_app.services import build_render_context

    context = build_render_context(contact)
    if session is not None:
        context["language"] = session.detected_language or ""
        for key, value in (session.variables or {}).items():
            context[str(key).lower()] = value
    if last_message:
        context["last_message"] = last_message[:MAX_VALUE_CHARS]
    context.setdefault("last_message", "")
    return context


def render(text: str, context: dict) -> str:
    """
    Substitute every variable in one string.

    Delegates to the platform's renderer rather than reimplementing it, which
    is what keeps ``[Name]`` and ``{{name}}`` both working here exactly as they
    do in a template -- and means an unknown variable renders empty rather than
    printing its own braces at a customer.
    """
    if not text:
        return ""
    from apps.templates_app.services import render_text

    try:
        return render_text(text, context, {})
    except Exception:  # noqa: BLE001 - a rendering failure must not swallow the message
        logger.exception("Could not render a chatbot message; sending it unrendered.")
        return text


def declared_names(text: str) -> list:
    """Every ``{{variable}}`` mentioned in a string, for the composer's hints."""
    return sorted({match.lower() for match in re.findall(r"\{\{\s*([a-zA-Z0-9_ .-]+?)\s*\}\}", text or "")})
