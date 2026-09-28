"""
Upload security: what the media endpoint will and will not store.

Everything here exists because ``/media/`` is served off this deployment's own
origin. A file a browser is willing to treat as a document, stored there, is
stored XSS against every logged-in user of the site -- so these are regression
tests for a vulnerability, not preference checks about supported formats.
"""

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from core import sample_media
from core.sample_media import upload as sample_upload
from core.uploads import UploadRejected, safe_display_filename, safe_extension, validate_upload

from .models import MediaAsset


class UploadValidationUnitTests(TestCase):
    """The validator itself, away from HTTP."""

    def test_accepts_each_allowed_family(self):
        for name in ("photo.png", "photo.jpg", "photo.gif", "clip.mp4", "clip.webm",
                     "voice.mp3", "invoice.pdf", "sheet.xlsx", "notes.txt", "list.csv"):
            with self.subTest(name=name):
                _ext, content_type, kind, stored, display = validate_upload(sample_upload(name))
                self.assertTrue(content_type)
                self.assertIn(kind, ("IMAGE", "VIDEO", "DOCUMENT"))
                self.assertEqual(display, name)
                self.assertTrue(stored.endswith(name[name.rindex("."):]))

    def test_rejects_html(self):
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("evil.html", b"<script>alert(1)</script>", content_type="text/html"))

    def test_rejects_svg(self):
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile(
                "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                content_type="image/svg+xml",
            ))

    def test_rejects_scripts_and_executables(self):
        for name, body in (
            ("run.js", b"alert(1)"),
            ("shell.sh", b"#!/bin/sh\nrm -rf /"),
            ("app.exe", b"MZ\x90\x00"),
            ("code.php", b"<?php system($_GET['c']); ?>"),
            ("page.htm", b"<html></html>"),
            ("style.css", b"body{}"),
            ("doc.xml", b"<?xml version='1.0'?><a/>"),
        ):
            with self.subTest(name=name), self.assertRaises(UploadRejected):
                validate_upload(SimpleUploadedFile(name, body))

    def test_rejects_html_wearing_an_image_extension(self):
        """The renamed-payload case: the extension is fine, the bytes are not."""
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("photo.png", b"<script>alert(1)</script>", content_type="image/png"))

    def test_rejects_a_spoofed_content_type(self):
        """A browser-declared type decides nothing -- in either direction."""
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("payload.html", sample_media.PNG, content_type="image/png"))

    def test_rejects_markup_dressed_as_plain_text(self):
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("notes.txt", b"  <html><body>hi</body></html>"))

    def test_rejects_an_empty_file(self):
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("photo.png", b""))

    def test_rejects_a_file_with_no_extension(self):
        with self.assertRaises(UploadRejected):
            validate_upload(SimpleUploadedFile("photo", sample_media.PNG))

    def test_allowed_narrowing_is_honoured(self):
        with self.assertRaises(UploadRejected):
            validate_upload(sample_upload("photo.png"), allowed={".csv"})

    def test_safe_extension_refuses_anything_that_could_be_a_path(self):
        for name in ("a.pn/g", "a.p\\ng", "a.\x00png", "a..", "a.averylongextension", "a.png ", "..%2fetc%2fpasswd"):
            with self.subTest(name=name):
                self.assertNotIn("/", safe_extension(name))
                self.assertNotIn("\\", safe_extension(name))

    def test_display_filename_strips_traversal_and_control_characters(self):
        self.assertEqual(safe_display_filename("../../../etc/passwd"), "passwd")
        self.assertEqual(safe_display_filename("..\\..\\windows\\system32\\cmd.exe"), "cmd.exe")
        self.assertEqual(safe_display_filename("na\x00me\n.png"), "name.png")
        self.assertEqual(safe_display_filename(""), "attachment")
        self.assertEqual(safe_display_filename("..."), "attachment")


