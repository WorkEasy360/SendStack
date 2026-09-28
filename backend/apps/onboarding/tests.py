"""
Tests for onboarding progress and the activation metric.

The two properties worth stating up front, because most of these tests are one
of them:

* **Progress is observed, never reported.** There is no endpoint that marks a
  step done, so a client cannot tick the checklist and cannot manufacture an
  activation. The tests check that milestones follow the underlying rows.
* **It is resumable, and it works for customers who came before it.** Closing
  the browser loses nothing, because nothing was being held in the browser;
  and an existing workspace with a year of campaigns is not dropped into a
  "connect your WhatsApp" wizard.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.campaigns.models import Campaign
from apps.compliance.models import CompliancePolicy
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.subscriptions.models import Plan, Subscription
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services
from .models import FunnelEvent, OnboardingState


def _account(tenant, user, status=WhatsAppAccount.Status.CONNECTED):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=status)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class OnboardingBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="New Co", slug="new-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="newco-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _accept_legal(self):
        from apps.legal.documents_registry import ACCEPTANCE_REQUIRED
        from apps.legal.services import record_acceptance

        for slug in ACCEPTANCE_REQUIRED:
            record_acceptance(self.user, slug)

    def _connect(self):
        return _account(self.tenant, self.user)

    def _contact(self, phone="+15551112222", subscribed=True, consent=None):
        return Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone=phone,
            subscribed=subscribed,
            marketing_consent=consent or Contact.MarketingConsent.UNKNOWN,
        )

    def _sent_message(self, account, contact):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=contact,
            to_phone=contact.phone, body="Hello", status=Message.Status.SENT,
            sent_at=timezone.now(),
        )


class MilestoneObservationTests(OnboardingBase):
    def test_a_new_workspace_starts_with_only_the_account_step(self):
        state = services.refresh(self.tenant)
        done = {step["key"] for step in state.as_steps() if step["done"]}
        self.assertEqual(done, {"started"})

    def test_connecting_whatsapp_ticks_the_step(self):
        self._connect()
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.whatsapp_connected_at)

    def test_a_disconnected_number_does_not_tick_it(self):
        _account(self.tenant, self.user, status=WhatsAppAccount.Status.DISCONNECTED)
        state = services.refresh(self.tenant)
        self.assertIsNone(state.whatsapp_connected_at)

    def test_adding_a_contact_ticks_the_step(self):
        self._contact()
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.contacts_added_at)

    def test_a_queued_message_is_not_a_sent_message(self):
        """
        A workspace whose every send fails has not activated, and a checklist
        that ticked on "queued" would say it had.
        """
        account = self._connect()
        contact = self._contact()
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=account, contact=contact,
            to_phone=contact.phone, body="Hi", status=Message.Status.QUEUED,
        )
        state = services.refresh(self.tenant)
        self.assertIsNone(state.test_message_sent_at)

    def test_a_sent_message_ticks_the_step(self):
        account = self._connect()
        self._sent_message(account, self._contact())
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.test_message_sent_at)

    def test_legal_acceptance_ticks_the_step(self):
        state = services.refresh(self.tenant)
        self.assertIsNone(state.legal_accepted_at)
        self._accept_legal()
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.legal_accepted_at)

    def test_a_campaign_ticks_created_but_not_sent(self):
        account = self._connect()
        Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Draft", whatsapp_account=account,
            message_body="Hi", status=Campaign.Status.DRAFT,
        )
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.first_campaign_created_at)
        self.assertIsNone(state.first_campaign_sent_at)

    def test_a_running_campaign_ticks_sent(self):
        account = self._connect()
        Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Live", whatsapp_account=account,
            message_body="Hi", status=Campaign.Status.RUNNING,
        )
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.first_campaign_sent_at)

    def test_a_milestone_is_never_un_ticked(self):
        """
        Disconnecting a number in March does not un-connect it in February.
        Rewriting history would be both wrong and demoralising.
        """
        account = self._connect()
        state = services.refresh(self.tenant)
        stamped = state.whatsapp_connected_at
        self.assertIsNotNone(stamped)

        account.status = WhatsAppAccount.Status.DISCONNECTED
        account.save(update_fields=["status"])
        state = services.refresh(self.tenant)
        self.assertEqual(state.whatsapp_connected_at, stamped)


class ConsentMilestoneTests(OnboardingBase):
    """
    The consent step asks the compliance rules, not the `subscribed` column.

    A workspace that requires opt-in must not be told it has confirmed consent
    because it holds a thousand contacts whose consent is UNKNOWN -- that
    walks the customer into a wall of blocked sends.
    """

    def test_a_subscribed_contact_is_enough_when_consent_is_not_required(self):
        self._contact(subscribed=True)
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.consent_confirmed_at)

    def test_an_unsubscribed_contact_is_not(self):
        self._contact(subscribed=False)
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.contacts_added_at)
        self.assertIsNone(state.consent_confirmed_at)

    def test_unknown_consent_is_not_enough_where_opt_in_is_required(self):
        CompliancePolicy.objects.create(tenant=self.tenant, require_marketing_consent=True)
        self._contact(subscribed=True, consent=Contact.MarketingConsent.UNKNOWN)
        state = services.refresh(self.tenant)
        self.assertIsNone(state.consent_confirmed_at)

    def test_granted_consent_is_enough_where_opt_in_is_required(self):
        CompliancePolicy.objects.create(tenant=self.tenant, require_marketing_consent=True)
        self._contact(subscribed=True, consent=Contact.MarketingConsent.GRANTED)
        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.consent_confirmed_at)


class ActivationTests(OnboardingBase):
    """
    Activation is the product being used, not a form being submitted.

    A connected number, a contact we may message, and a message that actually
    reached the provider. Nothing less, and set exactly once.
    """

    def test_creating_an_account_is_not_activation(self):
        state = services.refresh(self.tenant)
        self.assertIsNone(state.activated_at)

    def test_connecting_whatsapp_alone_is_not_activation(self):
        self._connect()
        self.assertIsNone(services.refresh(self.tenant).activated_at)

    def test_contacts_without_a_sent_message_is_not_activation(self):
        self._connect()
        self._contact()
        self.assertIsNone(services.refresh(self.tenant).activated_at)

    def test_the_full_journey_activates(self):
        account = self._connect()
        contact = self._contact()
        self._sent_message(account, contact)

        state = services.refresh(self.tenant)
        self.assertIsNotNone(state.activated_at)
        self.assertTrue(state.is_activated)

    def test_activation_is_recorded_once(self):
        account = self._connect()
        self._sent_message(account, self._contact())

        first = services.refresh(self.tenant).activated_at
        second = services.refresh(self.tenant).activated_at
        self.assertEqual(first, second)
        self.assertEqual(
            FunnelEvent.objects.filter(
                tenant=self.tenant, name=FunnelEvent.Name.ACTIVATED,
            ).count(),
            1,
        )

    def test_a_message_sent_to_an_ineligible_contact_does_not_activate(self):
        """No eligible contact means no activation, even with a sent message."""
        CompliancePolicy.objects.create(tenant=self.tenant, require_marketing_consent=True)
        account = self._connect()
        contact = self._contact(consent=Contact.MarketingConsent.UNKNOWN)
        self._sent_message(account, contact)

        self.assertIsNone(services.refresh(self.tenant).activated_at)


class ResumabilityTests(OnboardingBase):
    """Leaving halfway and coming back must lose nothing."""

    def test_progress_survives_a_new_session(self):
        self._connect()
        self.client.get(reverse("onboarding-state"))

        fresh_client = APIClient()
        fresh_client.force_authenticate(self.user)
        response = fresh_client.get(reverse("onboarding-state"))

        steps = {step["key"]: step["done"] for step in response.data["steps"]}
        self.assertTrue(steps["whatsapp_connected"])

    def test_the_next_step_is_the_first_unfinished_one(self):
        self._accept_legal()
        response = self.client.get(reverse("onboarding-state"))
        self.assertEqual(response.data["next_step"]["key"], "whatsapp_connected")

    def test_a_workspace_that_did_everything_before_onboarding_existed_is_complete(self):
        """
        The self-healing property: a customer who has been sending since March
        is not told to go and connect the number they already use.
        """
        account = self._connect()
        contact = self._contact()
        self._sent_message(account, contact)
        self._accept_legal()
        Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Old", whatsapp_account=account,
            message_body="Hi", status=Campaign.Status.COMPLETED,
        )

        response = self.client.get(reverse("onboarding-state"))
        self.assertTrue(response.data["is_complete"])
        self.assertIsNone(response.data["next_step"])


class ExistingCustomerTests(OnboardingBase):
    def test_a_workspace_created_before_onboarding_is_not_guided(self):
        """Section 34: existing users must not be forced through a wizard."""
        state = services.refresh(self.tenant)
        self.assertFalse(state.is_guided)

    def test_a_signup_created_workspace_is_guided(self):
        state = services.state_for(self.tenant, guided=True)
        self.assertTrue(state.is_guided)

    def test_the_guided_flag_is_not_flipped_on_by_a_later_read(self):
        services.refresh(self.tenant)  # creates it un-guided
        state = services.state_for(self.tenant, guided=True)
        self.assertFalse(state.is_guided)


class ChecklistApiTests(OnboardingBase):
    def test_the_checklist_can_be_dismissed_and_restored(self):
        response = self.client.post(reverse("onboarding-state"), {"action": "dismiss"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["dismissed_at"])

        response = self.client.post(reverse("onboarding-state"), {"action": "restore"}, format="json")
        self.assertIsNone(response.data["dismissed_at"])

    def test_there_is_no_way_to_mark_a_step_complete(self):
        """
        The security property of the whole module. A client that could tick a
        step could manufacture an activation.
        """
        for attempt in (
            {"action": "complete", "step": "whatsapp_connected"},
            {"action": "dismiss", "whatsapp_connected_at": timezone.now().isoformat()},
            {"activated_at": timezone.now().isoformat()},
            {"steps": [{"key": "test_message_sent", "done": True}]},
        ):
            self.client.post(reverse("onboarding-state"), attempt, format="json")

        state = OnboardingState.objects.get(tenant=self.tenant)
        self.assertIsNone(state.whatsapp_connected_at)
        self.assertIsNone(state.activated_at)
        self.assertIsNone(state.test_message_sent_at)

    def test_an_unknown_action_is_refused(self):
        response = self.client.post(reverse("onboarding-state"), {"action": "nonsense"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_endpoint_needs_authentication(self):
        response = APIClient().get(reverse("onboarding-state"))
        self.assertIn(response.status_code, (401, 403))

    def test_the_trial_block_reports_the_backends_numbers(self):
        plan = Plan.objects.create(owner=None, name="Trial", price_cents=0)
        Subscription.objects.create(
            tenant=self.tenant, plan=plan, status=Subscription.Status.TRIALING,
            trial_started_at=timezone.now(),
            trial_ends_at=timezone.now() + timedelta(days=5),
        )
        response = self.client.get(reverse("onboarding-state"))
        self.assertTrue(response.data["trial"]["active"])
        self.assertEqual(response.data["trial"]["days_remaining"], 5)


class FunnelTests(OnboardingBase):
    def test_a_milestone_records_its_funnel_event(self):
        self._connect()
        services.refresh(self.tenant)
        self.assertTrue(
            FunnelEvent.objects.filter(
                tenant=self.tenant, name=FunnelEvent.Name.WHATSAPP_CONNECTED,
            ).exists()
        )

    def test_a_milestone_event_is_recorded_once(self):
        self._connect()
        services.refresh(self.tenant)
        services.refresh(self.tenant)
        self.assertEqual(
            FunnelEvent.objects.filter(
                tenant=self.tenant, name=FunnelEvent.Name.WHATSAPP_CONNECTED,
            ).count(),
            1,
        )

    def test_a_repeatable_event_may_repeat(self):
        services.record(FunnelEvent.Name.UPGRADE_STARTED, tenant=self.tenant)
        services.record(FunnelEvent.Name.UPGRADE_STARTED, tenant=self.tenant)
        self.assertEqual(
            FunnelEvent.objects.filter(name=FunnelEvent.Name.UPGRADE_STARTED).count(), 2,
        )

    def test_signup_started_needs_no_tenant(self):
        self.assertIsNotNone(services.record(FunnelEvent.Name.SIGNUP_STARTED))

    def test_recording_never_raises(self):
        self.assertIsNone(services.record("not-a-real-event-name-that-is-far-too-long-to-fit"))

    def test_funnel_counts_count_workspaces_not_events(self):
        services.record(FunnelEvent.Name.UPGRADE_STARTED, tenant=self.tenant)
        services.record(FunnelEvent.Name.UPGRADE_STARTED, tenant=self.tenant)
        counts = services.funnel_counts()
        self.assertEqual(counts[FunnelEvent.Name.UPGRADE_STARTED]["workspaces"], 1)
        self.assertEqual(counts[FunnelEvent.Name.UPGRADE_STARTED]["events"], 2)

    def test_funnel_metadata_carries_no_personal_data(self):
        """Counts and plan names only -- never a phone number or message text."""
        account = self._connect()
        contact = self._contact(phone="+15559998888")
        self._sent_message(account, contact)
        services.refresh(self.tenant)

        blob = str(list(FunnelEvent.objects.values_list("metadata", flat=True)))
        self.assertNotIn("+15559998888", blob)
        self.assertNotIn("Hello", blob)


class FunnelEndpointTests(OnboardingBase):
    def test_a_customer_cannot_read_the_funnel(self):
        """It counts workspaces across the deployment. Not a customer's business."""
        response = self.client.get(reverse("onboarding-funnel"))
        self.assertEqual(response.status_code, 404)

    def test_a_superadmin_can(self):
        admin = get_user_model().objects.create_user(
            username="funnel-admin", password="x", tenant=self.tenant,
            role=get_user_model().Role.SUPERADMIN,
        )
        client = APIClient()
        client.force_authenticate(admin)
        response = client.get(reverse("onboarding-funnel"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("stages", response.data)

    def test_the_funnel_states_that_these_are_not_conversion_rates(self):
        admin = get_user_model().objects.create_user(
            username="funnel-admin-2", password="x", tenant=self.tenant,
            role=get_user_model().Role.SUPERADMIN,
        )
        client = APIClient()
        client.force_authenticate(admin)
        response = client.get(reverse("onboarding-funnel"))
        self.assertIn("caveat", response.data)
        self.assertIn("not conversion rates", response.data["caveat"])
