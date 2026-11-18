"""
Sign-in with WorkEasy360 Accounts, end to end, against a fake provider.

The provider (apps/workeasy360/fake_provider.py) is a real HTTP server on
127.0.0.1 that checks private_key_jwt (aud = the token endpoint) and PKCE the
way the platform does. The browser's half -- following the redirect to the
authorization endpoint and back -- is done in-process with Django's client.

The two-instance test lives in tests_instances.py.
"""

import os
import time
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import Tenant
from apps.workeasy360.fake_provider import FakeWorkEasy360
from apps.workeasy360.models import OrganizationLink

START = "/api/v1/auth/workeasy360/start/"
BACKCHANNEL = "/api/v1/auth/workeasy360/backchannel-logout/"
ME = "/api/v1/auth/me/"
CONTACTS = "/api/v1/contacts/contacts/"


def path_and_query(url):
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def query_of(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def reset_connector():
    from workeasy360_sso import discovery, entitlements
    from workeasy360_sso.conf import reset_config

    reset_config()
    discovery.reset_cache()
    entitlements.reset_state()
    cache.clear()


class WorkEasy360Mixin:
    """A fresh fake provider per test class, the product configured against it per test."""

    connector_overrides = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = FakeWorkEasy360()

    @classmethod
    def tearDownClass(cls):
        cls.provider.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.provider.entitlement = self.provider.allow
        self.provider.seen = {"token": [], "entitlement": []}
        override = override_settings(WORKEASY360_SSO=self.provider.connector_settings(**self.connector_overrides))
        override.enable()
        self.addCleanup(override.disable)
        reset_connector()
        self.addCleanup(reset_connector)

    # --- the browser ---------------------------------------------------------------

    def sign_in(self, client=None, person=None, *, tamper=None, error="", organization=None, next_path="/"):
        """Start, go to "WorkEasy360", come back. Returns (client, person, callback response)."""
        client = client or Client()
        person = person or self.provider.person()
        params = {"next": next_path}
        if organization:
            params["organization"] = organization
        start = client.get(START, params)
        self.assertEqual(start.status_code, 302, start.content)
        callback = self.provider.authorize(start["Location"], person, tamper=tamper, error=error)
        return client, person, client.get(path_and_query(callback))

    def assert_signed_in(self, response, destination="/"):
        self.assertEqual(response.status_code, 302, response.content)
        self.assertEqual(response["Location"], destination)

    def assert_refused(self, response, code):
        self.assertEqual(response.status_code, 302, response.content)
        location = response["Location"]
        self.assertTrue(location.startswith("/login?"), location)
        self.assertEqual(query_of(location).get("sso_error"), code, location)

    def assert_no_session(self, client):
        self.assertEqual(client.get(ME).status_code, 401)

    def user_for(self, person):
        from workeasy360_sso.models import FederatedIdentity

        identity = FederatedIdentity.objects.select_related("user").filter(
            issuer=self.provider.issuer, subject=person["sub"]
        ).first()
        return identity.user if identity else None

    def deliver(self, token, client=None):
        return (client or Client()).post(BACKCHANNEL, {"logout_token": token})


def make_password_user(tenant, username, *, role=None, email=None):
    user_model = get_user_model()
    return user_model.objects.create_user(
        username=username, email=email or f"{username}@example.test", password="a-Long-password-123",
        tenant=tenant, role=role or user_model.Role.USER,
    )


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


def platform_tenant():
    return Tenant.objects.filter(kind=Tenant.Kind.PLATFORM).order_by("pk").first()


class SignInTests(WorkEasy360Mixin, TestCase):
    def test_happy_path_pkce_private_key_jwt_and_a_server_side_session(self):
        client, person, response = self.sign_in(next_path="/inbox")
        self.assert_signed_in(response, "/inbox")

        # A server-side session in an httpOnly cookie -- and no token anywhere.
        cookie = response.cookies.get("sessionid")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie["httponly"])
        self.assertTrue(Session.objects.filter(session_key=cookie.value).exists())
        self.assertNotIn(b"access", response.content)

        me = client.get(ME)
        self.assertEqual(me.status_code, 200, me.content)
        data = me.json()["data"]
        self.assertTrue(data["workeasy360"]["session"])
        self.assertEqual(data["workeasy360"]["organization_id"], person["organization_id"])
        self.assertTrue(data["workeasy360"]["access"]["allowed"])

        # The code exchange: private_key_jwt with aud = the token endpoint, and PKCE.
        exchange = self.provider.seen["token"][0]
        self.assertEqual(exchange["assertion"]["aud"], self.provider.token_endpoint)
        self.assertNotEqual(exchange["assertion"]["aud"], self.provider.issuer)
        self.assertEqual(exchange["assertion"]["iss"], self.provider.client_id)
        self.assertEqual(exchange["assertion"]["sub"], self.provider.client_id)
        self.assertLessEqual(exchange["assertion"]["exp"] - exchange["assertion"]["iat"], 60)
        self.assertTrue(exchange["form"]["code_verifier"])

        # The organization's workspace: a customer directly under the platform tenant.
        user = self.user_for(person)
        link = OrganizationLink.objects.get(organization_id=person["organization_id"])
        self.assertEqual(user.tenant_id, link.tenant_id)
        self.assertEqual(link.tenant.kind, Tenant.Kind.CUSTOMER)
        self.assertEqual(link.tenant.parent_id, platform_tenant().pk)
        self.assertEqual(link.tenant.name, person["organization_name"])
        self.assertEqual(link.origin, OrganizationLink.Origin.FIRST_SIGN_IN)
        self.assertEqual(user.role, get_user_model().Role.USER)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.email, person["email"])

        # Entitlement was asked for the organization and the person, with private_key_jwt too.
        asked = self.provider.seen["entitlement"][0]
        self.assertEqual(asked["form"]["organization_id"], person["organization_id"])
        self.assertEqual(asked["form"]["subject"], person["sub"])
        self.assertEqual(asked["assertion"]["aud"], self.provider.token_endpoint)

    def test_the_same_person_is_the_same_account_and_colleagues_share_the_workspace(self):
        _, person, first = self.sign_in()
        self.assert_signed_in(first)
        _, _, again = self.sign_in(person=person)
        self.assert_signed_in(again)
        colleague = self.provider.person(organization_id=person["organization_id"])
        _, _, third = self.sign_in(person=colleague)
        self.assert_signed_in(third)

        user, other = self.user_for(person), self.user_for(colleague)
        self.assertEqual(get_user_model().objects.filter(workeasy360_identities__subject=person["sub"]).count(), 1)
        self.assertNotEqual(user.pk, other.pk)
        self.assertEqual(user.tenant_id, other.tenant_id)
        self.assertEqual(OrganizationLink.objects.filter(organization_id=person["organization_id"]).count(), 1)

    def test_state_and_nonce_are_single_use(self):
        client = Client()
        person = self.provider.person()
        start = client.get(START, {"next": "/"})
        callback = path_and_query(self.provider.authorize(start["Location"], person))
        self.assert_signed_in(client.get(callback))
        # The same callback again, in the same browser: the flow was spent.
        self.assert_refused(client.get(callback), "expired")
        # A callback for a flow this browser never started.
        self.assert_refused(Client().get(callback), "expired")

    def test_an_id_token_from_another_issuer_is_refused(self):
        client, person, response = self.sign_in(tamper={"issuer": "https://elsewhere.example"})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_an_id_token_for_another_audience_is_refused(self):
        client, person, response = self.sign_in(tamper={"audience": "another-product"})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_an_id_token_with_a_bad_signature_is_refused(self):
        client, person, response = self.sign_in(tamper={"foreign_key": True})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_an_id_token_with_another_nonce_is_refused(self):
        client, person, response = self.sign_in(tamper={"nonce": "not-this-browsers"})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)

    def test_an_access_token_presented_as_an_id_token_is_refused(self):
        client, _, response = self.sign_in(tamper={"typ": "at+jwt"})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)

    def test_a_token_for_another_product_is_refused(self):
        client, person, response = self.sign_in(tamper={"product_code": "easy_crm"})
        self.assert_refused(response, "wrong_product")
        self.assert_no_session(client)
        self.assertFalse(OrganizationLink.objects.filter(organization_id=person["organization_id"]).exists())

    def test_a_token_without_an_organization_is_refused(self):
        client, person, response = self.sign_in(tamper={"drop": ["organization_id"]})
        self.assert_refused(response, "refused")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_an_unverified_email_is_refused(self):
        client, person, response = self.sign_in(person=self.provider.person(email_verified=False))
        self.assert_refused(response, "email_unverified")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_the_launchers_organization_must_be_the_tokens(self):
        organization = str(uuid.uuid4())
        client, _, response = self.sign_in(organization=organization, tamper={"organization_id": str(uuid.uuid4())})
        self.assert_refused(response, "wrong_organization")
        self.assert_no_session(client)
        # And the launcher's organization is forwarded to WorkEasy360.
        start = Client().get(START, {"organization": organization})
        self.assertEqual(query_of(start["Location"]).get("organization"), organization)

    def test_an_organization_that_is_not_entitled_gets_nothing(self):
        self.provider.entitlement = self.provider.deny("not_entitled")
        client, person, response = self.sign_in()
        self.assert_refused(response, "not_entitled")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))
        self.assertFalse(OrganizationLink.objects.filter(organization_id=person["organization_id"]).exists())

    def test_workeasy360s_own_refusal_is_shown_as_such(self):
        client, _, response = self.sign_in(error="product_not_entitled")
        self.assert_refused(response, "not_entitled")
        _, _, cancelled = self.sign_in(error="access_denied")
        self.assert_refused(cancelled, "cancelled")
        self.assert_no_session(client)

    def test_a_refusal_of_this_products_client_fails_closed_at_sign_in(self):
        self.provider.entitlement = lambda organization_id, subject: (401, {"error": "invalid_client"})
        client, person, response = self.sign_in()
        self.assert_refused(response, "access_unverified")
        self.assert_no_session(client)
        self.assertIsNone(self.user_for(person))

    def test_an_address_that_already_has_an_account_is_never_taken_over(self):
        workspace = Tenant.objects.create(name="Existing", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        existing = make_password_user(workspace, "existing", email="taken@example.test")
        client, person, response = self.sign_in(person=self.provider.person(email="Taken@example.test"))
        self.assert_refused(response, "account_conflict")
        self.assert_no_session(client)
        existing.refresh_from_db()
        self.assertTrue(existing.has_usable_password())

    def test_no_workspace_is_created_when_provisioning_is_off(self):
        with mock.patch.dict(os.environ, {"WORKEASY360_PROVISION_TENANTS": "false"}):
            client, person, response = self.sign_in()
        self.assert_refused(response, "no_workspace")
        self.assertIsNone(self.user_for(person))

    def test_reseller_and_platform_accounts_cannot_be_used_through_workeasy360(self):
        from workeasy360_sso.models import FederatedIdentity

        reseller_tenant = Tenant.objects.create(name="Reseller", kind=Tenant.Kind.RESELLER, parent=platform_tenant())
        reseller = make_password_user(reseller_tenant, "reseller", role=get_user_model().Role.RESELLER)
        person = self.provider.person()
        FederatedIdentity.objects.create(user=reseller, issuer=self.provider.issuer, subject=person["sub"])
        client, _, response = self.sign_in(person=person)
        self.assert_refused(response, "role_not_allowed")
        self.assert_no_session(client)

    def test_the_sign_in_page_is_told_what_it_needs(self):
        status = Client().get("/api/v1/auth/workeasy360/session/").json()
        self.assertTrue(status["enabled"])
        self.assertFalse(status["sso_only"])
        self.assertFalse(status["signed_in"])
        self.assertEqual(status["start_url"], START)
        self.assertTrue(status["csrf_token"])
        self.assertEqual(status["logout_workeasy360_url"], "/api/v1/auth/workeasy360/logout/workeasy360/")
        self.assertIn("other_organization", status["messages"])
        with override_settings(WORKEASY360_SSO={}):
            reset_connector()
            off = Client().get("/api/v1/auth/workeasy360/session/").json()
        self.assertFalse(off["enabled"])
        self.assertEqual(off["start_url"], "")


class TenancyTests(WorkEasy360Mixin, TestCase):
    def test_a_person_never_crosses_into_a_second_organization(self):
        _, person, first = self.sign_in()
        self.assert_signed_in(first)
        workspace = self.user_for(person).tenant_id
        client, _, second = self.sign_in(person=dict(person, organization_id=str(uuid.uuid4())))
        self.assert_refused(second, "other_organization")
        self.assert_no_session(client)
        self.assertEqual(self.user_for(person).tenant_id, workspace)

    def test_a_session_reaches_only_its_own_workspace(self):
        from apps.contacts.models import Contact

        client_a, person_a, response_a = self.sign_in()
        client_b, person_b, response_b = self.sign_in()
        self.assert_signed_in(response_a)
        self.assert_signed_in(response_b)
        user_a, user_b = self.user_for(person_a), self.user_for(person_b)
        self.assertNotEqual(user_a.tenant_id, user_b.tenant_id)
        Contact.objects.create(tenant=user_a.tenant, created_by=user_a, name="Only A", phone="+15551110001")
        Contact.objects.create(tenant=user_b.tenant, created_by=user_b, name="Only B", phone="+15552220002")

        listed = client_a.get(CONTACTS)
        self.assertEqual(listed.status_code, 200, listed.content)
        names = [row["name"] for row in listed.json()["results"]]
        self.assertEqual(names, ["Only A"])
        other = Contact.objects.get(name="Only B")
        self.assertIn(client_a.get(f"{CONTACTS}{other.pk}/").status_code, (403, 404))
        self.assertEqual(client_a.get(ME).json()["data"]["tenant_id"], user_a.tenant_id)

    def test_a_session_whose_workspace_was_re_pointed_is_ended(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        user = self.user_for(person)
        # Somebody moves the account to another workspace by hand.
        other = Tenant.objects.create(name="Elsewhere", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        get_user_model().objects.filter(pk=user.pk).update(tenant=other)
        self.assertEqual(client.get(ME).status_code, 401)

    def test_operator_links_an_existing_workspace_and_an_existing_account(self):
        workspace = Tenant.objects.create(name="Acme", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        owner = make_password_user(workspace, "acme-owner", email="owner@acme.test")
        RefreshToken.for_user(owner)  # an outstanding password session
        organization_id = str(uuid.uuid4())
        call_command("w360_link_organization", str(workspace.pk), organization_id, "--actor", "ops@test")
        link = OrganizationLink.objects.get(organization_id=organization_id)
        self.assertEqual((link.tenant_id, link.origin), (workspace.pk, OrganizationLink.Origin.OPERATOR))

        person = self.provider.person(organization_id=organization_id, email="owner@acme.test")
        call_command("w360_link_user", str(owner.pk), person["sub"], "--actor", "ops@test")
        owner.refresh_from_db()
        self.assertFalse(owner.has_usable_password())
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        self.assertTrue(BlacklistedToken.objects.filter(token__user=owner).exists())

        client, _, response = self.sign_in(person=person)
        self.assert_signed_in(response)
        self.assertEqual(self.user_for(person).pk, owner.pk)
        self.assertEqual(client.get(ME).json()["data"]["tenant_id"], workspace.pk)

        # A colleague's first sign-in joins the same workspace.
        _, colleague, joined = self.sign_in(person=self.provider.person(organization_id=organization_id))
        self.assert_signed_in(joined)
        self.assertEqual(self.user_for(colleague).tenant_id, workspace.pk)

    def test_operator_links_are_never_re_pointed_and_never_a_resellers(self):
        platform = platform_tenant()
        first = Tenant.objects.create(name="First", kind=Tenant.Kind.CUSTOMER, parent=platform)
        second = Tenant.objects.create(name="Second", kind=Tenant.Kind.CUSTOMER, parent=platform)
        organization_id = str(uuid.uuid4())
        call_command("w360_link_organization", str(first.pk), organization_id, "--actor", "ops")
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(second.pk), organization_id, "--actor", "ops")
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(first.pk), str(uuid.uuid4()), "--actor", "ops")
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(platform.pk), str(uuid.uuid4()), "--actor", "ops")
        reseller = Tenant.objects.create(name="Resells", kind=Tenant.Kind.RESELLER, parent=platform)
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(reseller.pk), str(uuid.uuid4()), "--actor", "ops")
        under_reseller = Tenant.objects.create(name="Their customer", kind=Tenant.Kind.CUSTOMER, parent=reseller)
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(under_reseller.pk), str(uuid.uuid4()), "--actor", "ops")
        call_command(
            "w360_link_organization", str(under_reseller.pk), str(uuid.uuid4()), "--actor", "ops",
            "--detach-from-reseller",
        )
        under_reseller.refresh_from_db()
        self.assertEqual(under_reseller.parent_id, platform.pk)
        # An organization whose first sign-in made its workspace is not re-pointed either.
        _, person, response = self.sign_in()
        self.assert_signed_in(response)
        with self.assertRaises(CommandError):
            call_command("w360_link_organization", str(second.pk), person["organization_id"], "--actor", "ops")

    def test_operator_never_merges_two_accounts(self):
        _, person, response = self.sign_in()
        self.assert_signed_in(response)
        workspace = self.user_for(person).tenant
        other = make_password_user(workspace, "someone-else")
        with self.assertRaises(CommandError):
            call_command("w360_link_user", str(other.pk), person["sub"], "--actor", "ops")
        unlinked = Tenant.objects.create(name="Unlinked", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        stray = make_password_user(unlinked, "stray")
        with self.assertRaises(CommandError):
            call_command("w360_link_user", str(stray.pk), str(uuid.uuid4()), "--actor", "ops")


class ResellerAndImpersonationTests(WorkEasy360Mixin, TestCase):
    def setUp(self):
        super().setUp()
        user_model = get_user_model()
        platform = platform_tenant()
        self.superadmin = make_password_user(platform, "operator", role=user_model.Role.SUPERADMIN)
        self.reseller_tenant = Tenant.objects.create(name="Reseller Co", kind=Tenant.Kind.RESELLER, parent=platform)
        self.reseller = make_password_user(self.reseller_tenant, "reseller", role=user_model.Role.RESELLER)
        self.customer_tenant = Tenant.objects.create(
            name="Reseller's customer", kind=Tenant.Kind.CUSTOMER, parent=self.reseller_tenant,
        )
        self.customer = make_password_user(self.customer_tenant, "customer")

    def impersonate(self, actor, target, client=None):
        return (client or Client()).post(
            "/api/v1/auth/impersonations/", {"user_id": target.pk, "reason": "support"},
            content_type="application/json", **(bearer(actor) if actor else {}),
        )

    def listed_users(self, actor):
        response = Client().get("/api/v1/auth/users/", {"page_size": 200}, **bearer(actor))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return {row["id"] for row in body.get("results", body)}

    def test_a_reseller_loses_a_customer_whose_workspace_joins_workeasy360(self):
        from apps.credits.services import visible_accounts

        self.assertIn(self.customer.pk, self.listed_users(self.reseller))
        self.assertEqual(self.impersonate(self.reseller, self.customer).status_code, 201)

        call_command(
            "w360_link_organization", str(self.customer_tenant.pk), str(uuid.uuid4()),
            "--actor", "ops", "--detach-from-reseller",
        )
        self.customer.refresh_from_db()
        self.assertNotIn(self.customer.pk, self.listed_users(self.reseller))
        self.assertEqual(self.impersonate(self.reseller, self.customer).status_code, 403)
        self.assertFalse(self.reseller.can_manage(self.customer))
        self.assertNotIn(self.customer.pk, set(visible_accounts(self.reseller).values_list("pk", flat=True)))
        customers = Client().get("/api/v1/tenants/customers/", **bearer(self.reseller)).json()["customers"]
        self.assertNotIn(self.customer_tenant.pk, [row.get("id") for row in customers])

    def test_even_a_link_left_under_a_reseller_is_out_of_its_reach(self):
        # Data an operator should never produce (the command refuses it), checked anyway.
        OrganizationLink.objects.create(
            organization_id=uuid.uuid4(), tenant=self.customer_tenant, origin=OrganizationLink.Origin.OPERATOR,
        )
        self.assertNotIn(self.customer.pk, self.listed_users(self.reseller))
        self.assertEqual(self.impersonate(self.reseller, self.customer).status_code, 403)
        self.assertEqual(
            Client().delete(f"/api/v1/auth/users/{self.customer.pk}/", **bearer(self.reseller)).status_code, 404,
        )

    def test_impersonation_never_mints_a_workeasy360_identity(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        w360_user = self.user_for(person)

        # Nobody impersonates a WorkEasy360 person, not even the operator...
        refused = self.impersonate(self.superadmin, w360_user)
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertEqual(refused.json()["code"], "workeasy360_account")
        # ...nor anybody else in a WorkEasy360 organization's workspace.
        colleague = make_password_user(w360_user.tenant, "password-colleague")
        self.assertEqual(self.impersonate(self.superadmin, colleague).status_code, 403)

        # A SendStack token for the WorkEasy360 person -- however it was minted -- is refused.
        token_use = Client().get(ME, **bearer(w360_user))
        self.assertEqual(token_use.status_code, 401)
        self.assertEqual(token_use.json()["code"], "workeasy360_account")

        # A WorkEasy360 session acts as nobody else.
        self.assertEqual(self.impersonate(None, self.customer, client=client).status_code, 403)
        exit_attempt = client.post("/api/v1/auth/impersonations/exit/")
        self.assertEqual(exit_attempt.status_code, 400)

        # An impersonation that predates the link stops at the link -- the
        # workspace's link alone ends it, before any person is linked.
        started = self.impersonate(self.reseller, self.customer)
        self.assertEqual(started.status_code, 201)
        token = started.json()["access"]
        self.assertEqual(Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {token}").status_code, 200)
        person2 = self.provider.person()
        call_command(
            "w360_link_organization", str(self.customer_tenant.pk), person2["organization_id"],
            "--actor", "ops", "--detach-from-reseller",
        )
        ended = Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(ended.status_code, 401)
        self.assertEqual(ended.json()["code"], "workeasy360_account")
        # The customer's own password session still works, under the organization's entitlement.
        self.assertEqual(Client().get(ME, **bearer(self.customer)).status_code, 200)
        call_command("w360_link_user", str(self.customer.pk), person2["sub"], "--actor", "ops")
        self.assertEqual(Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {token}").status_code, 401)
        self.assertEqual(Client().get(ME, **bearer(self.customer)).status_code, 401)

    def test_a_workeasy360_session_has_no_password_to_change_or_reset(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        change = client.post(
            "/api/v1/auth/password-change/",
            {"current_password": "x", "new_password": "Another-long-pass-1"},
            content_type="application/json",
        )
        self.assertEqual(change.status_code, 403)
        with mock.patch("apps.accounts.emails.send_password_reset") as sent:
            reset = Client().post(
                "/api/v1/auth/password-reset/", {"email": person["email"]}, content_type="application/json",
            )
        self.assertEqual(reset.status_code, 200)
        sent.assert_not_called()


class EntitlementTests(WorkEasy360Mixin, TestCase):
    def signed_in(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        return client, person

    def later(self, seconds):
        return mock.patch("workeasy360_sso.entitlements._clock", return_value=time.time() + seconds)

    def test_asked_on_workeasy360s_schedule_not_per_request(self):
        client, _ = self.signed_in()
        asked = len(self.provider.seen["entitlement"])
        for _ in range(3):
            self.assertEqual(client.get(CONTACTS).status_code, 200)
        self.assertEqual(len(self.provider.seen["entitlement"]), asked)
        with self.later(901):
            self.assertEqual(client.get(CONTACTS).status_code, 200)
        self.assertEqual(len(self.provider.seen["entitlement"]), asked + 1)

    def test_a_denial_ends_access_but_keeps_the_session_to_explain_it(self):
        client, _ = self.signed_in()
        self.provider.entitlement = self.provider.deny("expired")
        with self.later(901):
            refused = client.get(CONTACTS)
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["code"], "workeasy360_access_ended")
            self.assertEqual(refused.json()["reason"], "expired")
            me = client.get(ME)
            self.assertEqual(me.status_code, 200)
            self.assertFalse(me.json()["data"]["workeasy360"]["access"]["allowed"])
            # An outage right after never hands access back.
            self.provider.entitlement = lambda organization_id, subject: (503, {"error": "temporarily_unavailable"})
            self.assertEqual(client.get(CONTACTS).status_code, 403)

    def test_read_only_allows_reads_and_refuses_writes(self):
        client, _ = self.signed_in()

        def read_only(organization_id, subject):
            status, body = self.provider.allow(organization_id, subject)
            return status, dict(body, write_allowed=False)

        self.provider.entitlement = read_only
        with self.later(901):
            self.assertEqual(client.get(CONTACTS).status_code, 200)
            write = client.post(CONTACTS, {"name": "New", "phone": "+15550001111"}, content_type="application/json")
            self.assertEqual(write.status_code, 403)
            self.assertEqual(write.json()["code"], "workeasy360_read_only")

    def test_an_outage_is_never_a_denial(self):
        client, _ = self.signed_in()
        self.provider.entitlement = lambda organization_id, subject: (503, {"error": "temporarily_unavailable"})
        with self.later(901):
            self.assertEqual(client.get(CONTACTS).status_code, 200)

    def test_a_refusal_of_this_products_client_fails_closed(self):
        client, _ = self.signed_in()
        self.provider.entitlement = lambda organization_id, subject: (401, {"error": "invalid_client"})
        with self.later(901):
            refused = client.get(CONTACTS)
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["code"], "workeasy360_access_unverified")

    def test_password_sessions_in_a_linked_workspace_answer_to_the_organization(self):
        workspace = Tenant.objects.create(name="Linked", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        member = make_password_user(workspace, "member")
        organization_id = str(uuid.uuid4())
        call_command("w360_link_organization", str(workspace.pk), organization_id, "--actor", "ops")
        self.assertEqual(Client().get(CONTACTS, **bearer(member)).status_code, 200)
        asked = self.provider.seen["entitlement"][-1]["form"]
        self.assertEqual(asked["organization_id"], organization_id)
        self.assertNotIn("subject", asked)
        self.provider.entitlement = self.provider.deny("not_entitled")
        with self.later(901):
            refused = Client().get(CONTACTS, **bearer(member))
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["code"], "workeasy360_access_ended")

    def test_unlinked_password_workspaces_are_untouched(self):
        workspace = Tenant.objects.create(name="Plain", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        member = make_password_user(workspace, "plain-member")
        self.provider.entitlement = self.provider.deny("not_entitled")
        self.assertEqual(Client().get(CONTACTS, **bearer(member)).status_code, 200)
        self.assertEqual(self.provider.seen["entitlement"], [])


class BackchannelLogoutTests(WorkEasy360Mixin, TestCase):
    def test_a_logout_token_naming_the_sid_ends_that_session_at_once(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        session_key = client.cookies["sessionid"].value
        self.assertEqual(client.get(ME).status_code, 200)

        answer = self.deliver(self.provider.logout_token(sid=person["sid"], sub=person["sub"]))
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(client.get(ME).status_code, 401)
        self.assertFalse(Session.objects.filter(session_key=session_key).exists())
        from workeasy360_sso.models import ProductSession
        self.assertEqual(ProductSession.objects.get(identity__subject=person["sub"]).end_reason, "backchannel_logout")

    def test_a_logout_token_naming_only_the_person_ends_all_their_sessions_and_nobody_elses(self):
        person = self.provider.person()
        laptop, _, first = self.sign_in(person=person)
        phone, _, second = self.sign_in(person=dict(person, sid="another-sid"))
        bystander, _, third = self.sign_in(person=self.provider.person(organization_id=person["organization_id"]))
        for response in (first, second, third):
            self.assert_signed_in(response)
        self.assertEqual(self.deliver(self.provider.logout_token(sub=person["sub"])).status_code, 200)
        self.assertEqual(laptop.get(ME).status_code, 401)
        self.assertEqual(phone.get(ME).status_code, 401)
        self.assertEqual(bystander.get(ME).status_code, 200)

    def test_forged_logout_tokens_are_refused_and_end_nothing(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        sid, sub = person["sid"], person["sub"]
        token = self.provider.logout_token
        forged = {
            "bad signature": token(sid=sid, foreign_key=True),
            "another issuer": token(sid=sid, issuer="https://elsewhere.example"),
            "another audience": token(sid=sid, audience="another-product"),
            "not typ logout+jwt": token(sid=sid, typ="JWT"),
            "no logout event": token(sid=sid, event=False),
            "a nonce": token(sid=sid, nonce="n-1"),
            "too old": token(sid=sid, issued_at=time.time() - 3600, lifetime=7200),
            "expired": token(sid=sid, issued_at=time.time() - 600, lifetime=120),
            "neither sub nor sid": token(),
            "an ID token": self.provider.id_token({
                "person": person, "tamper": {}, "nonce": "n", "redirect_uri": "", "challenge": "", "issued_at": 0,
            }),
            "not a JWT": "not-a-token",
        }
        for kind, forgery in forged.items():
            with self.subTest(kind):
                self.assertEqual(self.deliver(forgery).status_code, 400, kind)
                self.assertEqual(client.get(ME).status_code, 200, kind)
        self.assertEqual(Client().post(BACKCHANNEL, {}).status_code, 400)
        self.assertIsNotNone(sub)

    def test_a_replayed_logout_token_ends_nothing_more(self):
        _, person, response = self.sign_in()
        self.assert_signed_in(response)
        token = self.provider.logout_token(sub=person["sub"])
        self.assertEqual(self.deliver(token).status_code, 200)
        # The person signs in again; somebody replays the captured token.
        client, _, again = self.sign_in(person=dict(person, sid="fresh-sid"))
        self.assert_signed_in(again)
        replay = self.deliver(token)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(client.get(ME).status_code, 200)

    def test_a_sign_in_in_flight_for_an_ended_workeasy360_session_is_refused(self):
        person = self.provider.person()
        client = Client()
        start = client.get(START, {"next": "/"})
        callback = self.provider.authorize(start["Location"], person)
        self.assertEqual(self.deliver(self.provider.logout_token(sid=person["sid"])).status_code, 200)
        self.assert_refused(client.get(path_and_query(callback)), "session_ended")


class SignOutTests(WorkEasy360Mixin, TestCase):
    def test_signing_out_of_sendstack_ends_only_the_sendstack_session(self):
        client, person, response = self.sign_in()
        self.assert_signed_in(response)
        out = client.post("/api/v1/auth/workeasy360/logout/")
        self.assertEqual(out.status_code, 302)
        self.assertEqual(out["Location"], "/login")
        self.assertEqual(client.get(ME).status_code, 401)
        self.assertEqual(out.cookies["w360sso_signed_out"].value, "1")

    def test_signing_out_of_workeasy360_too_goes_to_its_end_session_endpoint(self):
        client, _, response = self.sign_in()
        self.assert_signed_in(response)
        out = client.post("/api/v1/auth/workeasy360/logout/workeasy360/")
        self.assertEqual(out.status_code, 302)
        target = urlsplit(out["Location"])
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}",
                         f"{self.provider.issuer}/api/v1/integrations/oauth/logout")
        self.assertEqual(query_of(out["Location"])["client_id"], self.provider.client_id)
        self.assertEqual(query_of(out["Location"])["post_logout_redirect_uri"], self.provider.post_logout_redirect_uri)
        self.assertEqual(client.get(ME).status_code, 401)

    def test_a_cookie_session_must_prove_its_writes_come_from_sendstack(self):
        client = Client(enforce_csrf_checks=True)
        client, _, response = self.sign_in(client=client)
        self.assert_signed_in(response)
        blocked = client.post(CONTACTS, {"name": "X", "phone": "+15550002222"}, content_type="application/json")
        self.assertEqual(blocked.status_code, 403)
        token = client.get("/api/v1/auth/workeasy360/session/").json()["csrf_token"]
        allowed = client.post(
            CONTACTS, {"name": "X", "phone": "+15550002222"}, content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertIn(allowed.status_code, (200, 201), allowed.content)

    def test_the_django_admins_session_is_not_an_api_credential(self):
        operator = make_password_user(platform_tenant(), "admin-op", role=get_user_model().Role.SUPERADMIN)
        client = Client()
        client.force_login(operator)
        self.assertEqual(client.get(ME).status_code, 401)


class PasswordSignInTests(WorkEasy360Mixin, TestCase):
    def setUp(self):
        super().setUp()
        self.workspace = Tenant.objects.create(name="Pw", kind=Tenant.Kind.CUSTOMER, parent=platform_tenant())
        self.member = make_password_user(self.workspace, "pw-member")

    def password_login(self):
        return Client().post(
            "/api/v1/auth/token/", {"username": "pw-member", "password": "a-Long-password-123"},
            content_type="application/json",
        )

    def test_password_sign_in_keeps_working_unless_sso_only(self):
        login = self.password_login()
        self.assertEqual(login.status_code, 200, login.content)
        self.assertEqual(Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}").status_code, 200)

    def test_sso_only_closes_every_password_door_at_once(self):
        login = self.password_login()
        access, refresh = login.json()["access"], login.json()["refresh"]
        with override_settings(WORKEASY360_SSO=self.provider.connector_settings(SSO_ONLY=True)):
            reset_connector()
            refused = self.password_login()
            self.assertEqual(refused.status_code, 403)
            self.assertEqual(refused.json()["code"], "password_sign_in_disabled")
            existing = Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")
            self.assertEqual(existing.status_code, 401)
            self.assertEqual(existing.json()["code"], "workeasy360_sign_in_required")
            renew = Client().post("/api/v1/auth/token/refresh/", {"refresh": refresh}, content_type="application/json")
            self.assertEqual(renew.status_code, 401)
            self.assertFalse(Client().get("/api/v1/auth/signup/").json()["signup_enabled"])
            self.assertTrue(Client().get("/api/v1/auth/workeasy360/session/").json()["sso_only"])
            # WorkEasy360 itself still signs people in.
            client, _, response = self.sign_in()
            self.assert_signed_in(response)
            self.assertEqual(client.get(ME).status_code, 200)
