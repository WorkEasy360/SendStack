import logging

from django.conf import settings
from rest_framework import mixins, status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.bulk import BulkActionMixin

from .models import MediaAsset, classify
from .serializers import MediaAssetSerializer

logger = logging.getLogger("multisender")

# nginx is configured with `client_max_body_size 20M`, so anything larger is
# rejected by the proxy before Django sees it -- refuse it here with a readable
# message rather than letting the browser surface a bare 413.
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_REQUEST = 30


def max_upload_bytes():
    return int(getattr(settings, "MEDIA_ASSET_MAX_UPLOAD_BYTES", DEFAULT_MAX_BYTES))


def _human(size):
    return f"{size / (1024 * 1024):.0f} MB"


class MediaAssetViewSet(
    BulkActionMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    Upload store for message attachments.

    ``POST`` accepts **many** files in one multipart request (repeat the
    ``files`` field), which is what lets the composer hand over a whole
    selection at once instead of one round trip per file. A single ``file``
    field is accepted too, so a one-file upload needs no special case.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = MediaAssetSerializer
    # Multipart for the upload itself; JSON so the bulk endpoint (which the
    # viewset shares) still takes an ordinary JSON body.
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filterset_fields = ["kind"]
    search_fields = ["original_filename"]

    bulk_object_label = "file"
    bulk_actions = {"delete": "_bulk_delete"}

    def get_queryset(self):
        return MediaAsset.objects.filter(tenant=self.request.user.tenant)

    def create(self, request, *args, **kwargs):
        uploads = request.FILES.getlist("files") + request.FILES.getlist("file")
        if not uploads:
            return Response({"detail": "Attach at least one file."}, status=status.HTTP_400_BAD_REQUEST)
        if len(uploads) > MAX_FILES_PER_REQUEST:
            return Response(
                {"detail": f"At most {MAX_FILES_PER_REQUEST} files can be uploaded at once ({len(uploads)} given)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        limit = max_upload_bytes()
        oversized = [upload.name for upload in uploads if upload.size > limit]
        if oversized:
            # All-or-nothing on size: a half-applied upload is far more
            # confusing in the picker than being told which files to drop.
            return Response(
                {"detail": f"These files are larger than {_human(limit)}: {', '.join(oversized)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assets = [
            MediaAsset.objects.create(
                tenant=request.user.tenant,
                uploaded_by=request.user,
                file=upload,
                original_filename=(upload.name or "")[:255],
                content_type=(getattr(upload, "content_type", "") or "")[:150],
                size_bytes=upload.size or 0,
                kind=classify(upload.name, getattr(upload, "content_type", "") or ""),
            )
            for upload in uploads
        ]
        logger.info("Tenant %s uploaded %s media asset(s).", request.user.tenant_id, len(assets))
        serializer = self.get_serializer(assets, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        # Drop the bytes as well as the row; message content keeps its own copy
        # of the URL, so nothing dangles in the database (see MediaAsset docs).
        stored = instance.file
        instance.delete()
        if stored:
            stored.delete(save=False)

    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)
