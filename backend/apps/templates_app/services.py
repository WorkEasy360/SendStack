from core.media import resolve_media_items, spec_from_object

from .models import VARIABLE_RE, MessageTemplate

_CONTACT_FIELDS = ("name", "phone", "email", "country")


def build_render_context(contact) -> dict:
    """Merge a contact's built-in fields and custom fields into one lookup, case-insensitive by key."""
    context = {}
    for field in _CONTACT_FIELDS:
        context[field.lower()] = getattr(contact, field, "") or ""
    for key, value in (contact.custom_fields or {}).items():
        context[str(key).lower()] = value
    return context


def render_text(text: str, context: dict, fallbacks: dict) -> str:
    def replace(match):
        variable = match.group(1)
        key = variable.lower()
        if key in context and context[key] not in (None, ""):
            return str(context[key])
        return str(fallbacks.get(variable, ""))

    return VARIABLE_RE.sub(replace, text or "")


def render_template(template: MessageTemplate, contact) -> dict:
    context = build_render_context(contact)
    return {
        "body": render_text(template.body, context, template.variables),
        "caption": render_text(template.caption, context, template.variables),
        "media_url": template.media_url,
        "media_items": resolve_media_items(spec_from_object(template)),
        "filename": template.filename,
        "message_type": template.message_type,
    }