class UploadApiSecurityTests(TestCase):
    """The same rules, through the endpoint the composer actually calls."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-upload-sec", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-upload-sec-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _post(self, *files):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.post("/api/v1/media/assets/", {"files": list(files)}, format="multipart")

    def test_allowed_image_is_accepted(self):
        response = self._post(sample_upload("photo.png", content_type="image/png"))

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["kind"], "IMAGE")

    def test_allowed_document_is_accepted(self):
        response = self._post(sample_upload("invoice.pdf", content_type="application/pdf"))

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()[0]["kind"], "DOCUMENT")

    def test_allowed_audio_is_accepted(self):
        response = self._post(sample_upload("voice.mp3", content_type="audio/mpeg"))

        self.assertEqual(response.status_code, 201, response.content)

    def test_html_is_rejected(self):
        response = self._post(SimpleUploadedFile("evil.html", b"<script>alert(1)</script>", content_type="text/html"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_svg_is_rejected(self):
        response = self._post(SimpleUploadedFile(
            "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            content_type="image/svg+xml",
        ))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_script_content_is_rejected(self):
        response = self._post(SimpleUploadedFile("payload.js", b"alert(document.cookie)", content_type="text/javascript"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_spoofed_mime_type_is_rejected(self):
        response = self._post(SimpleUploadedFile("evil.html", sample_media.PNG, content_type="image/png"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_renamed_html_is_rejected(self):
        response = self._post(SimpleUploadedFile("photo.png", b"<html><script>x</script></html>", content_type="image/png"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_one_bad_file_stores_none_of_them(self):
        response = self._post(
            sample_upload("good.png", content_type="image/png"),
            SimpleUploadedFile("bad.html", b"<html></html>", content_type="text/html"),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_traversal_filename_cannot_escape_the_tenant_directory(self):
        response = self._post(SimpleUploadedFile("../../../../etc/cron.d/pwn.png", sample_media.PNG))

        self.assertEqual(response.status_code, 201, response.content)
        asset = MediaAsset.objects.get()
        self.assertTrue(asset.file.name.startswith(f"message_media/tenant_{self.tenant.pk}/"))
        self.assertNotIn("..", asset.file.name)
        self.assertEqual(asset.original_filename, "pwn.png")

    def test_stored_name_is_generated_not_supplied(self):
        response = self._post(SimpleUploadedFile("holiday-photo.png", sample_media.PNG))

        self.assertEqual(response.status_code, 201, response.content)
        asset = MediaAsset.objects.get()
        self.assertNotIn("holiday-photo", asset.file.name)
        self.assertTrue(asset.file.name.endswith(".png"))
        # The name a person recognises survives, for the WhatsApp download label.
        self.assertEqual(asset.original_filename, "holiday-photo.png")

    def test_stored_content_type_comes_from_the_bytes_not_the_browser(self):
        response = self._post(SimpleUploadedFile("photo.png", sample_media.PNG, content_type="application/x-nonsense"))

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(MediaAsset.objects.get().content_type, "image/png")


class ContactImportUploadSecurityTests(TestCase):
    """The CSV importer writes under MEDIA_ROOT too, so it gets the same rules."""

    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-import-sec", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-import-sec-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _post(self, upload):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.post("/api/v1/contacts/imports/", {"file": upload}, format="multipart")

    def test_accepts_a_real_csv(self):
        self.assertEqual(self._post(sample_upload("contacts.csv")).status_code, 201)

    def test_rejects_html_renamed_to_csv(self):
        response = self._post(SimpleUploadedFile("contacts.csv", b"<html><script>x</script></html>"))

        self.assertEqual(response.status_code, 400)

    def test_rejects_a_non_csv_extension(self):
        response = self._post(SimpleUploadedFile("contacts.html", b"<html></html>", content_type="text/html"))

        self.assertEqual(response.status_code, 400)

    def test_stored_path_is_generated_not_supplied(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            response = self.client.post(
                "/api/v1/contacts/imports/",
                {"file": SimpleUploadedFile("../../evil.csv", sample_media.CSV)},
                format="multipart",
            )

        self.assertEqual(response.status_code, 201, response.content)
        stored = response.json()["file"]
        self.assertNotIn("..", stored)
        self.assertNotIn("evil", stored)
