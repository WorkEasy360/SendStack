"""
Tests for self-service registration.

Half of these are ordinary ("a signup creates a workspace"). The other half
are the ones worth having: a stranger POSTing a `role`, a `tenant_id` or a
`reseller_id` and getting them; a signup that creates a *reseller*; a
duplicate address answered differently from an invalid one. Those are the
failure modes that would turn a commercialization phase into a privilege
escalation, so each has its own test rather than being covered by implication.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from apps.legal.models import LegalAcceptance
from apps.onboarding.models import FunnelEvent, OnboardingState
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant

from . import registration

GOOD_PASSWORD = "correct-horse-battery-staple-42"

SIGNUP_ON = {
    "SELF_SERVICE_SIGNUP_ENABLED": True,
    "TRIALS_ENABLED": True,
    "BILLING_TRIAL_PLAN": "Trial",
    "BILLING_TRIAL_DAYS": 14,
}


def _body(**overrides):
    payload = {
        "full_name": "Priya Sharma",
        "email": "priya@example.com",
        "password": GOOD_PASSWORD,
        "company": "Sharma Traders",
        "accept_terms": True,
    }
    payload.update(overrides)
    return payload


class SignupBase(TestCase):
    def setUp(self):
        # The throttle and the per-address lockout both live in the cache,
        # which is one LocMemCache for the whole test process. Without this,
        # the fifth test in a class inherits the fourth test's rate-limit
        # budget and fails with a 429 that has nothing to do with what it is
        # testing -- and the endpoint's real 5/min limit makes that certain.
        cache.clear()
        self.client = APIClient()
        self.url = reverse("signup")
        # The platform tenant already exists: apps/accounts/migrations/
        # 0002_user_tenant.py creates it. Creating a second one here would
        # test against a tenant that `signup_parent_tenant` will never pick.
        self.platform = registration.signup_parent_tenant()
        self.trial_plan = Plan.objects.create(
            owner=None, name="Trial", price_cents=0, max_messages_per_month=500,
            max_contacts=500, max_devices=1, max_users=2, is_active=True,
        )


@override_settings(**SIGNUP_ON)
class SignupSuccessTests(SignupBase):
    def test_signup_creates_workspace_user_and_trial(self):
        response = self.client.post(self.url, _body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        user = get_user_model().objects.get(email="priya@example.com")
        tenant = user.tenant
        self.assertEqual(tenant.kind, Tenant.Kind.CUSTOMER)
        self.assertEqual(tenant.parent_id, self.platform.pk)
        self.assertEqual(tenant.name, "Sharma Traders")

        subscription = Subscription.objects.get(tenant=tenant)
        self.assertEqual(subscription.status, Subscription.Status.TRIALING)
        self.assertEqual(subscription.plan_id, self.trial_plan.pk)
        self.assertIsNotNone(subscription.trial_ends_at)

    def test_signup_returns_a_usable_session(self):
        response = self.client.post(self.url, _body(), format="json")
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["email"], "priya@example.com")
        self.assertEqual(response.data["next"], "/onboarding")

    def test_the_new_user_is_an_ordinary_customer(self):
        """Not a superadmin, not a reseller, not staff. The whole point."""
        self.client.post(self.url, _body(), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertEqual(user.role, user.Role.USER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_signup_records_legal_acceptance_at_the_server_version(self):
        from apps.legal.documents_registry import ACCEPTANCE_REQUIRED, current_version

        self.client.post(self.url, _body(), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        for slug in ACCEPTANCE_REQUIRED:
            acceptance = LegalAcceptance.objects.get(user=user, document=slug)
            self.assertEqual(acceptance.version, current_version(slug))
            self.assertEqual(acceptance.tenant_id, user.tenant_id)

    def test_marketing_consent_is_off_unless_asked_for(self):
        """Separate from accepting the terms, and never pre-ticked."""
        self.client.post(self.url, _body(), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertFalse(user.marketing_opt_in)

    def test_marketing_consent_is_recorded_when_given(self):
        self.client.post(self.url, _body(marketing_opt_in=True), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertTrue(user.marketing_opt_in)

    def test_email_is_normalised(self):
        self.client.post(self.url, _body(email="  PRIYA@Example.COM "), format="json")
        self.assertTrue(get_user_model().objects.filter(email="priya@example.com").exists())

    def test_onboarding_starts_guided(self):
        self.client.post(self.url, _body(), format="json")
        tenant = get_user_model().objects.get(email="priya@example.com").tenant
        state = OnboardingState.objects.get(tenant=tenant)
        self.assertTrue(state.is_guided)
        self.assertIsNotNone(state.started_at)

    def test_funnel_events_are_recorded(self):
        self.client.post(self.url, _body(), format="json")
        tenant = get_user_model().objects.get(email="priya@example.com").tenant
        names = set(FunnelEvent.objects.filter(tenant=tenant).values_list("name", flat=True))
        self.assertIn(FunnelEvent.Name.SIGNUP_COMPLETED, names)
        self.assertIn(FunnelEvent.Name.TRIAL_STARTED, names)

    def test_two_businesses_with_the_same_name_both_get_a_workspace(self):
        """Not an error, and must not be one."""
        self.client.post(self.url, _body(), format="json")
        self.client.post(self.url, _body(email="second@example.com"), format="json")

        tenants = Tenant.objects.filter(kind=Tenant.Kind.CUSTOMER).order_by("pk")
        self.assertEqual(tenants.count(), 2)
        self.assertNotEqual(tenants[0].slug, tenants[1].slug)
        self.assertNotEqual(tenants[0].name, tenants[1].name)


@override_settings(**SIGNUP_ON)
class SignupSecurityTests(SignupBase):
    """The requests a signup endpoint must refuse."""

    def test_a_caller_cannot_choose_their_role(self):
        self.client.post(self.url, _body(role="SUPERADMIN"), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertEqual(user.role, user.Role.USER)

    def test_a_caller_cannot_choose_their_tenant(self):
        """The classic: join somebody else's workspace by naming it."""
        victim = Tenant.objects.create(
            name="Victim Co", slug="victim-co", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        self.client.post(
            self.url,
            _body(tenant_id=victim.pk, tenant=victim.pk, owner_id=1, reseller_id=victim.pk),
            format="json",
        )
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertNotEqual(user.tenant_id, victim.pk)
        self.assertEqual(user.tenant.kind, Tenant.Kind.CUSTOMER)

    def test_signup_never_creates_a_reseller(self):
        self.client.post(self.url, _body(kind="RESELLER"), format="json")
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertEqual(user.tenant.kind, Tenant.Kind.CUSTOMER)
        self.assertFalse(Tenant.objects.filter(kind=Tenant.Kind.RESELLER).exists())

    def test_a_caller_cannot_name_a_private_plan(self):
        """A reseller's negotiated tier is not on offer to a stranger."""
        reseller = Tenant.objects.create(
            name="Reseller", slug="sec-reseller", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        private = Plan.objects.create(
            owner=reseller, name="Negotiated", price_cents=1, is_public=True, is_active=True,
        )
        self.client.post(self.url, _body(plan="Negotiated"), format="json")

        subscription = Subscription.objects.get(tenant__name__startswith="Sharma")
        self.assertNotEqual(subscription.plan_id, private.pk)
        self.assertEqual(subscription.plan_id, self.trial_plan.pk)

    def test_a_caller_cannot_name_a_non_public_plan(self):
        Plan.objects.create(
            owner=None, name="Internal", price_cents=1, is_public=False, is_active=True,
        )
        self.client.post(self.url, _body(plan="Internal"), format="json")
        subscription = Subscription.objects.get(tenant__name__startswith="Sharma")
        self.assertEqual(subscription.plan_id, self.trial_plan.pk)

    def test_a_public_plan_may_be_chosen(self):
        chosen = Plan.objects.create(
            owner=None, name="Starter", price_cents=99900, is_public=True, is_active=True,
            max_contacts=5000,
        )
        self.client.post(self.url, _body(plan="Starter"), format="json")
        subscription = Subscription.objects.get(tenant__name__startswith="Sharma")
        self.assertEqual(subscription.plan_id, chosen.pk)

    def test_a_duplicate_address_is_refused_without_confirming_it_exists(self):
        self.client.post(self.url, _body(), format="json")
        response = self.client.post(self.url, _body(company="Someone Else"), format="json")

        self.assertEqual(response.status_code, 400)
        detail = str(response.data.get("detail", ""))
        self.assertEqual(detail, registration.GENERIC_EMAIL_REFUSAL)
        # Nothing that says "taken", "exists" or "registered".
        for word in ("exists", "already registered", "taken", "UNIQUE"):
            self.assertNotIn(word.lower(), detail.lower())
        self.assertEqual(get_user_model().objects.filter(email="priya@example.com").count(), 1)

    def test_a_duplicate_address_leaves_no_orphan_workspace(self):
        """The transaction rolls the half-made workspace back with it."""
        self.client.post(self.url, _body(), format="json")
        before = Tenant.objects.count()
        self.client.post(self.url, _body(company="Ghost Co"), format="json")
        self.assertEqual(Tenant.objects.count(), before)

    def test_a_weak_password_is_refused(self):
        response = self.client.post(self.url, _body(password="password123"), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(get_user_model().objects.filter(email="priya@example.com").exists())

    def test_an_invalid_email_is_refused(self):
        response = self.client.post(self.url, _body(email="not-an-address"), format="json")
        self.assertEqual(response.status_code, 400)

    def test_terms_must_be_accepted(self):
        response = self.client.post(self.url, _body(accept_terms=False), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(get_user_model().objects.filter(email="priya@example.com").exists())
        self.assertFalse(LegalAcceptance.objects.exists())

    def test_omitting_the_terms_field_is_refused(self):
        payload = _body()
        payload.pop("accept_terms")
        response = self.client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_blank_company_is_refused(self):
        response = self.client.post(self.url, _body(company="   "), format="json")
        self.assertEqual(response.status_code, 400)

    def test_repeated_failures_lock_the_address(self):
        """
        The per-address counter, which an attacker rotating IPs cannot escape.
        Counted for *every* refusal, so it cannot itself become an oracle.
        """
        from core import lockout

        from .signup_views import SIGNUP_LOCKOUT_SCOPE

        for _ in range(lockout.max_attempts() + 1):
            lockout.record_failure(SIGNUP_LOCKOUT_SCOPE, "priya@example.com")

        response = self.client.post(self.url, _body(), format="json")
        self.assertEqual(response.status_code, 429)
        self.assertFalse(get_user_model().objects.filter(email="priya@example.com").exists())


class SignupDisabledTests(SignupBase):
    @override_settings(SELF_SERVICE_SIGNUP_ENABLED=False)
    def test_signup_is_refused_when_switched_off(self):
        response = self.client.post(self.url, _body(), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "SIGNUP_DISABLED")
        self.assertFalse(get_user_model().objects.filter(email="priya@example.com").exists())

    @override_settings(SELF_SERVICE_SIGNUP_ENABLED=False)
    def test_the_form_can_ask_whether_signups_are_open(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["signup_enabled"])

    @override_settings(**SIGNUP_ON)
    def test_the_form_is_told_the_trial_length_rather_than_hardcoding_it(self):
        response = self.client.get(self.url)
        self.assertEqual(response.data["trial_days"], 14)
        self.assertTrue(response.data["trials_enabled"])


@override_settings(
    SELF_SERVICE_SIGNUP_ENABLED=True, TRIALS_ENABLED=True, BILLING_TRIAL_PLAN="Nonexistent",
)
class SignupWithoutATrialPlanTests(SignupBase):
    def test_a_workspace_is_still_created_but_gets_no_subscription(self):
        """
        A misconfigured deployment. The account is real, the trial is not, and
        the response says so instead of pretending.
        """
        response = self.client.post(self.url, _body(), format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data["subscription"])
        self.assertIn("notice", response.data)
        user = get_user_model().objects.get(email="priya@example.com")
        self.assertFalse(Subscription.objects.filter(tenant=user.tenant).exists())


@override_settings(**SIGNUP_ON)
class SignupParentTenantTests(SignupBase):
    def test_a_reseller_parent_can_be_configured(self):
        reseller = Tenant.objects.create(
            name="White Label", slug="white-label", kind=Tenant.Kind.RESELLER, parent=self.platform,
        )
        with override_settings(SELF_SERVICE_SIGNUP_PARENT_SLUG="white-label"):
            self.client.post(self.url, _body(), format="json")

        user = get_user_model().objects.get(email="priya@example.com")
        self.assertEqual(user.tenant.parent_id, reseller.pk)

    def test_a_customer_tenant_may_not_be_the_parent(self):
        """
        A customer under a customer is not a shape this hierarchy has, and it
        would put one customer's data inside another's subtree.
        """
        customer = Tenant.objects.create(
            name="A Customer", slug="a-customer", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        with override_settings(SELF_SERVICE_SIGNUP_PARENT_SLUG="a-customer"):
            self.client.post(self.url, _body(), format="json")

        user = get_user_model().objects.get(email="priya@example.com")
        self.assertNotEqual(user.tenant.parent_id, customer.pk)
        self.assertEqual(user.tenant.parent_id, self.platform.pk)


@override_settings(**SIGNUP_ON)
class RegistrationHelperTests(SignupBase):
    def test_workspace_identity_avoids_both_constraints(self):
        Tenant.objects.create(
            name="Acme", slug="acme", kind=Tenant.Kind.CUSTOMER, parent=self.platform,
        )
        name, slug = registration.unique_workspace_identity("Acme", self.platform)
        self.assertNotEqual(name, "Acme")
        self.assertNotEqual(slug, "acme")

    def test_a_blank_company_still_yields_a_usable_identity(self):
        name, slug = registration.unique_workspace_identity("", self.platform)
        self.assertTrue(name)
        self.assertTrue(slug)

    def test_a_company_name_of_only_punctuation_still_slugs(self):
        """slugify("!!!") is "", and an empty slug would break the unique index."""
        name, slug = registration.unique_workspace_identity("!!!", self.platform)
        self.assertTrue(slug)
