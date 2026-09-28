"""
Support requests: who may raise one, who may read one, and what a snapshot holds.

Two things are being defended here. The first is ordinary tenant isolation --
one workspace must not read another's requests. The second is subtler and is
the reason the context snapshot exists at all: it must be **useful to support
and useless to an attacker**, which in practice means it must never contain a
phone number, a contact's name or a message body. Several tests below plant
exactly those values and then assert they are absent from the captured
context, because that is the regression somebody will introduce later by
adding "just one more field".
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.subscriptions.models import Plan, Subscription
from apps.support import services
from apps.support.models import SupportRequest, generate_reference
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

User = get_user_model()


class SupportTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER)
        self.other = Tenant.objects.create(name="Rival", slug="rival", kind=Tenant.Kind.CUSTOMER)
        self.plan = Plan.objects.create(
            name="Growth", price_cents=99900, currency="INR", is_public=True,
            max_messages_per_month=10000, max_contacts=5000,
        )
        Subscription.objects.create(
            tenant=self.tenant, plan=self.plan, status=Subscription.Status.ACTIVE,
        )
        self.user = User.objects.create_user(
            username="acme-owner", password="pw", tenant=self.tenant, role=User.Role.USER,
        )
        self.other_user = User.objects.create_user(
            username="rival-owner", password="pw", tenant=self.other, role=User.Role.USER,
        )
        self.staff = User.objects.create_user(
            username="platform", password="pw", tenant=self.tenant, role=User.Role.SUPERADMIN,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def raise_request(self, **kwargs):
        payload = {
            "category": "TECHNICAL",
            "subject": "Campaign did not send",
            "message": "I started a campaign this morning and nothing seems to have gone out.",
        }
        payload.update(kwargs)
        return self.client.post(reverse("support-request-list"), payload, format="json")


class ReferenceTests(TestCase):
    def test_references_avoid_ambiguous_characters(self):
        """A reference read down a phone line must not come back as a different one."""
        for _ in range(200):
            reference = generate_reference()
            self.assertEqual(len(reference), 8)
            self.assertFalse(set(reference) & set("ILOU"))

    def test_references_are_not_sequential(self):
        references = {generate_reference() for _ in range(200)}
        self.assertEqual(len(references), 200)


class RaisingTests(SupportTestCase):
    def test_a_customer_can_raise_a_request_and_gets_a_reference(self):
        response = self.raise_request()
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["reference"])
        self.assertEqual(response.data["status"], "OPEN")

    def test_a_very_short_message_is_refused_with_a_useful_reason(self):
        response = self.raise_request(message="broken")
        self.assertEqual(response.status_code, 400)

    def test_the_correlation_id_is_captured(self):
        self.raise_request()
        support_request = SupportRequest.objects.get()
        self.assertTrue(support_request.correlation_id)

    def test_a_client_cannot_set_its_own_priority(self):
        """A priority a customer can set is a field where everything is urgent."""
        self.raise_request(priority="URGENT")
        self.assertEqual(SupportRequest.objects.get().priority, SupportRequest.Priority.NORMAL)

    def test_a_client_cannot_set_the_tenant_or_the_status(self):
        self.raise_request(tenant=self.other.pk, status="RESOLVED")
        support_request = SupportRequest.objects.get()
        self.assertEqual(support_request.tenant_id, self.tenant.pk)
        self.assertEqual(support_request.status, SupportRequest.Status.OPEN)

    def test_a_client_cannot_choose_its_own_reference(self):
        self.raise_request(reference="AAAAAAAA")
        self.assertNotEqual(SupportRequest.objects.get().reference, "AAAAAAAA")


class IsolationTests(SupportTestCase):
    def test_a_customer_sees_only_its_own_requests(self):
        SupportRequest.objects.create(
            tenant=self.other, subject="Theirs", message="x" * 20, category="OTHER",
        )
        self.raise_request()

        response = self.client.get(reverse("support-request-list"))
        self.assertEqual(response.status_code, 200)
        subjects = [row["subject"] for row in response.data["results"]] if "results" in response.data else [
            row["subject"] for row in response.data
        ]
        self.assertNotIn("Theirs", subjects)

    def test_another_tenants_request_is_not_retrievable_by_id(self):
        foreign = SupportRequest.objects.create(
            tenant=self.other, subject="Theirs", message="x" * 20, category="OTHER",
        )
        response = self.client.get(
            reverse("support-request-detail", kwargs={"pk": foreign.pk}),
        )
        self.assertEqual(response.status_code, 404)

    def test_a_customer_cannot_reach_the_triage_endpoint(self):
        response = self.client.get(reverse("support-triage-list"))
        self.assertIn(response.status_code, (403, 404))

    def test_platform_staff_can_read_every_workspaces_requests(self):
        SupportRequest.objects.create(
            tenant=self.other, subject="Theirs", message="x" * 20, category="OTHER",
        )
        self.raise_request()

        staff_client = APIClient()
        staff_client.force_authenticate(self.staff)
        response = staff_client.get(reverse("support-triage-list"))
        self.assertEqual(response.status_code, 200)


class ContextSnapshotTests(SupportTestCase):
    def setUp(self):
        super().setUp()
        WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Sales line",
            status=WhatsAppAccount.Status.CONNECTED, phone_number="+919876543210",
        )
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user,
            name="Priya Sharma", phone="+919812345678",
        )

    def test_the_context_records_the_plan_and_the_device_state(self):
        context = services.capture_context(self.tenant)
        self.assertEqual(context["subscription"]["plan"], "Growth")
        self.assertEqual(context["devices"][0]["status"], "CONNECTED")

    def test_the_context_never_contains_a_phone_number(self):
        """
        The support view is the one screen where somebody legitimately looks at
        another company's workspace, which makes it the one screen where
        "show a bit more" must not accumulate.
        """
        blob = json.dumps(services.capture_context(self.tenant))
        self.assertNotIn("919876543210", blob)
        self.assertNotIn("919812345678", blob)

    def test_the_context_never_contains_a_contact_name(self):
        blob = json.dumps(services.capture_context(self.tenant))
        self.assertNotIn("Priya", blob)

    def test_the_context_never_contains_message_content(self):
        from apps.messaging.models import Message

        account = WhatsAppAccount.objects.filter(tenant=self.tenant).first()
        contact = Contact.objects.filter(tenant=self.tenant).first()
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=contact,
            body="A very secret offer only for you", status=Message.Status.FAILED,
        )
        blob = json.dumps(services.capture_context(self.tenant))
        self.assertNotIn("secret offer", blob)

    def test_the_context_survives_a_broken_subsystem(self):
        """
        A request raised *because* something is broken is the one that most
        needs the rest of the picture, so one failing block must not lose it.
        """
        from unittest.mock import patch

        with patch("apps.support.services._devices", side_effect=RuntimeError("boom")):
            context = services.capture_context(self.tenant)

        self.assertEqual(context["devices"], {"error": "unavailable"})
        self.assertIn("subscription", context)

    def test_a_campaign_id_from_another_tenant_reports_not_found(self):
        other_account = WhatsAppAccount.objects.create(
            tenant=self.other, user=self.other_user, name="Theirs",
            status=WhatsAppAccount.Status.CONNECTED, phone_number="+15000000009",
        )
        foreign = Campaign.objects.create(
            tenant=self.other, created_by=self.other_user, whatsapp_account=other_account,
            name="Their campaign", message_body="Hi", audience_type=Campaign.AudienceType.ALL,
        )
        context = services.capture_context(self.tenant, campaign_id=foreign.pk)
        self.assertFalse(context["campaign"]["found"])

    def test_failure_reasons_are_grouped_by_code_not_by_prose(self):
        from apps.messaging.models import Message

        account = WhatsAppAccount.objects.filter(tenant=self.tenant).first()
        for _ in range(3):
            Message.objects.create(
                tenant=self.tenant, whatsapp_account=account, body="x",
                status=Message.Status.FAILED,
                failure_reason=Message.FailureReason.OPTED_OUT,
                error_message="wording that could change tomorrow",
            )
        context = services.capture_context(self.tenant)
        reasons = {row["reason"]: row["count"] for row in context["failures"]["by_reason"]}
        self.assertEqual(reasons.get("opted_out"), 3)


class TriageTests(SupportTestCase):
    def setUp(self):
        super().setUp()
        self.raise_request()
        self.support_request = SupportRequest.objects.get()
        self.staff_client = APIClient()
        self.staff_client.force_authenticate(self.staff)

    def test_staff_can_change_the_status(self):
        response = self.staff_client.patch(
            reverse("support-triage-detail", kwargs={"pk": self.support_request.pk}),
            {"status": "RESOLVED"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.support_request.refresh_from_db()
        self.assertEqual(self.support_request.status, SupportRequest.Status.RESOLVED)
        self.assertIsNotNone(self.support_request.resolved_at)

    def test_staff_cannot_rewrite_what_the_customer_said(self):
        self.staff_client.patch(
            reverse("support-triage-detail", kwargs={"pk": self.support_request.pk}),
            {"message": "something else entirely"}, format="json",
        )
        self.support_request.refresh_from_db()
        self.assertNotEqual(self.support_request.message, "something else entirely")

    def test_a_status_change_is_audited(self):
        from apps.audit.models import AuditEvent

        self.staff_client.patch(
            reverse("support-triage-detail", kwargs={"pk": self.support_request.pk}),
            {"status": "IN_PROGRESS"}, format="json",
        )
        self.assertTrue(
            AuditEvent.objects.filter(action="support.request_updated").exists()
        )

    def test_the_internal_note_is_never_returned_to_the_customer(self):
        self.support_request.internal_note = "Third time this week, check their device."
        self.support_request.save()

        response = self.client.get(
            reverse("support-request-detail", kwargs={"pk": self.support_request.pk}),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("internal_note", response.data)
        self.assertNotIn("context", response.data)
