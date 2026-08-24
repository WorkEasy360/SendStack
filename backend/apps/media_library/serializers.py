from django.conf import settings
from rest_framework import serializers

from .models import MediaAsset


class MediaAssetSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = MediaAsset
        fields = ("id", "url", "kind", "original_filename", "content_type", "size_bytes", "created_at")
        read_only_fields = fields

    def get_url(self, obj):
        if not obj.file:
            return ""
        url = obj.file.url
        # Absolute, because the WhatsApp provider fetches this URL from its own
        # process -- a "/media/..." path would resolve against the wrong host.
        # A configured public origin wins over the request one: behind a proxy
        # the request host can be an internal name nothing outside can reach.
        if settings.MEDIA_PUBLIC_BASE_URL:
            return f"{settings.MEDIA_PUBLIC_BASE_URL}{url}"
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def to_media_item(self, obj):
        """The ``media_items`` entry that attaches this asset to a message."""
        return {
            "url": self.get_url(obj),
            "type": obj.kind,
            "filename": obj.original_filename,
            "caption": "",
        }
