"""
The CSV import reads its file incrementally, and still parses it correctly.

The memory saving is the point, but correctness is what the tests are mostly
about: a decoder that holds a partial line has three edge cases -- a multi-byte
character split across a chunk boundary, a quoted field containing a newline,
and a final line with no terminator -- and every one of them corrupts a
customer's contact list silently rather than raising.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.contacts.models import Contact, ContactImport
from apps.contacts.tasks import _READ_CHUNK_BYTES, _decoded_lines, process_contact_import
from apps.tenants.models import Tenant


class _FakeFieldFile:
    """
    The two methods `_decoded_lines` uses, over an in-memory buffer.

    A real FieldFile needs storage, and these tests are about the decoding, not
    about Django's file backend.
    """

    def __init__(self, data: bytes, chunk_size=8):
        self.data = data
        self._chunk_size = chunk_size
        self.closed = False

    def open(self, mode="rb"):
        self.closed = False

    def chunks(self, chunk_size=None):
        size = chunk_size or self._chunk_size
        for start in range(0, len(self.data), size):
            yield self.data[start:start + size]

    def close(self):
        self.closed = True


class DecodedLinesTests(TestCase):
    def test_lines_come_back_whole_across_chunk_boundaries(self):
        text = "a,b\n1,2\n3,4\n"
        # A chunk size of 3 guarantees every line is split.
        lines = list(_decoded_lines(_FakeFieldFile(text.encode("utf-8"), chunk_size=3)))

        self.assertEqual(lines, ["a,b\n", "1,2\n", "3,4\n"])

    def test_a_multibyte_character_split_across_chunks_survives(self):
        """
        The reason for an incremental decoder rather than chunk.decode().

        A name in Devanagari or an emoji is several bytes; a chunk boundary
        landing inside one would raise UnicodeDecodeError on a naive decode --
        and "a contact list of names" is precisely where that happens.
        """
        text = "name\nप्रिया शर्मा\n😀 Smiley\n"
        raw = text.encode("utf-8")

        for chunk_size in (1, 2, 3, 5, 7):
            with self.subTest(chunk_size=chunk_size):
                lines = list(_decoded_lines(_FakeFieldFile(raw, chunk_size=chunk_size)))
                self.assertEqual("".join(lines), text)

    def test_a_final_line_without_a_newline_is_still_yielded(self):
        lines = list(_decoded_lines(_FakeFieldFile(b"a,b\n1,2", chunk_size=3)))

        self.assertEqual(lines, ["a,b\n", "1,2"])

    def test_the_excel_byte_order_mark_is_stripped(self):
        """
        Without this the first column's header is "\\ufeffphone", which the
        column detector does not recognise -- and the import fails with "no
        phone column" on a file that plainly has one.
        """
        raw = "﻿phone,name\n+15551234567,Priya\n".encode("utf-8")

        lines = list(_decoded_lines(_FakeFieldFile(raw, chunk_size=4)))

        self.assertTrue(lines[0].startswith("phone"))

    def test_crlf_files_parse(self):
        raw = b"phone,name\r\n+15551234567,Priya\r\n"

        rows = list(__import__("csv").DictReader(_decoded_lines(_FakeFieldFile(raw, chunk_size=5))))

        self.assertEqual(rows[0]["name"], "Priya")

    def test_a_quoted_field_containing_a_newline_stays_one_row(self):
        """
        csv treats a newline inside quotes as part of the value.

        Feeding it pre-split lines is only safe because the terminator is kept
        on each one; dropping it would turn one contact into two.
        """
        raw = b'phone,name\n+15551234567,"Priya\nSharma"\n'

        rows = list(__import__("csv").DictReader(_decoded_lines(_FakeFieldFile(raw, chunk_size=4))))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Priya\nSharma")

    def test_unicode_line_separators_do_not_split_a_row(self):
        """
        str.splitlines() breaks on U+2028 and friends; csv does not.

        Splitting on them would tear a contact whose name contained one into
        two rows -- a silent corruption, which is the worst kind.
        """
        raw = "phone,name\n+15551234567,Priya Sharma\n".encode("utf-8")

        rows = list(__import__("csv").DictReader(_decoded_lines(_FakeFieldFile(raw, chunk_size=6))))

        self.assertEqual(len(rows), 1)
        self.assertIn("Sharma", rows[0]["name"])

    def test_the_file_is_closed_even_if_the_consumer_stops_early(self):
        handle = _FakeFieldFile(b"a\nb\nc\n", chunk_size=2)

        generator = _decoded_lines(handle)
        next(generator)
        generator.close()

        self.assertTrue(handle.closed)

    def test_an_empty_file_yields_nothing(self):
        self.assertEqual(list(_decoded_lines(_FakeFieldFile(b""))), [])

    def test_the_chunk_size_is_bounded(self):
        """A sanity bound: the point is not to read the file in one go."""
        self.assertLessEqual(_READ_CHUNK_BYTES, 1024 * 1024)


class ImportEndToEndTests(TestCase):
    """The whole task, so the streaming change is proven not to have broken it."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Import Co", slug="import-streaming", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="import-owner", password="x", tenant=self.tenant,
        )

    def _run(self, csv_text):
        upload = SimpleUploadedFile(
            "contacts.csv", csv_text.encode("utf-8"), content_type="text/csv",
        )
        record = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user, file=upload,
        )
        process_contact_import(record.pk)
        record.refresh_from_db()
        return record

    def test_a_normal_file_imports(self):
        record = self._run(
            "phone,name,email\n"
            "+15551110001,Priya,priya@example.com\n"
            "+15551110002,Rahul,rahul@example.com\n"
        )

        self.assertEqual(record.status, ContactImport.Status.COMPLETED)
        self.assertEqual(record.created_count, 2)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 2)

    def test_a_file_larger_than_one_chunk_imports_completely(self):
        """
        The regression that matters: rows lost at a chunk boundary.

        Sized well past _READ_CHUNK_BYTES so the boundary is crossed many
        times, and every row is accounted for at the end.
        """
        rows = 5000
        body = "phone,name\n" + "".join(
            f"+1555{n:07d},Person {n}\n" for n in range(rows)
        )
        self.assertGreater(len(body.encode("utf-8")), _READ_CHUNK_BYTES)

        record = self._run(body)

        self.assertEqual(record.status, ContactImport.Status.COMPLETED)
        self.assertEqual(record.total_rows, rows)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), rows)

    def test_a_file_with_no_phone_column_fails_with_a_readable_message(self):
        record = self._run("name,email\nPriya,priya@example.com\n")

        self.assertEqual(record.status, ContactImport.Status.FAILED)

    def test_consent_is_still_not_inferred_from_the_upload(self):
        """
        Phase 3's rule, re-asserted because this change touched the importer.

        An importer that silently marks strangers as opted-in is a compliance
        incident with a progress bar -- so a file uploaded without an explicit
        consent assertion must leave every contact at UNKNOWN.
        """
        self._run("phone,name\n+15551110009,Priya\n")

        contact = Contact.objects.get(tenant=self.tenant, phone="+15551110009")
        self.assertEqual(contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)
