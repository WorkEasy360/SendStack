"""
Legal documents, versioning, and the acceptance record.

The question the record has to answer, and the reason a boolean will not do:

    Which version of the Terms did this user accept, and when?
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.legal.documents_registry import ACCEPTANCE_REQUIRED, DOCUMENTS, render, summary
from apps.legal.models import LegalAcceptance
from apps.legal.services import outstanding_for, record_acceptance
from apps.tenants.models import Tenant


class LegalTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-legal", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-legal", email="ada@legal.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.client = APIClient()


class PublicAccessTests(LegalTestCase):
    def test_the_documents_are_readable_without_signing_in(self):
        """
        Somebody deciding whether to sign up has to be able to read the terms
        first, and a privacy policy behind a login is not a privacy policy.
        """
        for slug in DOCUMENTS:
            with self.subTest(slug=slug):
                response = APIClient().get(f"/api/v1/legal/documents/{slug}/")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["body"].strip())

    def test_the_document_list_is_public(self):
        response = APIClient().get("/api/v1/legal/documents/")
        self.assertEqual(response.status_code, 200)
        slugs = {row["slug"] for row in response.json()["results"]}
        self.assertEqual(slugs, set(DOCUMENTS))

    def test_an_unknown_document_is_a_404(self):
        self.assertEqual(APIClient().get("/api/v1/legal/documents/eula/").status_code, 404)

    def test_each_document_reports_its_version(self):
        for row in summary():
            self.assertTrue(row["version"])


class DraftHonestyTests(LegalTestCase):
    def test_unconfigured_documents_are_marked_as_drafts(self):
        """
        A Terms of Service with a blank where the company name should be reads
        as finished when it is not. The banner is not dismissible and the flag
        is not something the frontend decides.
        """
        with override_settings(
            LEGAL_ENTITY_NAME="", LEGAL_ENTITY_ADDRESS="",
            LEGAL_CONTACT_EMAIL="", LEGAL_GOVERNING_LAW="",
        ):
            self.assertTrue(render("terms")["is_draft"])

    def test_placeholders_render_visibly_rather_than_as_blanks(self):
        with override_settings(LEGAL_ENTITY_NAME=""):
            body = render("terms")["body"]
        self.assertIn("[TO BE COMPLETED]", body)
        # And never as an empty gap where a party's name should be.
        self.assertNotIn("**Provider:** \n", body)

    def test_configured_details_are_substituted(self):
        with override_settings(
            LEGAL_ENTITY_NAME="Acme Messaging Ltd",
            LEGAL_ENTITY_ADDRESS="1 Example Road",
            LEGAL_CONTACT_EMAIL="legal@acme.example",
            LEGAL_GOVERNING_LAW="England and Wales",
        ):
            body = render("terms")["body"]
        self.assertIn("Acme Messaging Ltd", body)
        self.assertIn("legal@acme.example", body)
        self.assertNotIn("{{", body)

    def test_the_dpa_is_not_offered_for_acceptance(self):
        """
        It is a contract between two named entities that this deployment cannot
        yet complete. Asking somebody to click "I accept" on a document full of
        blanks would manufacture evidence of an agreement that does not exist.
        """
        self.assertNotIn("dpa", ACCEPTANCE_REQUIRED)
        self.assertFalse(render("dpa")["acceptance_required"])


class AcceptanceTests(LegalTestCase):
    def test_a_new_user_has_everything_outstanding(self):
        pending = {row["slug"] for row in outstanding_for(self.user)}
        self.assertEqual(pending, set(ACCEPTANCE_REQUIRED))

    def test_accepting_records_the_version_and_the_moment(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/v1/legal/accept/", {"documents": ["terms"]}, format="json")
        self.assertEqual(response.status_code, 201)

        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertEqual(row.version, str(DOCUMENTS["terms"][1] and render("terms")["version"]))
        self.assertIsNotNone(row.accepted_at)
        self.assertEqual(row.tenant_id, self.tenant.pk)

    def test_the_version_comes_from_the_server_not_the_request(self):
        """
        A caller that could name the version it was accepting could record
        acceptance of wording it was never shown.
        """
        self.client.force_authenticate(self.user)
        self.client.post(
            "/api/v1/legal/accept/", {"documents": ["terms"], "version": "99.0"}, format="json",
        )
        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertNotEqual(row.version, "99.0")
        self.assertEqual(row.version, render("terms")["version"])

    def test_accepting_twice_does_not_inflate_the_trail(self):
        self.client.force_authenticate(self.user)
        for _ in range(3):
            self.client.post("/api/v1/legal/accept/", {"documents": ["terms"]}, format="json")
        self.assertEqual(LegalAcceptance.objects.filter(user=self.user, document="terms").count(), 1)

    def test_a_new_version_makes_the_old_acceptance_stale(self):
        """
        The reason a boolean is not enough. When the Terms move to 1.1, a
        `terms_accepted = True` silently starts claiming the user accepted 1.1
        -- a record of something that never happened.
        """
        record_acceptance(self.user, "terms")
        self.assertNotIn("terms", {row["slug"] for row in outstanding_for(self.user)})

        with override_settings(TERMS_VERSION="1.1"):
            pending = {row["slug"] for row in outstanding_for(self.user)}
            self.assertIn("terms", pending)

    def test_the_old_acceptance_survives_the_new_one(self):
        """History is appended to, never overwritten."""
        record_acceptance(self.user, "terms")
        with override_settings(TERMS_VERSION="1.1"):
            record_acceptance(self.user, "terms")

        versions = set(
            LegalAcceptance.objects.filter(user=self.user, document="terms").values_list("version", flat=True)
        )
        self.assertEqual(versions, {"1.0", "1.1"})

    def test_which_version_and_when_is_answerable(self):
        """The question this table exists for."""
        record_acceptance(self.user, "terms")
        row = LegalAcceptance.objects.filter(user=self.user, document="terms").first()
        self.assertEqual(row.version, "1.0")
        self.assertIsNotNone(row.accepted_at)

    def test_accepting_requires_authentication(self):
        response = APIClient().post("/api/v1/legal/accept/", {"documents": ["terms"]}, format="json")
        self.assertIn(response.status_code, (401, 403))

    def test_an_unknown_document_cannot_be_accepted(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/v1/legal/accept/", {"documents": ["eula"]}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LegalAcceptance.objects.filter(user=self.user).exists())

    def test_the_pending_endpoint_reports_what_is_left(self):
        self.client.force_authenticate(self.user)
        record_acceptance(self.user, "terms")

        response = self.client.get("/api/v1/legal/pending/")
        self.assertEqual(response.status_code, 200)
        pending = {row["slug"] for row in response.json()["pending"]}
        self.assertEqual(pending, set(ACCEPTANCE_REQUIRED) - {"terms"})

    def test_the_evidence_fields_are_stored_but_not_echoed(self):
        """
        The address and browser string are kept as corroboration, and there is
        no reason for any client to read them back.
        """
        self.client.force_authenticate(self.user)
        response = self.client.post(
            "/api/v1/legal/accept/", {"documents": ["terms"]}, format="json",
            HTTP_USER_AGENT="Mozilla/5.0 (test)",
        )
        row = LegalAcceptance.objects.get(user=self.user, document="terms")
        self.assertEqual(row.user_agent, "Mozilla/5.0 (test)")

        body = response.json()
        serialised = body["accepted"][0]
        self.assertNotIn("ip_address", serialised)
        self.assertNotIn("user_agent", serialised)


class IsolationTests(LegalTestCase):
    def test_one_users_acceptance_is_not_anothers(self):
        other_tenant = Tenant.objects.create(
            name="Other", slug="other-legal", kind=Tenant.Kind.CUSTOMER,
        )
        other = get_user_model().objects.create_user(
            username="bob-legal", email="bob@legal.example",
            password="correct-horse-battery-staple", tenant=other_tenant,
        )
        record_acceptance(self.user, "terms")

        self.assertIn("terms", {row["slug"] for row in outstanding_for(other)})

    def test_the_pending_endpoint_only_reports_the_caller(self):
        other_tenant = Tenant.objects.create(
            name="Other2", slug="other2-legal", kind=Tenant.Kind.CUSTOMER,
        )
        other = get_user_model().objects.create_user(
            username="carol-legal", email="carol@legal.example",
            password="correct-horse-battery-staple", tenant=other_tenant,
        )
        record_acceptance(other, "terms")

        self.client.force_authenticate(self.user)
        accepted = self.client.get("/api/v1/legal/pending/").json()["accepted"]
        self.assertEqual(accepted, [])
