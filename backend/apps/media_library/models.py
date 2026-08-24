import os
import uuid

from django.conf import settings
from django.db import models

from core.media import DOCUMENT, IMAGE, VIDEO

# Extension -> attachment kind. WhatsApp treats "anything that isn't a picture
# or a clip" as a document, so DOCUMENT is the fallback rather than a rejection.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".3gp", ".mkv", ".avi", ".webm", ".m4v"}


def classify(filename: str, content_type: str = "") -> str:
    extension = os.path.splitext(filename or "")[1].lower()
    if extension in IMAGE_EXTENSIONS:
        return IMAGE
    if extension in VIDEO_EXTENSIONS:
        return VIDEO
    if content_type.startswith("image/"):
        return IMAGE
    if content_type.startswith("video/"):
        return VIDEO
    return DOCUMENT


def media_asset_upload_path(instance, filename):
    # A UUID stem keeps two uploads of "invoice.pdf" from colliding while the
    # original name is preserved separately for the WhatsApp download label.
    extension = os.path.splitext(filename)[1].lower()[:16]
    return f"message_media/tenant_{instance.tenant_id}/{uuid.uuid4().hex}{extension}"


class MediaAsset(models.Model):
    """
    One uploaded image / video / document, reusable across any number of
    templates, campaigns, automation rules and inbox replies.

    Nothing references an asset by foreign key: message content stores the
    resolved URL (see ``core.media``), exactly as it did when the only way to
    attach media was to paste a link. That keeps an already-sent message's
    attachment stable even if the asset is later deleted from the library, and
    means externally hosted URLs and uploaded files stay interchangeable.
    """

    class Kind(models.TextChoices):
        IMAGE = IMAGE, "Image"
        VIDEO = VIDEO, "Video"
        DOCUMENT = DOCUMENT, "Document"

    tenant = models.ForeignKey("tenants.Tenant", on_delete=models.CASCADE, related_name="media_assets")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="media_assets"
    )

    file = models.FileField(upload_to=media_asset_upload_path)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=150, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.DOCUMENT)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["tenant", "kind"])]

    def __str__(self):
        return self.original_filename or self.file.name
