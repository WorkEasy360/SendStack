"""
Customer suspension: what a suspended workspace can and cannot do.

Suspension is a deliberate operator action, usually for abuse or non-payment.
It has to take effect on the next request rather than whenever a signed token
happens to expire, and it must not delete anything.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.contacts.models import Contact
from apps.tenants.models import Tenant


class SuspensionTestCase(TestCase):
    password = "correct-horse-battery-staple"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-susp", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-susp", email="ada@susp.example", password=self.password, tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )
        self.client = APIClient()

    def suspend(self):
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

    def login(self):
        return self.client.post(
            "/api/v1/auth/token/", {"username": "ada-susp", "password": self.password}, format="json",
        )


class AuthenticationTests(SuspensionTestCase):
    def test_an_active_workspace_can_sign_in(self):
        self.assertEqual(self.login().status_code, 200)

    def test_a_suspended_workspace_cannot_sign_in(self):
        self.suspend()
        response = self.login()
        self.assertEqual(response.status_code, 401)

    def test_the_refusal_says_why(self):
        """
        Somebody who signs in successfully and is then refused by every screen
        has a much worse time than somebody who is told what happened.
        """
        self.suspend()
        self.assertIn("suspended", str(self.login().json()).lower())

    def test_an_existing_token_stops_working_immediately(self):
        """
        The important half. A JWT is valid because it is signed and stays valid
        for its whole lifetime; left to expire on its own, a suspended customer
        would keep full access for up to fifteen minutes and could keep
        refreshing indefinitely.
        """
        access = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)

        self.suspend()
        self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 401)

    def test_reactivating_restores_access(self):
        self.suspend()
        self.assertEqual(self.login().status_code, 401)

        self.tenant.is_active = True
        self.tenant.save(update_fields=["is_active"])
        self.assertEqual(self.login().status_code, 200)

    def test_a_user_with_no_workspace_is_unaffected(self):
        """A superadmin provisioned without a tenant must not be locked out by this."""
        loner = get_user_model().objects.create_user(
            username="root-susp", email="root@susp.example", password=self.password,
        )
        access = str(RefreshToken.for_user(loner).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(client.get("/api/v1/auth/me/").status_code, 200)


class DataPreservationTests(SuspensionTestCase):
    def test_suspension_deletes_nothing(self):
        """Suspension is a door being locked, not a bin being emptied."""
        self.suspend()
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Tenant.objects.filter(pk=self.tenant.pk).exists())

    def test_the_user_account_itself_stays_active(self):
        """
        Deliberately a different flag from `user.is_active`. Suspending a
        workspace must not silently disable each individual account, because
        reactivating would then have to remember which accounts were disabled
        before and which by the suspension.
        """
        self.suspend()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
