"""
Uploading an attachment, so a caller has a `media_url` to send.

An integration usually has bytes, not a public URL -- a PDF invoice its own
server just generated, sitting behind that server's authentication. This gives
it somewhere to put them and hands back a URL the provider can fetch.

The validation is `core.uploads.validate_upload`, the same two-layer check the
dashboard's uploader uses: the stored filename is ours and the content type is
derived from the bytes, so nothing an uploader writes reaches the path or is
believed about the file. Duplicating that logic here would be duplicating the
one defence that stops a .php named .jpg.
"""

import logging

from django.conf import settings
from rest_framework.parsers import FormParser, MultiPartParser

from apps.media_library.models import MediaAsset
from core.uploads import UploadRejected, validate_upload

from .. import scopes
from ..errors import INVALID_REQUEST, PublicAPIError
from .base import PublicAPIView

logger = logging.getLogger("sendstack")


class MediaUploadView(PublicAPIView):
    """``POST /media`` -- multipart, one file per request."""

    required_scopes = [scopes.MEDIA_UPLOAD]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        upload = request.FILES.get("file") or (request.FILES.getlist("files") or [None])[0]
        if upload is None:
            raise PublicAPIError(
                INVALID_REQUEST, "Attach a file in the multipart field 'file'.",
            )

        limit = int(getattr(settings, "MEDIA_ASSET_MAX_UPLOAD_BYTES", 20 * 1024 * 1024))
        if (upload.size or 0) > limit:
            raise PublicAPIError(
                INVALID_REQUEST,
                f"That file is larger than the {limit // (1024 * 1024)} MB limit.",
                status_code=413,
            )

        try:
            _extension, content_type, kind, stored_name, display_name = validate_upload(upload)
        except UploadRejected as exc:
            raise PublicAPIError(INVALID_REQUEST, str(exc)) from exc

        upload.name = stored_name
        asset = MediaAsset.objects.create(
            tenant=request.auth.tenant,
            uploaded_by=request.auth.owner,
            file=upload,
            original_filename=display_name,
            content_type=content_type,
            size_bytes=upload.size or 0,
            kind=kind,
        )

        return self.ok(
            {
                "id": asset.pk,
                "url": self._absolute_url(asset, request),
                "kind": asset.kind,
                "filename": asset.original_filename,
                "content_type": asset.content_type,
                "size_bytes": asset.size_bytes,
            },
            status_code=201,
        )

    @staticmethod
    def _absolute_url(asset, request):
        """
        An absolute URL, always.

        The provider fetches this from its own process -- Baileys from inside
        the qr-bridge container, Meta from the public internet -- so a relative
        `/media/...` path resolves against the wrong host and the send fails
        with a media error that looks like a provider fault. A configured
        public origin wins over the request's, because behind a proxy the
        request host can be an internal name nothing outside can reach.
        """
        if not asset.file:
            return ""
        url = asset.file.url
        if getattr(settings, "MEDIA_PUBLIC_BASE_URL", ""):
            return f"{settings.MEDIA_PUBLIC_BASE_URL}{url}"
        return request.build_absolute_uri(url)
