"""DRF glue for the ``buttons`` list. See ``core.buttons`` for the schema."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .buttons import MAX_BODY_LENGTH, body_with_text_buttons, clean_buttons, plan_delivery


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

    The cap is measured against the body *plus* whatever the button list adds
    to it. A mixed list has its extra kinds written into the body on the wire
    (see ``core.buttons.plan_delivery``), so a body that fits on its own and a
    long link beneath it is exactly the message Meta would refuse -- and it
    would refuse it per recipient, hours after the template was saved.
    """
    if not buttons:
        return
    text = (body or "").strip()
    if not text:
        raise serializers.ValidationError(
            {field: "A message with buttons needs body text -- WhatsApp shows the buttons underneath it."}
        )

    sent = body_with_text_buttons(text, plan_delivery(buttons)[1])
    if len(sent) > MAX_BODY_LENGTH:
        extra = len(sent) - len(text)
        raise serializers.ValidationError({
            field: (
                f"A message with buttons is limited to {MAX_BODY_LENGTH} characters "
                f"({len(sent)} given"
                + (f", including {extra} for the buttons sent as text" if extra else "")
                + ")."
            )
        })
