"""DRF glue for the ``media_items`` list. See ``core.media`` for the schema."""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .media import clean_media_items, legacy_fields


class MediaItemsField(serializers.JSONField):
    """A validated list of attachments, normalised on the way in."""

    def to_internal_value(self, data):
        data = super().to_internal_value(data)
        try:
            return clean_media_items(data)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc


def apply_media_items(attrs):
    """
    Keep the single-attachment columns in step with ``media_items[0]``.

    Called from each content serializer's ``validate()`` *before* its own
    media checks run. Every reader that predates multi-attachment support --
    campaign launch validation, CSV exports, the inbox bubble, the provider
    call's fallback path -- reads ``media_url``/``message_type``/``filename``,
    so those have to keep describing the first attachment or the feature would
    silently change what those callers see.

    Only touched when the caller actually sent ``media_items``: a client that
    still posts ``media_url`` alone behaves exactly as it did before.
    """
    if "media_items" not in attrs:
        return attrs

    items = attrs["media_items"]
    if items:
        attrs.update(legacy_fields(items))
    else:
        # Attachments were explicitly cleared. Blank the mirror too, unless
        # this request also set a media_url by hand (in which case that URL is
        # the caller's intent and resolve_media_items falls back to it).
        attrs.setdefault("media_url", "")
        attrs.setdefault("filename", "")
    return attrs
