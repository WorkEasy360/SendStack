import io

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from .mapping import custom_fields_for, detect_columns
from .models import Contact, ContactGroup, ContactImport, ContactTag
from .segments import resolve_segment_queryset
from .tasks import process_contact_import
from .utils import normalize_phone


class NormalizePhoneTests(TestCase):
    def test_strips_formatting_and_adds_plus(self):
        self.assertEqual(normalize_phone("+1 (555) 123-4567"), "+15551234567")

    def test_rejects_too_short_numbers(self):
        with self.assertRaises(ValueError):
            normalize_phone("12345")


class ContactApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-contacts", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-contacts", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-contacts-owner", password="x", tenant=self.tenant_a)
        self.user_b = get_user_model().objects.create_user(username="globex-contacts-owner", password="x", tenant=self.tenant_b)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_tenant_isolation_on_list(self):
        Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="Acme Contact", phone="+15551110000")
        Contact.objects.create(tenant=self.tenant_b, created_by=self.user_b, name="Globex Contact", phone="+15552220000")

        response = self.client.get("/api/v1/contacts/contacts/")

        names = {c["name"] for c in response.json()["results"]}
        self.assertEqual(names, {"Acme Contact"})

    def test_unsubscribe_and_resubscribe(self):
        contact = Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="Priya", phone="+15551230000")

        unsub = self.client.post(f"/api/v1/contacts/contacts/{contact.pk}/unsubscribe/")
        contact.refresh_from_db()
        self.assertFalse(contact.subscribed)
        self.assertIsNotNone(contact.unsubscribed_at)
        self.assertEqual(unsub.status_code, 200)

        resub = self.client.post(f"/api/v1/contacts/contacts/{contact.pk}/resubscribe/")
        contact.refresh_from_db()
        self.assertTrue(contact.subscribed)
        self.assertIsNone(contact.unsubscribed_at)
        self.assertEqual(resub.status_code, 200)

    def test_bulk_add_tag(self):
        tag = ContactTag.objects.create(tenant=self.tenant_a, name="VIP")
        c1 = Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="A", phone="+15551230001")
        c2 = Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="B", phone="+15551230002")

        response = self.client.post(
            "/api/v1/contacts/contacts/bulk/",
            {"contact_ids": [c1.pk, c2.pk], "action": "add_tag", "tag_id": tag.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["affected"], 2)
        self.assertTrue(c1.tags.filter(pk=tag.pk).exists())
        self.assertTrue(c2.tags.filter(pk=tag.pk).exists())

    def test_bulk_action_cannot_touch_other_tenants_contacts(self):
        foreign_contact = Contact.objects.create(tenant=self.tenant_b, created_by=self.user_b, name="Foreign", phone="+15559990000")

        response = self.client.post(
            "/api/v1/contacts/contacts/bulk/",
            {"contact_ids": [foreign_contact.pk], "action": "unsubscribe"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["affected"], 0)
        foreign_contact.refresh_from_db()
        self.assertTrue(foreign_contact.subscribed)

    def test_delete_removes_contact(self):
        contact = Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="Priya", phone="+15551230004")

        response = self.client.delete(f"/api/v1/contacts/contacts/{contact.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Contact.objects.filter(pk=contact.pk).exists())

    def test_cannot_delete_another_tenants_contact(self):
        foreign_contact = Contact.objects.create(tenant=self.tenant_b, created_by=self.user_b, name="Foreign", phone="+15559990001")

        response = self.client.delete(f"/api/v1/contacts/contacts/{foreign_contact.pk}/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Contact.objects.filter(pk=foreign_contact.pk).exists())

    def test_export_returns_csv(self):
        Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="Priya", phone="+15551230003")

        response = self.client.get("/api/v1/contacts/contacts/export/")

        content = b"".join(response.streaming_content).decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Priya", content)
        self.assertIn("+15551230003", content)


class ContactImportTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-import", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-import-owner", password="x", tenant=self.tenant)

    def test_import_creates_updates_and_flags_invalid_rows(self):
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="", phone="+15550000001")

        csv_content = (
            "name,phone,email,City\n"
            "New Contact,+15550000002,new@example.test,Mumbai\n"
            "Existing Contact,+15550000001,existing@example.test,Delhi\n"
            "Bad Row,not-a-phone,bad@example.test,Pune\n"
        )
        contact_import = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user,
            file=SimpleUploadedFile("contacts.csv", csv_content.encode(), content_type="text/csv"),
        )

        process_contact_import(contact_import.pk)

        contact_import.refresh_from_db()
        self.assertEqual(contact_import.status, ContactImport.Status.COMPLETED)
        self.assertEqual(contact_import.created_count, 1)
        self.assertEqual(contact_import.updated_count, 1)
        self.assertEqual(contact_import.error_count, 1)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 2)

        updated = Contact.objects.get(tenant=self.tenant, phone="+15550000001")
        self.assertEqual(updated.name, "Existing Contact")
        self.assertEqual(updated.custom_fields.get("City"), "Delhi")

    def test_import_assigns_requested_groups_and_tags(self):
        group = ContactGroup.objects.create(tenant=self.tenant, name="Leads")
        tag = ContactTag.objects.create(tenant=self.tenant, name="Imported")
        csv_content = "name,phone\nSomeone,+15550000009\n"
        contact_import = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user, group_ids=[group.pk], tag_ids=[tag.pk],
            file=SimpleUploadedFile("contacts.csv", csv_content.encode(), content_type="text/csv"),
        )

        process_contact_import(contact_import.pk)

        contact = Contact.objects.get(tenant=self.tenant, phone="+15550000009")
        self.assertTrue(contact.groups.filter(pk=group.pk).exists())
        self.assertTrue(contact.tags.filter(pk=tag.pk).exists())


class ContactSegmentTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-segment", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-segment-owner", password="x", tenant=self.tenant)

    def test_resolves_and_combines_filters(self):
        vip = ContactTag.objects.create(tenant=self.tenant, name="VIP")
        match = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="A", phone="+15551110001", country="IN", subscribed=True)
        match.tags.add(vip)
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="B", phone="+15551110002", country="US", subscribed=True)
        unsubscribed = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="C", phone="+15551110003", country="IN", subscribed=False)
        unsubscribed.tags.add(vip)

        queryset = resolve_segment_queryset(
            self.tenant,
            [
                {"field": "country", "operator": "eq", "value": "IN"},
                {"field": "tags", "operator": "eq", "value": vip.pk},
                {"field": "subscribed", "operator": "eq", "value": True},
            ],
        )

        self.assertEqual(list(queryset), [match])


class ContactBulkActionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-contact-bulk", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-contact-bulk-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.contacts = [
            Contact.objects.create(tenant=self.tenant, created_by=self.user, name=f"C{n}", phone=f"+1555200000{n}")
            for n in range(3)
        ]

    def _bulk(self, payload):
        return self.client.post("/api/v1/contacts/contacts/bulk/", payload, format="json")

    def test_accepts_the_shared_ids_key(self):
        response = self._bulk({"action": "delete", "ids": [self.contacts[0].pk, self.contacts[1].pk]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual(Contact.objects.count(), 1)

    def test_still_accepts_the_original_contact_ids_key(self):
        response = self._bulk({"action": "unsubscribe", "contact_ids": [c.pk for c in self.contacts]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["affected"], 3)
        self.assertEqual(Contact.objects.filter(subscribed=True).count(), 0)

    def test_an_empty_selection_is_refused(self):
        response = self._bulk({"action": "delete", "ids": []})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Contact.objects.count(), 3)

    def test_ids_outside_this_tenant_are_counted_as_failures(self):
        other = Tenant.objects.create(name="Globex", slug="globex-contact-bulk", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="globex-contact-bulk-owner", password="x", tenant=other
        )
        theirs = Contact.objects.create(tenant=other, created_by=other_user, name="Theirs", phone="+15559998888")

        response = self._bulk({"action": "delete", "ids": [self.contacts[0].pk, theirs.pk]})

        body = response.json()
        self.assertEqual((body["succeeded"], body["failed"]), (1, 1))
        self.assertTrue(Contact.objects.filter(pk=theirs.pk).exists())

    def test_export_can_be_narrowed_to_the_selected_rows(self):
        selected = [self.contacts[0], self.contacts[2]]
        ids = ",".join(str(contact.pk) for contact in selected)

        response = self.client.get(f"/api/v1/contacts/contacts/export/?ids={ids}")

        rows = b"".join(response.streaming_content).decode()
        self.assertIn(self.contacts[0].phone, rows)
        self.assertIn(self.contacts[2].phone, rows)
        self.assertNotIn(self.contacts[1].phone, rows)

    def test_export_without_ids_still_covers_everything(self):
        response = self.client.get("/api/v1/contacts/contacts/export/")

        rows = b"".join(response.streaming_content).decode()
        for contact in self.contacts:
            self.assertIn(contact.phone, rows)


class ColumnDetectionTests(TestCase):
    """
    Nobody exports a file with our header names. See mapping.py -- guessing
    these is what removes the column-mapping screen entirely.
    """

    def test_it_recognises_the_obvious_names(self):
        mapping = detect_columns(["Name", "Phone", "Email", "Country"])
        self.assertEqual(mapping["name"], "Name")
        self.assertEqual(mapping["phone"], "Phone")
        self.assertEqual(mapping["email"], "Email")
        self.assertEqual(mapping["country"], "Country")

    def test_it_recognises_what_people_actually_export(self):
        mapping = detect_columns(["Customer Name", "Mobile No.", "E-mail ID", "Firm"])
        self.assertEqual(mapping["name"], "Customer Name")
        self.assertEqual(mapping["phone"], "Mobile No.")
        self.assertEqual(mapping["email"], "E-mail ID")
        self.assertEqual(mapping["company"], "Firm")

    def test_it_matches_a_header_that_merely_contains_the_word(self):
        mapping = detect_columns(["Primary Mobile Number", "Full Name"])
        self.assertEqual(mapping["phone"], "Primary Mobile Number")
        self.assertEqual(mapping["name"], "Full Name")

    def test_one_header_is_never_used_for_two_fields(self):
        mapping = detect_columns(["Contact Number"])
        self.assertEqual(len(set(mapping.values())), len(mapping.values()))

    def test_unrecognised_headers_are_simply_absent(self):
        mapping = detect_columns(["Order ID", "Amount"])
        self.assertEqual(mapping, {})

    def test_everything_unmapped_becomes_a_custom_field(self):
        mapping = detect_columns(["Phone", "Order ID"])
        custom = custom_fields_for({"Phone": "+15550000001", "Order ID": "A-99"}, mapping)
        self.assertEqual(custom, {"Order ID": "A-99"})


class ImportMappingTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-import-map", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-import-map-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _upload(self, content, name="contacts.csv"):
        return SimpleUploadedFile(name, content.encode(), content_type="text/csv")

    def test_an_import_with_unfamiliar_headers_still_works(self):
        csv_content = (
            "Customer Name,Mobile No.,E-mail ID,Firm,Order ID\n"
            "Ravi Sharma,+15550000021,ravi@example.test,Sharma Textiles,A-1\n"
        )
        contact_import = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user, file=self._upload(csv_content),
        )
        process_contact_import(contact_import.pk)

        contact = Contact.objects.get(tenant=self.tenant, phone="+15550000021")
        self.assertEqual(contact.name, "Ravi Sharma")
        self.assertEqual(contact.email, "ravi@example.test")
        self.assertEqual(contact.company, "Sharma Textiles")
        self.assertEqual(contact.custom_fields.get("Order ID"), "A-1")

        contact_import.refresh_from_db()
        self.assertEqual(contact_import.column_mapping["phone"], "Mobile No.")

    def test_a_file_with_no_phone_column_fails_with_a_readable_reason(self):
        contact_import = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user, file=self._upload("Name,City\nRavi,Mumbai\n"),
        )
        process_contact_import(contact_import.pk)
        contact_import.refresh_from_db()
        self.assertEqual(contact_import.status, ContactImport.Status.FAILED)
        self.assertIn("phone", contact_import.errors[0]["error"].lower())

    def test_the_preview_counts_new_existing_and_invalid_without_importing(self):
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Old", phone="+15550000031")
        csv_content = (
            "Mobile,Name\n"
            "+15550000031,Existing\n"
            "+15550000032,Fresh\n"
            "+15550000032,Duplicate in file\n"
            "nonsense,Broken\n"
        )
        response = self.client.post(
            "/api/v1/contacts/imports/preview/", {"file": self._upload(csv_content)}, format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mapping"]["phone"], "Mobile")
        self.assertEqual(response.data["valid"], 2)
        self.assertEqual(response.data["existing"], 1)
        self.assertEqual(response.data["new"], 1)
        self.assertEqual(response.data["invalid"], 1)
        self.assertEqual(response.data["duplicates_in_file"], 1)
        # Nothing was written.
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 1)

    def test_the_preview_reports_headers_it_could_not_place(self):
        response = self.client.post(
            "/api/v1/contacts/imports/preview/",
            {"file": self._upload("Phone,Order ID\n+15550000041,A-1\n")}, format="multipart",
        )
        self.assertEqual(response.data["unmapped_headers"], ["Order ID"])

    def test_the_preview_refuses_a_non_csv(self):
        response = self.client.post(
            "/api/v1/contacts/imports/preview/",
            {"file": SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")}, format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_the_preview_needs_authentication(self):
        self.assertEqual(
            APIClient().post("/api/v1/contacts/imports/preview/").status_code, 401,
        )
