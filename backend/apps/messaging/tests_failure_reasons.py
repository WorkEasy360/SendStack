"""
Every way a message can fail records a code, and the right one.

The value of `failure_reason` is entirely in it being *complete and correct*.
A code that is missing on one path makes "how many failed because the device
was disconnected" quietly wrong, and a wrong code is worse than a blank one --
a blank is visibly unknown, a wrong one gets counted.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.contacts.models import Contact
from apps.messaging import gating
from apps.messaging.models import Message
from apps.messaging.services import mark_skipped
from apps.messaging.tasks import _failure_reason, send_message_task
from apps.tenants.models import Tenant, TenantSettings
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.providers.base import SendResult


class ProviderCodeMappingTests(TestCase):
    def test_transport_problems_map_to_provider_unavailable(self):
        for code in ("network_error", "timeout", "connection_error"):
            with self.subTest(code=code):
                result = SendResult(success=False, error="x", code=code)
                self.assertEqual(_failure_reason(result), Message.FailureReason.PROVIDER_UNAVAILABLE)

    def test_a_dead_session_maps_to_device_disconnected(self):
        for code in ("no_session", "missing_credentials", "authentication_error"):
            with self.subTest(code=code):
                result = SendResult(success=False, error="x", code=code)
                self.assertEqual(_failure_reason(result), Message.FailureReason.DEVICE_DISCONNECTED)

    def test_an_unknown_code_becomes_provider_error_rather_than_being_guessed(self):
        """
        Honest, and still countable.

        The alternative -- picking the closest-looking specific bucket -- makes
        the breakdown confidently wrong, which is the failure mode a code
        column exists to remove.
        """
        result = SendResult(success=False, error="x", code="something-new")
        self.assertEqual(_failure_reason(result), Message.FailureReason.PROVIDER_ERROR)

    def test_a_missing_code_becomes_provider_error(self):
        result = SendResult(success=False, error="x")
        self.assertEqual(_failure_reason(result), Message.FailureReason.PROVIDER_ERROR)

    def test_ambiguity_wins_over_the_provider_code(self):
        """
        An ambiguous send is its own category and outranks whatever the
        provider said, because the operational consequence is different: it is
        the one failure that must never be retried.
        """
        result = SendResult(success=False, error="x", code="network_error", ambiguous=True)
        self.assertEqual(_failure_reason(result), Message.FailureReason.DELIVERY_UNCONFIRMED)


class GateDecisionCodeTests(TestCase):
    """Each refusal from the send gate carries the code that explains it."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Gate Co", slug="gate-codes", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="gate-codes-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+15554440001",
        )

    def _message(self, **kwargs):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user, **kwargs
        )

    def test_an_unsubscribed_contact_is_skipped_as_opted_out(self):
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])
        message = self._message()

        decision = gating.evaluate(message, self.account)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertEqual(decision.code, Message.FailureReason.OPTED_OUT)

    def test_a_suspended_workspace_is_blocked_as_tenant_suspended(self):
        self.tenant.is_active = False
        self.tenant.save(update_fields=["is_active"])
        message = self._message()

        decision = gating.evaluate(message, self.account)

        self.assertEqual(decision.action, gating.BLOCK)
        self.assertEqual(decision.code, Message.FailureReason.TENANT_SUSPENDED)

    def test_a_hold_carries_no_code(self):
        """
        A held message has not failed, so recording a reason for it would put
        a failure in the counts that never happened.
        """
        decision = gating.GateDecision(action=gating.HOLD, reason="quiet hours", retry_after=60)

        self.assertEqual(decision.code, "")

    def test_an_allow_carries_no_code(self):
        self.assertEqual(gating.GateDecision().code, "")


class MarkSkippedTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Skip Co", slug="skip-codes", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="skip-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+15554440002",
        )

    def test_the_code_is_stored_alongside_the_sentence(self):
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

        self.assertTrue(mark_skipped(
            message, "This contact has unsubscribed.", code=Message.FailureReason.OPTED_OUT,
        ))

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SKIPPED)
        self.assertEqual(message.failure_reason, Message.FailureReason.OPTED_OUT)
        # The sentence is still what a person reads.
        self.assertIn("unsubscribed", message.error_message)

    def test_a_caller_with_no_better_answer_leaves_it_blank(self):
        """
        Optional on purpose: a wrong code is worse than none, because a wrong
        one gets counted and a blank one is visibly unknown.
        """
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

        mark_skipped(message, "Something went wrong.")

        message.refresh_from_db()
        self.assertEqual(message.failure_reason, "")

    def test_a_message_already_sending_is_not_skipped_from_under_the_worker(self):
        """The pre-existing guard, re-asserted because this change touched it."""
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
            status=Message.Status.SENDING,
        )

        self.assertFalse(mark_skipped(message, "x", code=Message.FailureReason.OPTED_OUT))

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENDING)


class EndToEndFailureCodeTests(TestCase):
    """The code that reaches the row when the whole task runs."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="E2E Co", slug="e2e-codes", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="e2e-owner", password="x", tenant=self.tenant, credits=100,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+15554440003",
        )

    def test_a_permanently_refused_send_records_the_provider_code(self):
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

        with patch(
            "apps.messaging.services.ConnectionManager.send_text_message",
            return_value=SendResult(
                success=False, error="Number is not on WhatsApp.",
                code="invalid_recipient", ambiguous=True,
            ),
        ):
            send_message_task(message.pk)

        message.refresh_from_db()
        # An ambiguous send is never terminal-FAILED and never retried: it
        # becomes RECOVERY_REQUIRED, and the reason says the outcome is
        # unconfirmed (which outranks the provider's own code).
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(message.failure_reason, Message.FailureReason.DELIVERY_UNCONFIRMED)

    def test_no_connected_device_records_device_disconnected(self):
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
        )

        # No retry budget, so the task gives up rather than retrying. The
        # budget is the workspace's own setting now (apps.tenants.
        # TenantSettings), so it is turned off there rather than by patching
        # the task's class attribute, which the task no longer reads.
        TenantSettings.objects.update_or_create(tenant=self.tenant, defaults={"retry_enabled": False})

        with patch("apps.messaging.tasks.ensure_sendable_account", return_value=None):
            send_message_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SKIPPED)
        self.assertEqual(message.failure_reason, Message.FailureReason.DEVICE_DISCONNECTED)

    def test_existing_rows_keep_a_blank_reason(self):
        """
        The column is blank by default and is never backfilled with a guess.

        A reason invented for a historical row would be indistinguishable from
        a measured one, which is exactly what makes a metric untrustworthy.
        """
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, body="hi", billed_to=self.user,
            status=Message.Status.FAILED, error_message="old style failure",
        )

        self.assertEqual(message.failure_reason, "")
