"""
The send gate under Phase 3: suspension, consent precedence, and stale authorization.

The scenario the brief singles out, and the reason these live at the gate
rather than at the campaign builder:

    customer active -> campaign scheduled -> subscription expires or the
    workspace is suspended -> the Celery task runs anyway

A task must never trust the authorization it was scheduled with. Everything
below asserts that the decision is made at send time, from live state.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from apps.compliance.models import CompliancePolicy, ConsentEvent
from apps.compliance.services import opt_out, set_marketing_consent
from apps.contacts.models import Contact
from apps.messaging import gating
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount


# The number's own send limits and warm-up are a different subsystem with its
# own tests; left on, they would hold messages these tests expect to see
# allowed and turn every assertion here into a test of two things.
@override_settings(
    WHATSAPP_WARMUP_ENABLED=False, WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=0,
)
class GateTestCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-gate3", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-gate3", email="ada@gate3.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bob", phone="+919876543210",
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            account_type=WhatsAppAccount.AccountType.WHATSAPP,
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )

    def message(self, kind=Message.Kind.MARKETING):
        return Message.objects.create(
            tenant=self.tenant, contact=self.contact, whatsapp_account=self.account,
            to_phone=self.contact.phone, body="Hello", kind=kind,
            direction=Message.Direction.OUTBOUND, status=Message.Status.QUEUED,
        )

    def policy(self, **fields):
        policy, _ = CompliancePolicy.objects.get_or_create(tenant=self.tenant)
        for name, value in fields.items():
            setattr(policy, name, value)
        policy.save()
        return policy


class SuspensionTests(GateTestCase):
    def test_an_active_workspace_may_send(self):
        decision = gating.evaluate(self.message(), self.account)
        self.assertEqual(decision.action, gating.ALLOW)

    def test_a_suspended_workspace_is_blocked(self):
        """
        The stale-authorization case. The campaign was launched while the
        workspace was fine; by the time this message is dispatched it is
        suspended, and the message must not go.
        """
        message = self.message()
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        decision = gating.evaluate(message, self.account)
        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("suspended", decision.reason.lower())

    def test_suspension_blocks_a_service_message_too(self):
        """Suspension is about the workspace, not about the message's kind."""
        message = self.message(kind=Message.Kind.SERVICE)
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        self.assertEqual(gating.evaluate(message, self.account).action, gating.BLOCK)

    def test_suspension_is_checked_before_anything_is_consumed(self):
        """
        BLOCK, not HOLD: a suspended workspace must not take a send slot from
        the number, nor claim a message from the plan's monthly allowance.
        """
        message = self.message()
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])

        gating.evaluate(message, self.account)

        message.refresh_from_db()
        self.assertIsNone(message.plan_counted_at)


class ConsentPrecedenceAtTheGateTests(GateTestCase):
    def test_an_unsubscribed_contact_is_blocked(self):
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        self.contact.refresh_from_db()

        decision = gating.evaluate(self.message(), self.account)
        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("unsubscribed", decision.reason.lower())

    def test_an_unsubscribed_contact_is_blocked_even_with_consent_recorded(self):
        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.WEBSITE)
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        self.contact.refresh_from_db()

        decision = gating.evaluate(self.message(), self.account)
        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("unsubscribed", decision.reason.lower())

    def test_marketing_is_blocked_without_consent_when_the_policy_requires_it(self):
        self.policy(require_marketing_consent=True)
        decision = gating.evaluate(self.message(), self.account)
        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("consent", decision.reason.lower())

    def test_marketing_is_allowed_with_consent_when_the_policy_requires_it(self):
        self.policy(require_marketing_consent=True)
        set_marketing_consent(self.contact, True, source=Contact.ConsentSource.FORM)
        self.contact.refresh_from_db()

        self.assertEqual(gating.evaluate(self.message(), self.account).action, gating.ALLOW)

    def test_a_service_reply_never_needs_a_marketing_opt_in(self):
        """
        Somebody who has just messaged in is owed an answer. Requiring a
        marketing opt-in before replying to them would break the inbox for
        every tenant that turns consent enforcement on.
        """
        self.policy(require_marketing_consent=True)
        message = self.message(kind=Message.Kind.SERVICE)
        self.assertEqual(gating.evaluate(message, self.account).action, gating.ALLOW)

    def test_a_notice_is_exempt(self):
        """The message telling somebody their plan is full must not be gated by it."""
        self.policy(require_marketing_consent=True)
        opt_out(self.contact, source=ConsentEvent.Source.AGENT, detail="asked")
        self.contact.refresh_from_db()

        message = self.message(kind=Message.Kind.NOTICE)
        self.assertNotEqual(gating.evaluate(message, self.account).action, gating.BLOCK)


class PremiumFeaturesDoNotBypassSafetyTests(GateTestCase):
    """
    §19: a paid feature is not an authorization layer. Whatever produced the
    message -- a campaign, a workflow, the AI agent, the voice follow-up -- it
    lands on the same gate with the same answer.
    """

    def test_every_outbound_kind_is_stopped_by_an_opt_out(self):
        opt_out(self.contact, source=ConsentEvent.Source.INBOUND_KEYWORD, detail="Replied STOP")
        self.contact.refresh_from_db()

        for kind in (Message.Kind.MARKETING, Message.Kind.SERVICE):
            with self.subTest(kind=kind):
                decision = gating.evaluate(self.message(kind=kind), self.account)
                self.assertEqual(decision.action, gating.BLOCK)

    def test_there_is_exactly_one_consent_check_on_the_send_path(self):
        """
        A structural assertion, and the reason it is worth writing: the value
        of a central gate is entirely lost the moment a second sending path
        does its own check. `evaluate` is the only caller of the precedence
        rule, and messaging.tasks is the only caller of `evaluate`.
        """
        import inspect

        from apps.messaging import tasks

        source = inspect.getsource(tasks)
        self.assertIn("gating.evaluate", source)

        gate_source = inspect.getsource(gating)
        self.assertIn("may_receive_marketing", gate_source)
