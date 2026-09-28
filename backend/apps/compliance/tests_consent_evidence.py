"""
Consent evidence, and the precedence rule that must never be worked around.

The rule, stated once and enforced in one place
(compliance.services.may_receive_marketing):

    explicit opt-out  >  marketing consent  >  campaign eligibility

Every test here is a route by which somebody could previously have got a
message to a person who asked not to receive one, or a route by which a
platform could invent consent on a customer's behalf.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.compliance.models import CompliancePolicy, ConsentEvent
from apps.compliance.services import may_receive_marketing, opt_out, set_marketing_consent
from apps.contacts.models import Contact
from apps.tenants.models import Tenant


class ConsentTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-consent", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-consent", email="ada@consent.example", password="correct-horse-battery-staple",
            tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def policy(self, **fields):
        policy, _ = CompliancePolicy.objects.get_or_create(tenant=self.tenant)
        for name, value in fields.items():
            setattr(policy, name, value)
        policy.save()
        return policy


class DefaultsTests(ConsentTestCase):
    def test_a_new_contact_has_no_recorded_marketing_consent(self):
        """
        The single most important default in this file. A phone number in a
        spreadsheet is not evidence that its owner agreed to anything, and a
        platform that defaults this to GRANTED is inventing consent for its
        customers.
        """
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)
        self.assertEqual(self.contact.consent_source, "")
        self.assertIsNone(self.contact.consent_at)

    def test_a_new_contact_is_subscribed(self):
        """Somebody who has never said stop has not said stop."""
        self.assertTrue(self.contact.subscribed)


class PrecedenceTests(ConsentTestCase):
    def test_an_opted_out_contact_is_refused_even_with_consent_on_file(self):
        """The precedence rule, in one assertion."""
        set_marketing_consent(
            self.contact, True, source=Contact.ConsentSource.WEBSITE, reference="signup-form",
        )
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")

        permitted, reason = may_receive_marketing(self.contact)
        self.assertFalse(permitted)
        self.assertIn("unsubscribed", reason.lower())

    def test_recording_consent_never_clears_an_opt_out(self):
        """
        The route the audit was most worried about: a customer uploads a list
        that "has consent", and somebody who replied STOP last week starts
        receiving messages again.
        """
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        self.contact.refresh_from_db()

        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.CSV, reference="list-2026")

        self.contact.refresh_from_db()
        self.assertFalse(self.contact.subscribed)
        self.assertIsNotNone(self.contact.unsubscribed_at)
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.GRANTED)
        # Consent is on file, and it still does not get them messaged.
        self.assertFalse(may_receive_marketing(self.contact)[0])

    def test_without_the_policy_an_unknown_consent_is_allowed(self):
        """
        The default is permissive, deliberately and documented: every contact
        that already exists is UNKNOWN, so requiring consent by default would
        stop every existing customer's campaigns on upgrade.
        """
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)
        self.assertTrue(may_receive_marketing(self.contact)[0])

    def test_with_the_policy_an_unknown_consent_is_refused(self):
        self.policy(require_marketing_consent=True)
        permitted, reason = may_receive_marketing(self.contact)
        self.assertFalse(permitted)
        self.assertIn("consent", reason.lower())

    def test_with_the_policy_a_granted_consent_is_allowed(self):
        self.policy(require_marketing_consent=True)
        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.FORM)
        self.contact.refresh_from_db()
        self.assertTrue(may_receive_marketing(self.contact)[0])

    def test_with_the_policy_a_denied_consent_is_refused(self):
        self.policy(require_marketing_consent=True)
        set_marketing_consent(self.contact, False, source=Contact.ConsentSource.FORM)
        self.contact.refresh_from_db()
        self.assertFalse(may_receive_marketing(self.contact)[0])


class EvidenceTests(ConsentTestCase):
    def test_recording_consent_stores_its_source_and_time(self):
        set_marketing_consent(
            self.contact, True, source=Contact.ConsentSource.WEBSITE, reference="newsletter-2026",
        )
        self.contact.refresh_from_db()

        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.GRANTED)
        self.assertEqual(self.contact.consent_source, Contact.ConsentSource.WEBSITE)
        self.assertEqual(self.contact.consent_reference, "newsletter-2026")
        self.assertIsNotNone(self.contact.consent_at)

    def test_every_consent_decision_appends_a_row(self):
        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.WEBSITE)
        set_marketing_consent(self.contact, False, source=Contact.ConsentSource.MANUAL)

        events = list(ConsentEvent.objects.filter(contact=self.contact).order_by("created_at"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event, ConsentEvent.Event.OPT_IN)
        self.assertEqual(events[1].event, ConsentEvent.Event.OPT_OUT)

    def test_the_record_survives_the_contact_being_deleted(self):
        """
        The regression this closes. `ConsentEvent.contact` was CASCADE, so
        deleting a contact silently destroyed the record that they had ever
        opted out -- the one fact that most needs to outlive them.

        Re-import the same number a month later and nothing said they had asked
        to be left alone; and if they complained about messages already sent,
        the evidence that the opt-out was honoured was gone too.
        """
        phone = self.contact.phone
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")

        self.contact.delete()

        surviving = ConsentEvent.objects.filter(tenant=self.tenant, event=ConsentEvent.Event.OPT_OUT)
        self.assertTrue(surviving.exists(), "Deleting the contact destroyed the opt-out record.")

        row = surviving.first()
        self.assertIsNone(row.contact_id)
        # And it still names somebody: a consent row that identifies nobody is
        # not evidence of anything.
        self.assertEqual(row.phone, phone)

    def test_the_number_is_recorded_on_every_consent_event(self):
        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.WEBSITE)
        opt_out(self.contact, source=ConsentEvent.Source.AGENT, detail="asked")

        for row in ConsentEvent.objects.filter(tenant=self.tenant):
            self.assertEqual(row.phone, self.contact.phone)

    def test_history_is_never_rewritten(self):
        """An opt-out that a later opt-in erases is not an audit trail."""
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        first = ConsentEvent.objects.filter(contact=self.contact).first()

        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.WEBSITE)

        first.refresh_from_db()
        self.assertEqual(first.event, ConsentEvent.Event.OPT_OUT)
        self.assertEqual(ConsentEvent.objects.filter(contact=self.contact).count(), 2)


class ApiTests(ConsentTestCase):
    def url(self, suffix=""):
        return f"/api/v1/contacts/contacts/{self.contact.pk}/{suffix}"

    def test_patch_cannot_resubscribe_an_opted_out_contact(self):
        """
        The hole this closes. `subscribed` was a writable serializer field, so
        `PATCH {"subscribed": true}` re-enabled somebody who had replied STOP
        with no consent record, no cleared timestamp and nothing in the log.
        A CRM sync doing a read-modify-write of the whole object would do it by
        accident, because a GET hands back `subscribed` and a PUT sends it
        straight back.
        """
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")

        response = self.client.patch(self.url(), {"subscribed": True}, format="json")
        self.assertEqual(response.status_code, 200)  # the rest of the PATCH is fine

        self.contact.refresh_from_db()
        self.assertFalse(self.contact.subscribed, "PATCH re-subscribed an opted-out contact.")

    def test_patch_cannot_forge_consent_fields(self):
        response = self.client.patch(
            self.url(),
            {
                "marketing_consent": Contact.MarketingConsent.GRANTED,
                "consent_source": Contact.ConsentSource.WEBSITE,
                "consent_reference": "invented",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)
        self.assertEqual(self.contact.consent_source, "")
        # And no evidence row was manufactured.
        self.assertEqual(ConsentEvent.objects.filter(contact=self.contact).count(), 0)

    def test_the_set_consent_endpoint_records_consent(self):
        response = self.client.post(
            self.url("set-consent/"),
            {"granted": True, "source": "WEBSITE", "reference": "signup"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.GRANTED)
        self.assertEqual(self.contact.consent_source, Contact.ConsentSource.WEBSITE)
        self.assertTrue(ConsentEvent.objects.filter(contact=self.contact).exists())

    def test_set_consent_requires_a_recognised_source(self):
        response = self.client.post(
            self.url("set-consent/"), {"granted": True, "source": "i-just-know"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)

    def test_set_consent_warns_when_the_contact_has_opted_out(self):
        """The correct outcome is surprising, so it is said rather than implied."""
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        response = self.client.post(
            self.url("set-consent/"), {"granted": True, "source": "WEBSITE"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("warning", response.json())

    def test_bulk_consent_records_one_event_per_contact(self):
        second = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Carol", phone="+919876543211",
        )
        response = self.client.post(
            "/api/v1/contacts/contacts/bulk/",
            {
                "ids": [self.contact.pk, second.pk],
                "action": "grant_consent",
                "source": "CUSTOMER_PROVIDED",
                "reference": "onboarding-list",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        for contact in (self.contact, second):
            contact.refresh_from_db()
            self.assertEqual(contact.marketing_consent, Contact.MarketingConsent.GRANTED)
        self.assertEqual(
            ConsentEvent.objects.filter(contact__in=[self.contact, second]).count(), 2,
        )

    def test_the_export_carries_the_consent_columns(self):
        set_marketing_consent(
            self.contact, True, source=Contact.ConsentSource.WEBSITE, reference="signup",
        )
        response = self.client.get("/api/v1/contacts/contacts/export/")
        self.assertEqual(response.status_code, 200)

        body = b"".join(response.streaming_content).decode()
        self.assertIn("marketing_consent", body)
        self.assertIn("consent_source", body)
        self.assertIn("GRANTED", body)
        self.assertIn("signup", body)


class ImportConsentTests(ConsentTestCase):
    def test_an_import_without_a_declared_source_grants_nothing(self):
        """
        Importing a file is not a statement about consent, and the importer
        must not turn it into one. This is the "do not silently mark all
        imported contacts as opted-in" requirement.
        """
        from apps.contacts.models import ContactImport

        contact_import = ContactImport.objects.create(
            tenant=self.tenant, created_by=self.user, file="contact_imports/x.csv",
        )
        self.assertEqual(contact_import.consent_source, "")

        # And a contact created by such an import stays UNKNOWN.
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.marketing_consent, Contact.MarketingConsent.UNKNOWN)

    def test_a_declared_source_must_be_one_of_the_recorded_ones(self):
        from apps.contacts.serializers import ContactImportSerializer

        serializer = ContactImportSerializer()
        with self.assertRaises(Exception):
            serializer.validate_consent_source("they-told-me-verbally")

        self.assertEqual(serializer.validate_consent_source(""), "")
        self.assertEqual(serializer.validate_consent_source("csv"), "CSV")
