"""DRF glue for ``list_content`` / ``poll_content``. See ``core.interactive``."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .buttons import MAX_BODY_LENGTH
from .interactive import clean_list_content, clean_poll_content, exclusive_content_error


class ListContentField(serializers.JSONField):
    """A validated list/menu payload, normalised on the way in."""

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        try:
            return clean_list_content(data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


class PollContentField(serializers.JSONField):
    """A validated poll payload, normalised on the way in."""

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        try:
            return clean_poll_content(data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


def validate_interactive_content(attrs, instance=None, *, body_field="body"):
    """
    Check buttons/list/poll together, the way the send path will read them.

    Called from a content serializer's ``validate()`` after
    ``core.button_fields.validate_button_body``. Two rules, both of which are
    WhatsApp's rather than ours:

      * only one interactive kind per message -- there is no envelope that
        carries a list *and* a poll;
      * a list needs body text, exactly as a button message does. The list
        itself is behind a tap, so a message with no body is a bare button
        with nothing explaining what it opens.

    A poll deliberately does not require body text: its question *is* the
    message, and WhatsApp renders a poll as a bubble of its own.

    ``attrs`` may be a partial PATCH, so every value falls back to the stored
    one -- otherwise clearing a list by PATCHing only ``poll_content`` would
    be judged against a list this request never saw.
    """
    def current(name, default):
        if name in attrs:
            return attrs[name]
        return getattr(instance, name, default) if instance is not None else default

    buttons = current("buttons", [])
    list_content = current("list_content", {})
    poll_content = current("poll_content", {})

    conflict = exclusive_content_error(
        buttons=buttons, list_content=list_content, poll_content=poll_content
    )
    if conflict:
        raise serializers.ValidationError({"list_content": conflict})

    if list_content:
        text = (current(body_field, "") or "").strip()
        if not text:
            raise serializers.ValidationError({
                body_field: "A list message needs body text -- the menu opens from a button under it."
            })
        if len(text) > MAX_BODY_LENGTH:
            raise serializers.ValidationError({
                body_field: (
                    f"A list message is limited to {MAX_BODY_LENGTH} characters ({len(text)} given)."
                )
            })

    return attrs
