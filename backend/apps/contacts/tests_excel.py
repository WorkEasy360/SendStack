"""
Importing contacts from an Excel workbook.

The workbook goes through exactly the reader the CSV goes through
(`readers.open_rows`), so the tests here are about the parts that are
different: Excel's number cells, a title row above the header row, the
formats that are refused and why, and -- because the CSV path was refactored
to share the reader -- that a CSV that is not UTF-8 still gets the same
advice it always did.
"""

import datetime as dt
import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from .models import Contact, ContactImport
from .readers import _cell_text

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PREVIEW = "/api/v1/contacts/imports/preview/"
IMPORTS = "/api/v1/contacts/imports/"


def _xlsx(rows) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class CellTextTests(TestCase):
    def test_excel_numbers_come_back_as_the_digits_somebody_typed(self):
        # Excel has no integer type: a phone typed as digits is a float.
        self.assertEqual(_cell_text(919876543210.0), "919876543210")
        self.assertEqual(_cell_text(42), "42")
        self.assertEqual(_cell_text(12.5), "12.5")
        self.assertEqual(_cell_text(None), "")
        self.assertEqual(_cell_text(True), "TRUE")
        self.assertEqual(_cell_text(dt.date(2026, 1, 2)), "2026-01-02")
        # Excel stores a date as a datetime at midnight; the day is what was typed.
        self.assertEqual(_cell_text(dt.datetime(2026, 1, 2)), "2026-01-02")
        self.assertEqual(_cell_text(dt.datetime(2026, 1, 2, 9, 30)), "2026-01-02 09:30:00")
        self.assertEqual(_cell_text("  Ravi "), "Ravi")


class ExcelImportTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-excel", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-excel-owner", password="x", tenant=self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _workbook(self, rows, name="contacts.xlsx"):
        return SimpleUploadedFile(name, _xlsx(rows), content_type=XLSX_TYPE)

    def test_the_preview_reads_a_workbook_like_a_csv(self):
        upload = self._workbook([
            ["Mobile", "Name", "Joined"],
            [15550000051, "Ravi", dt.date(2026, 1, 2)],
            [15550000052.0, "Meera", None],
            ["nonsense", "Broken", None],
        ])

        response = self.client.post(PREVIEW, {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mapping"], {"phone": "Mobile", "name": "Name"})
        self.assertEqual(response.data["unmapped_headers"], ["Joined"])
        self.assertEqual((response.data["valid"], response.data["invalid"]), (2, 1))
        self.assertEqual(response.data["sample"][0]["phone"], "+15550000051")
        self.assertFalse(Contact.objects.filter(tenant=self.tenant).exists())

    def test_importing_a_workbook_creates_contacts_and_keeps_the_extra_columns(self):
        upload = self._workbook([
            ["Phone", "Name", "Order ID", "Joined"],
            [15550000061, "Ravi", 42, dt.date(2026, 1, 2)],
        ])

        response = self.client.post(IMPORTS, {"file": upload}, format="multipart")

        self.assertEqual(response.status_code, 201, response.data)
        contact_import = ContactImport.objects.get(pk=response.data["id"])
        self.assertEqual(contact_import.status, ContactImport.Status.COMPLETED, contact_import.errors)
        self.assertEqual(contact_import.created_count, 1)
        self.assertEqual(contact_import.column_mapping["phone"], "Phone")
        contact = Contact.objects.get(tenant=self.tenant, phone="+15550000061")
        self.assertEqual(contact.name, "Ravi")
        self.assertEqual(contact.custom_fields.get("Order ID"), "42")
        self.assertEqual(contact.custom_fields.get("Joined"), "2026-01-02")

    def test_rows_above_the_header_row_are_skipped(self):
        """
        A title line is non-empty, so the header is the first row that names
        a phone column -- not merely the first row with something in it.
        """
        upload = self._workbook([
            ["Customer list export", None],
            ["Exported 2 Jan 2026", "by Ravi"],
            [],
            ["Phone", "Name"],
            [15550000071, "Asha"],
        ])
        response = self.client.post(PREVIEW, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mapping"]["phone"], "Phone")
        self.assertEqual(response.data["valid"], 1)
        self.assertEqual(response.data["rows_scanned"], 1)

    def test_a_workbook_with_no_phone_column_still_fails_with_the_readable_reason(self):
        upload = self._workbook([["Name", "City"], ["Ravi", "Mumbai"]])
        response = self.client.post(IMPORTS, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        contact_import = ContactImport.objects.get(pk=response.data["id"])
        self.assertEqual(contact_import.status, ContactImport.Status.FAILED)
        self.assertIn("phone", contact_import.errors[0]["error"].lower())

    def test_an_old_xls_workbook_is_refused_with_the_fix(self):
        legacy = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64

        preview = self.client.post(
            PREVIEW, {"file": SimpleUploadedFile("old.xls", legacy, content_type="application/vnd.ms-excel")},
            format="multipart",
        )
        self.assertEqual(preview.status_code, 400)
        self.assertIn(".xlsx", preview.data["detail"])

        imported = self.client.post(
            IMPORTS, {"file": SimpleUploadedFile("old.xls", legacy, content_type="application/vnd.ms-excel")},
            format="multipart",
        )
        self.assertEqual(imported.status_code, 400)
        self.assertFalse(ContactImport.objects.exists())

    def test_a_workbook_that_is_not_one_is_refused(self):
        fake = SimpleUploadedFile("fake.xlsx", b"PK\x03\x04 not really a workbook", content_type=XLSX_TYPE)
        response = self.client.post(PREVIEW, {"file": fake}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Excel", response.data["detail"])

    def test_a_csv_that_is_not_utf8_still_gets_the_same_advice(self):
        upload = SimpleUploadedFile(
            "contacts.csv", "phone,name\n+15550000081,Jos\xe9\n".encode("latin-1"), content_type="text/csv",
        )
        response = self.client.post(PREVIEW, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("UTF-8", response.data["detail"])

    def test_a_csv_still_imports_through_the_shared_reader(self):
        upload = SimpleUploadedFile(
            "contacts.csv", "Mobile No.,Customer Name\n+15550000091,Ravi\n".encode("utf-8"), content_type="text/csv",
        )
        response = self.client.post(IMPORTS, {"file": upload}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        contact_import = ContactImport.objects.get(pk=response.data["id"])
        self.assertEqual(contact_import.status, ContactImport.Status.COMPLETED, contact_import.errors)
        self.assertEqual(Contact.objects.get(tenant=self.tenant, phone="+15550000091").name, "Ravi")
