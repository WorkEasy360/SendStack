from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.compliance.models import CompliancePolicy
from apps.contacts.models import Contact
from apps.messaging import gating
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp import health
from apps.whatsapp.models import WhatsAppAccount


def _tenant(slug):
    return Tenant.objects.create(name=slug, slug=slug, kind=Tenant.Kind.CUSTOMER)


@override_settings(
    WHATSAPP_WARMUP_ENABLED=False, WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=0,
)
class SendGateTests(TestCase):
    """
    The gate is the last thing between a queued message and a recipient, so
    what it does with each rule matters more than that it has the rule at all:
    a rule that blocks where it should have held loses the recipient for good.
    """

    def setUp(self):
        self.tenant = _tenant("acme-gate")
        self.user = get_user_model().objects.create_user(
            username="acme-gate-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount(
            tenant=self.tenant, user=self.user, name="Sales Line",
            account_type=WhatsAppAccount.AccountType.WHATSAPP,
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.account.save()
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+15559990000", created_by=self.user,
        )

    def _message(self, **overrides):
        defaults = {
            "tenant": self.tenant, "whatsapp_account": self.account, "contact": self.contact,
            "to_phone": self.contact.phone, "direction": Message.Direction.OUTBOUND,
            "body": "Hello", "status": Message.Status.QUEUED, "kind": Message.Kind.MARKETING,
        }
        defaults.update(overrides)
        return Message.objects.create(**defaults)

    def _quiet_policy(self):
        return CompliancePolicy.objects.create(
            tenant=self.tenant, quiet_hours_enabled=True,
            quiet_hours_start=time(0, 0), quiet_hours_end=time(23, 59), timezone="UTC",
        )

    # -- Consent ------------------------------------------------------------

    def test_an_unsubscribed_contact_blocks_a_campaign_message(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        decision = gating.evaluate(self._message(), self.account)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("unsubscribed", decision.reason)

    def test_an_unsubscribed_contact_also_blocks_an_agent_reply(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        decision = gating.evaluate(self._message(kind=Message.Kind.SERVICE), self.account)

        self.assertEqual(decision.action, gating.BLOCK)

    def test_the_opt_out_confirmation_itself_still_goes_out(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        decision = gating.evaluate(self._message(kind=Message.Kind.NOTICE), self.account)

        self.assertTrue(decision.allowed)

    def test_a_subscribed_contact_is_not_blocked(self):
        self.assertTrue(gating.evaluate(self._message(), self.account).allowed)

    # -- Quiet hours --------------------------------------------------------

    def test_marketing_is_held_during_quiet_hours(self):
        self._quiet_policy()

        decision = gating.evaluate(self._message(), self.account)

        self.assertEqual(decision.action, gating.HOLD)
        self.assertGreater(decision.retry_after, 0)

    def test_a_service_reply_is_never_held_for_quiet_hours(self):
        # Someone waiting on an answer is worse served by getting it at 9am
        # than by getting it at 11pm.
        self._quiet_policy()

        self.assertTrue(gating.evaluate(self._message(kind=Message.Kind.SERVICE), self.account).allowed)

    def test_a_message_held_past_the_limit_is_given_up_on(self):
        self._quiet_policy()
        message = self._message()
        Message.objects.filter(pk=message.pk).update(queued_at=timezone.now() - timedelta(days=7))
        message.refresh_from_db()

        decision = gating.evaluate(message, self.account)

        self.assertEqual(decision.action, gating.BLOCK)

    # -- The number itself --------------------------------------------------

    def test_a_timed_pause_holds_rather_than_loses_the_recipient(self):
        health.pause_sending(self.account, "Too many failures", minutes=30)

        decision = gating.evaluate(self._message(), self.account)

        self.assertEqual(decision.action, gating.HOLD)
        self.assertGreater(decision.retry_after, 0)

    def test_an_indefinite_pause_blocks(self):
        health.pause_sending(self.account, "Suspected ban", minutes=None)

        decision = gating.evaluate(self._message(), self.account)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertIn("ban", decision.reason)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=1)
    def test_the_daily_limit_holds_the_rest_of_the_campaign(self):
        self.assertTrue(gating.evaluate(self._message(), self.account).allowed)

        decision = gating.evaluate(self._message(), self.account)

        self.assertEqual(decision.action, gating.HOLD)
        self.assertGreater(decision.retry_after, 0)

    @override_settings(WHATSAPP_DAILY_SEND_LIMIT=1)
    def test_a_slot_is_not_spent_by_a_message_that_was_blocked(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        gating.evaluate(self._message(), self.account)

        from apps.whatsapp.limits import usage

        self.assertEqual(usage(self.account)["daily_sent"], 0)


@override_settings(
    WHATSAPP_WARMUP_ENABLED=False, WHATSAPP_DAILY_SEND_LIMIT=0, WHATSAPP_HOURLY_SEND_LIMIT=0,
)
class SendTaskGateTests(TestCase):
    """The task's half of the contract: a HOLD re-queues, a BLOCK skips."""

    def setUp(self):
        self.tenant = _tenant("acme-gate-task")
        self.user = get_user_model().objects.create_user(
            username="acme-gate-task-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount(
            tenant=self.tenant, user=self.user, name="Sales Line",
            account_type=WhatsAppAccount.AccountType.WHATSAPP,
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.account.save()
        self.contact = Contact.objects.create(
            tenant=self.tenant, phone="+15559990001", created_by=self.user, subscribed=False,
        )

    def _message(self):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND,
            body="Hello", status=Message.Status.QUEUED, kind=Message.Kind.MARKETING,
        )

    @patch("apps.messaging.tasks.attempt_send")
    def test_a_blocked_message_is_skipped_and_never_sent(self, attempt):
        from apps.messaging.tasks import send_message_task

        message = self._message()
        send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SKIPPED)
        self.assertIn("unsubscribed", message.error_message)
        self.assertFalse(attempt.called)

    @patch("apps.messaging.tasks.send_message_task.apply_async")
    @patch("apps.messaging.tasks.attempt_send")
    def test_a_held_message_stays_queued_and_comes_back(self, attempt, apply_async):
        from apps.messaging.tasks import send_message_task

        self.contact.subscribed = True
        self.contact.save(update_fields=["subscribed"])
        health.pause_sending(self.account, "Too many failures", minutes=30)

        message = self._message()
        send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.QUEUED)
        self.assertFalse(attempt.called)
        self.assertTrue(apply_async.called)
        self.assertEqual(apply_async.call_args.kwargs["args"], [message.pk])
        self.assertGreater(apply_async.call_args.kwargs["countdown"], 0)
