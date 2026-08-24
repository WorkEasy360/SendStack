from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.tenants.models import Tenant


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AuthenticationApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER
        )
        self.user = get_user_model().objects.create_user(
            username="acme-owner",
            email="owner@acme.test",
            password="StrongPass!123",
            tenant=self.tenant,
        )

    def test_login_includes_tenant_context(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            data={"username": "acme-owner", "password": "StrongPass!123"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.json())
        self.assertEqual(response.json()["user"]["tenant_id"], self.tenant.id)

    def test_password_reset_does_not_expose_account_existence(self):
        existing = self.client.post(
            "/api/v1/auth/password-reset/",
            data={"email": self.user.email},
            content_type="application/json",
        )
        unknown = self.client.post(
            "/api/v1/auth/password-reset/",
            data={"email": "missing@acme.test"},
            content_type="application/json",
        )

        self.assertEqual(existing.status_code, 200)
        self.assertEqual(existing.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_confirmation_changes_password(self):
        self.client.post(
            "/api/v1/auth/password-reset/",
            data={"email": self.user.email},
            content_type="application/json",
        )
        reset_url = mail.outbox[0].body.rsplit(" ", 1)[-1]
        query = parse_qs(urlparse(reset_url).query)
        response = self.client.post(
            "/api/v1/auth/password-reset/confirm/",
            data={
                "uid": query["uid"][0],
                "token": query["token"][0],
                "new_password": "ChangedPass!456",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("ChangedPass!456"))


class ManagedUserApiTests(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(
            name="Platform", slug="platform-test", kind=Tenant.Kind.PLATFORM
        )
        self.superadmin = get_user_model().objects.create_user(
            username="platform-admin", password="StrongPass!123", role="SUPERADMIN", tenant=self.platform
        )
        self.reseller_tenant = Tenant.objects.create(
            name="Partner", slug="partner-test", kind=Tenant.Kind.RESELLER, parent=self.platform
        )
        self.reseller = get_user_model().objects.create_user(
            username="partner-admin", password="StrongPass!123", role="RESELLER", tenant=self.reseller_tenant
        )

    def test_superadmin_can_create_a_reseller_with_its_own_tenant(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.post(
            "/api/v1/auth/users/",
            data={
                "username": "new-partner",
                "email": "new-partner@example.test",
                "password": "PartnerPass!456",
                "role": "RESELLER",
                "tenant_name": "New Partner",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        user = get_user_model().objects.get(username="new-partner")
        self.assertEqual(user.role, "RESELLER")
        self.assertEqual(user.tenant.parent_id, self.platform.id)

    def test_reseller_can_create_user_but_not_another_reseller(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.reseller)
        allowed = self.client.post(
            "/api/v1/auth/users/",
            data={
                "username": "client-owner",
                "email": "client@example.test",
                "password": "ClientOwner!456",
                "role": "USER",
                "tenant_name": "Client Business",
            },
            content_type="application/json",
        )
        forbidden = self.client.post(
            "/api/v1/auth/users/",
            data={
                "username": "bad-partner",
                "email": "bad@example.test",
                "password": "AnotherPass!456",
                "role": "RESELLER",
                "tenant_name": "Invalid Partner",
            },
            content_type="application/json",
        )

        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(forbidden.status_code, 403)

    def test_impersonation_token_is_invalid_after_exit(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)
        started = self.client.post(
            "/api/v1/auth/impersonations/",
            data={"user_id": self.reseller.pk, "reason": "Support request"},
            format="json",
        )

        self.assertEqual(started.status_code, 201)
        self.assertIn("impersonation_session_id", AccessToken(started.json()["access"]))

        impersonated_client = APIClient()
        impersonated_client.credentials(HTTP_AUTHORIZATION=f"Bearer {started.json()['access']}")
        self.assertEqual(impersonated_client.get("/api/v1/auth/me/").status_code, 200)
        self.assertEqual(impersonated_client.post("/api/v1/auth/impersonations/exit/").status_code, 200)
        self.assertEqual(impersonated_client.get("/api/v1/auth/me/").status_code, 401)

    def test_superadmin_deleting_reseller_reclaims_credits_to_superadmin(self):
        from apps.credits.services import assign_credits

        assign_credits(actor=self.superadmin, target=self.reseller, amount=300)
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.delete(f"/api/v1/auth/users/{self.reseller.pk}/")

        self.assertEqual(response.status_code, 204, response.content)
        self.reseller.refresh_from_db()
        self.superadmin.refresh_from_db()
        self.assertTrue(self.reseller.is_deleted)
        self.assertFalse(self.reseller.is_active)
        self.assertEqual(self.reseller.credits, 0)
        self.assertEqual(self.superadmin.credits, 300)

    def test_reseller_deleting_user_reclaims_credits_to_reseller(self):
        from apps.credits.services import assign_credits

        assign_credits(actor=self.superadmin, target=self.reseller, amount=300)
        user_tenant = Tenant.objects.create(
            name="Client Co", slug="client-co-test", kind=Tenant.Kind.CUSTOMER, parent=self.reseller_tenant
        )
        managed_user = get_user_model().objects.create_user(
            username="managed-user", password="StrongPass!123", role="USER", tenant=user_tenant
        )
        assign_credits(actor=self.reseller, target=managed_user, amount=50)
        self.client = APIClient()
        self.client.force_authenticate(user=self.reseller)

        response = self.client.delete(f"/api/v1/auth/users/{managed_user.pk}/")

        self.assertEqual(response.status_code, 204, response.content)
        managed_user.refresh_from_db()
        self.reseller.refresh_from_db()
        self.assertTrue(managed_user.is_deleted)
        self.assertEqual(managed_user.credits, 0)
        self.assertEqual(self.reseller.credits, 250 + 50)

    def test_deleted_user_no_longer_appears_in_listing_and_cannot_log_in(self):
        from apps.credits.services import assign_credits

        assign_credits(actor=self.superadmin, target=self.reseller, amount=300)
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)
        self.client.delete(f"/api/v1/auth/users/{self.reseller.pk}/")

        listing = self.client.get("/api/v1/auth/users/")
        ids = {row["id"] for row in listing.json()["results"]}
        self.assertNotIn(self.reseller.pk, ids)

        login = self.client.post(
            "/api/v1/auth/token/",
            data={"username": "partner-admin", "password": "StrongPass!123"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 401)

    def test_superadmin_cannot_delete_self(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.delete(f"/api/v1/auth/users/{self.superadmin.pk}/")

        self.assertEqual(response.status_code, 403)

    def test_cannot_delete_account_with_reserved_credits(self):
        from apps.credits.services import assign_credits, reserve_credits

        assign_credits(actor=self.superadmin, target=self.reseller, amount=300)
        reserve_credits(user_id=self.reseller.pk, amount=100, reference="campaign:test")
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)

        response = self.client.delete(f"/api/v1/auth/users/{self.reseller.pk}/")

        self.assertEqual(response.status_code, 400)
        self.reseller.refresh_from_db()
        self.assertFalse(self.reseller.is_deleted)


class SuperuserCreationTests(TestCase):
    def test_createsuperuser_lands_on_the_platform_tenant(self):
        user = get_user_model().objects.create_superuser(
            username="root", email="root@multisender.test", password="StrongPass!123"
        )

        self.assertEqual(user.role, get_user_model().Role.SUPERADMIN)
        self.assertIsNotNone(user.tenant)
        self.assertEqual(user.tenant.slug, "multisender-platform")
        self.assertEqual(user.tenant.kind, Tenant.Kind.PLATFORM)

    def test_explicit_tenant_is_respected(self):
        tenant = Tenant.objects.create(name="Beta", slug="beta", kind=Tenant.Kind.CUSTOMER)

        user = get_user_model().objects.create_superuser(
            username="beta-root", email="root@beta.test", password="StrongPass!123", tenant=tenant
        )

        self.assertEqual(user.tenant, tenant)


class ManagedUserBulkActionTests(TestCase):
    def setUp(self):
        self.platform = Tenant.objects.create(name="Platform", slug="platform-bulk", kind=Tenant.Kind.PLATFORM)
        self.superadmin = get_user_model().objects.create_user(
            username="platform-bulk-admin", password="StrongPass!123", role="SUPERADMIN", tenant=self.platform
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.superadmin)
        self.users = []
        for index in range(2):
            tenant = Tenant.objects.create(
                name=f"Customer {index}", slug=f"customer-bulk-{index}", kind=Tenant.Kind.CUSTOMER, parent=self.platform
            )
            self.users.append(
                get_user_model().objects.create_user(
                    username=f"customer-bulk-{index}", password="StrongPass!123", role="USER", tenant=tenant
                )
            )

    def _bulk(self, action, ids):
        return self.client.post("/api/v1/auth/users/bulk/", {"action": action, "ids": ids}, format="json")

    def test_bulk_suspend_then_activate(self):
        ids = [user.pk for user in self.users]

        self.assertEqual(self._bulk("suspend", ids).status_code, 200)
        self.assertEqual(get_user_model().objects.filter(pk__in=ids, is_active=True).count(), 0)

        self.assertEqual(self._bulk("activate", ids).status_code, 200)
        self.assertEqual(get_user_model().objects.filter(pk__in=ids, is_active=True).count(), 2)

    def test_bulk_delete_soft_deletes_each_account(self):
        response = self._bulk("delete", [user.pk for user in self.users])

        self.assertEqual(response.status_code, 200, response.content)
        for user in self.users:
            user.refresh_from_db()
            self.assertTrue(user.is_deleted)
            self.assertFalse(user.is_active)

    def test_you_cannot_delete_or_suspend_yourself_in_bulk(self):
        for action in ("delete", "suspend"):
            response = self._bulk(action, [self.superadmin.pk])

            self.assertEqual(response.status_code, 400, response.content)
            self.superadmin.refresh_from_db()
            self.assertTrue(self.superadmin.is_active)
            self.assertFalse(self.superadmin.is_deleted)

    def test_one_refusal_does_not_stop_the_others(self):
        response = self._bulk("suspend", [self.users[0].pk, self.superadmin.pk])

        self.assertEqual(response.status_code, 207, response.content)
        body = response.json()
        self.assertEqual((body["succeeded"], body["failed"]), (1, 1))
        self.users[0].refresh_from_db()
        self.assertFalse(self.users[0].is_active)
