"""
Tenant data export: it must carry everything the customer owns, and no secrets.

Two failure modes, both serious and both tested here:

* **Leaking sideways** -- one customer's export containing another's data.
* **Leaking downwards** -- an export file, which is emailed around and sits in
  a Downloads folder, containing credentials.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.tenants.export import build_export
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

EXPORT_URL = "/api/v1/tenants/export/"


class ExportTestCase(TestCase):
    password = "correct-horse-battery-staple"

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-export", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-export", email="ada@export.example", password=self.password, tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )

        self.other_tenant = Tenant.objects.create(
            name="Rival", slug="rival-export", kind=Tenant.Kind.CUSTOMER,
        )
        self.other_user = get_user_model().objects.create_user(
            username="eve-export", email="eve@export.example", password=self.password,
            tenant=self.other_tenant,
        )
        self.other_contact = Contact.objects.create(
            tenant=self.other_tenant, created_by=self.other_user,
            name="Rival Customer", phone="+919999999999",
        )

        self.client = APIClient()

    def export(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)


class ContentTests(ExportTestCase):
    def test_the_export_contains_the_customers_own_contacts(self):
        payload = self.export()
        phones = {row["phone"] for row in payload["contacts"]}
        self.assertIn("+919876543210", phones)

    def test_the_export_covers_the_collections_the_brief_names(self):
        payload = self.export()
        for collection in (
            "contacts", "campaigns", "messages", "templates", "deals", "tasks",
            "notes", "media", "consent_events", "settings", "whatsapp_accounts",
        ):
            self.assertIn(collection, payload)

    def test_the_manifest_counts_what_was_written(self):
        payload = self.export()
        self.assertEqual(payload["manifest"]["counts"]["contacts"], len(payload["contacts"]))

    def test_the_manifest_says_what_is_excluded(self):
        """Said in the file itself, because the file is what somebody reads later."""
        excluded = " ".join(self.export()["manifest"]["excluded"]).lower()
        self.assertIn("password", excluded)
        self.assertIn("token", excluded)

    def test_the_consent_trail_travels_with_the_contacts(self):
        from apps.compliance.models import ConsentEvent
        from apps.compliance.services import opt_out

        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        payload = self.export()
        self.assertTrue(payload["consent_events"])
        self.assertEqual(payload["consent_events"][0]["event"], "OPT_OUT")

    def test_a_file_name_is_offered_for_download(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("acme-export", response["Content-Disposition"])


class IsolationTests(ExportTestCase):
    def test_the_export_never_contains_another_tenants_contacts(self):
        payload = self.export()
        phones = {row["phone"] for row in payload["contacts"]}
        self.assertNotIn("+919999999999", phones)

    def test_no_collection_leaks_another_tenants_rows(self):
        """
        Asserted against the serialised bytes rather than per collection, so a
        collection added later without a tenant filter is caught by this test
        rather than by a customer.
        """
        raw = json.dumps(self.export())
        self.assertNotIn("+919999999999", raw)
        self.assertNotIn("Rival Customer", raw)
        self.assertNotIn("Rival", raw)

    def test_the_endpoint_takes_no_tenant_id(self):
        """
        There is deliberately no `?tenant_id=`. An export is the highest-value
        single response this API produces, and an endpoint that takes an id is
        one where a missing permission check hands over a competitor's contact
        list.
        """
        self.client.force_authenticate(self.user)
        response = self.client.get(f"{EXPORT_URL}?tenant_id={self.other_tenant.pk}")
        raw = response.content.decode()
        self.assertNotIn("+919999999999", raw)

    def test_the_export_requires_authentication(self):
        self.assertIn(APIClient().get(EXPORT_URL).status_code, (401, 403))

    def test_each_tenant_gets_only_its_own(self):
        direct = build_export(self.other_tenant)
        phones = {row["phone"] for row in direct["contacts"]}
        self.assertEqual(phones, {"+919999999999"})


class SecretTests(ExportTestCase):
    def test_no_password_hash_reaches_the_export(self):
        raw = json.dumps(self.export())
        self.user.refresh_from_db()
        self.assertNotIn(self.user.password, raw)
        self.assertNotIn("argon2", raw.lower())
        self.assertNotIn("pbkdf2", raw.lower())

    def test_no_whatsapp_credential_reaches_the_export(self):
        """
        The single most important omission in the file. A WhatsApp access
        token in an export is an account takeover for whoever opens it.
        """
        account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            account_type=WhatsAppAccount.AccountType.WHATSAPP,
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        account.set_access_token("EAA-super-secret-provider-token")
        account.save()

        payload = self.export()
        # The manifest deliberately *names* what is excluded ("WhatsApp access
        # tokens and device session credentials"), so scanning it for those
        # words finds the promise rather than a breach. Everything except the
        # manifest is data, and that is what must be clean.
        data = {key: value for key, value in payload.items() if key != "manifest"}
        raw = json.dumps(data)

        self.assertNotIn("EAA-super-secret-provider-token", raw)
        for marker in ("access_token", "session_data", "credential", "api_key", "webhook_secret"):
            self.assertNotIn(marker, raw.lower())

    def test_the_account_rows_carry_state_and_not_credentials(self):
        WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line2",
            account_type=WhatsAppAccount.AccountType.WHATSAPP,
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        row = self.export()["whatsapp_accounts"][0]
        self.assertEqual(set(row), {"id", "name", "phone_number", "provider", "status", "created_at"})

    def test_no_settings_secret_reaches_the_export(self):
        from django.conf import settings

        raw = json.dumps(self.export())
        self.assertNotIn(settings.SECRET_KEY, raw)
        for marker in ("SECRET_KEY", "JWT_SECRET", "DATABASE_URL", "REDIS_URL", "ENCRYPTION_KEY"):
            self.assertNotIn(marker, raw)
