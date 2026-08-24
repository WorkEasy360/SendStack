"""DRF glue for the ``buttons`` list. See ``core.buttons`` for the schema."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .buttons import MAX_BODY_LENGTH, clean_buttons


class ButtonsField(serializers.JSONField):
    """A validated list of interactive buttons, normalised on the way in."""

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        try:
            return clean_buttons(data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


def validate_button_body(buttons, body, *, field="body"):
    """
    Check the text that will accompany these buttons.

    An interactive message has no "media only" form: WhatsApp requires body
    text alongside the buttons, and caps it well below a plain text message's
    limit. Content serializers call this from ``validate()`` so a template that
    could never send is rejected on save rather than at send time.
    """
    if not buttons:
        return
    text = (body or "").strip()
    if not text:
        raise serializers.ValidationError(
            {field: "A message with buttons needs body text -- WhatsApp shows the buttons underneath it."}
        )
    if len(text) > MAX_BODY_LENGTH:
        raise serializers.ValidationError(
            {field: f"A message with buttons is limited to {MAX_BODY_LENGTH} characters ({len(text)} given)."}
        )
