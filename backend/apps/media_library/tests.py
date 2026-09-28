import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from core.media import (
    clean_media_items,
    legacy_fields,
    resolve_media_items,
    spec_from_object,
)
from core.sample_media import upload as sample_upload

from .models import MediaAsset, classify


class MediaItemSchemaTests(TestCase):
    def test_normalises_and_drops_unknown_keys(self):
        cleaned = clean_media_items([
            {"url": "  https://cdn.example.com/x.png  ", "type": "image", "colour": "red"},
        ])

        self.assertEqual(cleaned, [{"url": "https://cdn.example.com/x.png", "type": "IMAGE", "filename": "", "caption": ""}])

    def test_blank_input_is_an_empty_list(self):
        for value in (None, "", []):
            self.assertEqual(clean_media_items(value), [])

    def test_rejects_missing_url(self):
        with self.assertRaises(ValidationError):
            clean_media_items([{"type": "IMAGE"}])

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValidationError):
            clean_media_items([{"url": "https://cdn.example.com/x.png", "type": "AUDIO"}])

    def test_document_requires_a_filename(self):
        with self.assertRaises(ValidationError):
            clean_media_items([{"url": "https://cdn.example.com/x.pdf", "type": "DOCUMENT"}])

    def test_rejects_more_than_the_cap(self):
        items = [{"url": f"https://cdn.example.com/{n}.png", "type": "IMAGE"} for n in range(5)]
        with self.assertRaises(ValidationError):
            clean_media_items(items, max_items=4)

    def test_legacy_fields_describe_the_first_item(self):
        items = clean_media_items([
            {"url": "https://cdn.example.com/a.pdf", "type": "DOCUMENT", "filename": "a.pdf"},
            {"url": "https://cdn.example.com/b.png", "type": "IMAGE"},
        ])

        self.assertEqual(
            legacy_fields(items),
            {
                "message_type": "DOCUMENT",
                "media_url": "https://cdn.example.com/a.pdf",
                "filename": "a.pdf",
                "caption": "",
            },
        )

    def test_legacy_fields_of_nothing_touch_nothing(self):
        self.assertEqual(legacy_fields([]), {})


class ResolveMediaItemsTests(TestCase):
    """A row written before multi-attachment support must still send its media."""

    def test_falls_back_to_the_single_media_url(self):
        spec = {"message_type": "IMAGE", "media_url": "https://cdn.example.com/x.png", "filename": "", "caption": "Hi"}

        self.assertEqual(
            resolve_media_items(spec),
            [{"url": "https://cdn.example.com/x.png", "type": "IMAGE", "filename": "", "caption": "Hi"}],
        )

    def test_media_items_wins_over_the_fallback(self):
        spec = {
            "media_items": [{"url": "https://cdn.example.com/new.png", "type": "IMAGE", "filename": "", "caption": ""}],
            "message_type": "IMAGE",
            "media_url": "https://cdn.example.com/old.png",
        }

        self.assertEqual([item["url"] for item in resolve_media_items(spec)], ["https://cdn.example.com/new.png"])

    def test_text_messages_have_no_attachments(self):
        self.assertEqual(resolve_media_items({"message_type": "TEXT", "media_url": ""}), [])

    def test_a_text_type_with_a_stray_url_still_sends_nothing(self):
        self.assertEqual(resolve_media_items({"message_type": "TEXT", "media_url": "https://cdn.example.com/x.png"}), [])

    def test_spec_from_object_reads_a_model_instance(self):
        class Row:
            media_items = []
            message_type = "VIDEO"
            media_url = "https://cdn.example.com/clip.mp4"
            filename = ""
            caption = ""

        self.assertEqual([item["type"] for item in resolve_media_items(spec_from_object(Row()))], ["VIDEO"])


class ClassifyTests(TestCase):
    def test_classifies_by_extension(self):
        self.assertEqual(classify("photo.JPG"), "IMAGE")
        self.assertEqual(classify("clip.mp4"), "VIDEO")
        self.assertEqual(classify("invoice.pdf"), "DOCUMENT")

    def test_falls_back_to_the_content_type(self):
        self.assertEqual(classify("noextension", "image/png"), "IMAGE")

    def test_anything_unrecognised_is_a_document(self):
        self.assertEqual(classify("data.xyz", "application/octet-stream"), "DOCUMENT")


class MediaAssetApiTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

        self.tenant = Tenant.objects.create(name="Acme", slug="acme-media", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-media-owner", password="x", tenant=self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _upload(self, *files):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.post("/api/v1/media/assets/", {"files": list(files)}, format="multipart")

    def test_uploads_several_files_in_one_request(self):
        response = self._upload(
            sample_upload("a.png", content_type="image/png"),
            sample_upload("b.mp4", content_type="video/mp4"),
            sample_upload("c.pdf", content_type="application/pdf"),
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual([asset["kind"] for asset in body], ["IMAGE", "VIDEO", "DOCUMENT"])
        self.assertEqual([asset["original_filename"] for asset in body], ["a.png", "b.mp4", "c.pdf"])
        self.assertTrue(all(asset["url"].startswith("http") for asset in body))
        self.assertEqual(MediaAsset.objects.filter(tenant=self.tenant).count(), 3)

    def test_a_single_file_field_still_works(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.post(
                "/api/v1/media/assets/",
                {"file": sample_upload("solo.png", content_type="image/png")},
                format="multipart",
            )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()), 1)

    def test_rejects_a_request_with_no_files(self):
        response = self.client.post("/api/v1/media/assets/", {}, format="multipart")

        self.assertEqual(response.status_code, 400)

    def test_rejects_oversized_files_without_storing_any_of_them(self):
        with override_settings(MEDIA_ROOT=self.media_root, MEDIA_ASSET_MAX_UPLOAD_BYTES=60):
            response = self.client.post(
                "/api/v1/media/assets/",
                {"files": [
                    sample_upload("small.gif", content_type="image/gif"),
                    sample_upload("big.png", content_type="image/png"),
                ]},
                format="multipart",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("big.png", response.json()["detail"])
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_listing_is_scoped_to_the_callers_tenant(self):
        other = Tenant.objects.create(name="Other", slug="other-media", kind=Tenant.Kind.CUSTOMER)
        MediaAsset.objects.create(tenant=other, original_filename="theirs.png", kind=MediaAsset.Kind.IMAGE)
        self._upload(sample_upload("mine.png", content_type="image/png"))

        response = self.client.get("/api/v1/media/assets/")

        self.assertEqual([a["original_filename"] for a in response.json()["results"]], ["mine.png"])

    def test_bulk_delete_removes_every_selected_asset(self):
        created = self._upload(
            sample_upload("a.png", content_type="image/png"),
            sample_upload("b.png", content_type="image/png"),
        ).json()

        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.post(
                "/api/v1/media/assets/bulk/", {"action": "delete", "ids": [a["id"] for a in created]}, format="json"
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_bulk_delete_reports_an_id_it_cannot_reach(self):
        created = self._upload(sample_upload("a.png", content_type="image/png")).json()

        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.post(
                "/api/v1/media/assets/bulk/", {"action": "delete", "ids": [created[0]["id"], 999999]}, format="json"
            )

        self.assertEqual(response.status_code, 207)
        body = response.json()
        self.assertEqual((body["succeeded"], body["failed"]), (1, 1))
        self.assertEqual(body["errors"], [{"id": 999999, "detail": "Not found."}])
