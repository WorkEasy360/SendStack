"""
Tests for the customer-facing account deletion request.

The behaviour under test is mostly what this endpoint **refuses** to do. It
does not delete, it does not accept a checkbox as confirmation, it does not
let one workspace ask for another's removal, and it does not trap a customer
who changes their mind.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contacts.models import Contact

from .models import AccountDeletionRequest, Tenant


class DeletionRequestTests(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(
            name="Platform", slug="del-platform", kind=Tenant.Kind.PLATFORM,
        )
        self.tenant = Tenant.objects.create(
            name="Leaving Co", slug="leaving-co", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.user = get_user_model().objects.create_user(
            username="leaving-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = reverse("tenant-deletion-request")

    def test_a_request_is_recorded_with_a_cooling_off_window(self):
        response = self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")

        self.assertEqual(response.status_code, 201)
        request_row = AccountDeletionRequest.objects.get(tenant=self.tenant)
        self.assertEqual(request_row.status, AccountDeletionRequest.Status.REQUESTED)
        self.assertGreater(request_row.eligible_at, timezone.now())

    def test_requesting_deletes_nothing(self):
        """The whole point. This records an ask; an operator carries it out."""
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Still here", phone="+15551239999",
        )
        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")

        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())
        self.assertTrue(Contact.objects.filter(tenant=self.tenant).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_the_workspace_name_must_be_typed(self):
        """A checkbox is one mis-click, and a business is on the other side of it."""
        for bad in ("", "yes", "DELETE", "leaving", "Leaving Corp"):
            response = self.client.post(self.url, {"confirm": bad}, format="json")
            self.assertEqual(response.status_code, 400, bad)
        self.assertFalse(AccountDeletionRequest.objects.exists())

    def test_the_confirmation_is_case_insensitive(self):
        """Typing the name is the point; matching capitals is not."""
        response = self.client.post(self.url, {"confirm": "leaving co"}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_the_response_names_the_date_nothing_happens_before(self):
        response = self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        row = AccountDeletionRequest.objects.get()
        self.assertIn(row.eligible_at.date().isoformat(), response.data["detail"])

    def test_a_customer_can_change_their_mind(self):
        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            AccountDeletionRequest.objects.get().status, AccountDeletionRequest.Status.CANCELLED,
        )

    def test_cancelling_with_nothing_open_is_refused_clearly(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 400)

    def test_only_one_request_may_be_open(self):
        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        response = self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(AccountDeletionRequest.objects.count(), 1)

    def test_a_new_request_may_be_made_after_cancelling(self):
        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        self.client.delete(self.url)
        response = self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(AccountDeletionRequest.objects.count(), 2)

    def test_the_state_is_visible_to_the_customer(self):
        """Not a quiet delay: they can see it, and see when it ends."""
        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["pending"])
        self.assertIn("retained", response.data)
        self.assertTrue(response.data["retained"])

    def test_a_reseller_cannot_delete_itself_this_way(self):
        """Deleting a reseller would cascade into other people's businesses."""
        reseller = Tenant.objects.create(
            name="A Reseller", slug="a-reseller", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        user = get_user_model().objects.create_user(
            username="reseller-owner", password="x", tenant=reseller,
            role=get_user_model().Role.RESELLER,
        )
        client = APIClient()
        client.force_authenticate(user)
        response = client.post(
            reverse("tenant-deletion-request"), {"confirm": "A Reseller"}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AccountDeletionRequest.objects.exists())

    def test_a_customer_cannot_request_deletion_of_another_workspace(self):
        """
        There is no id in the request at all -- the workspace is the caller's
        own. This asserts that adding one changes nothing.
        """
        victim = Tenant.objects.create(
            name="Victim Co", slug="victim-co", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        response = self.client.post(
            self.url,
            {"confirm": "Victim Co", "tenant": victim.pk, "tenant_id": victim.pk},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(AccountDeletionRequest.objects.filter(tenant=victim).exists())

    def test_the_request_is_audited(self):
        from apps.audit.models import AuditEvent

        self.client.post(self.url, {"confirm": "Leaving Co"}, format="json")
        self.assertTrue(
            AuditEvent.objects.filter(action="tenant.deletion_requested", tenant=self.tenant).exists()
        )

    def test_the_endpoint_needs_authentication(self):
        response = APIClient().get(self.url)
        self.assertIn(response.status_code, (401, 403))
