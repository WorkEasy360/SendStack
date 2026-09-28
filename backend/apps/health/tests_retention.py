"""
Retention deletes what it is supposed to, and -- more importantly -- nothing else.

The second half is what most of this file is. A pruning job is a piece of code
whose bug destroys customer data silently and is discovered months later, so the
tests that assert what *survives* a sweep are worth more than the ones that
assert what goes.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.health import retention
from apps.tenants.models import Tenant


def _age(model, pk, field, days):
    """Backdate a row past a horizon. update() so auto_now fields stay put."""
    model.objects.filter(pk=pk).update(**{field: timezone.now() - timedelta(days=days)})


class WhatsAppWebhookEventRetentionTests(TestCase):
    def setUp(self):
        from apps.whatsapp.models import WhatsAppAccount

        self.tenant = Tenant.objects.create(
            name="Retention Co", slug="retention-co", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="retention-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )

    def _event(self, event_id, age_days):
        from apps.whatsapp.models import WhatsAppWebhookEvent

        event = WhatsAppWebhookEvent.objects.create(
            account=self.account,
            event_type=WhatsAppWebhookEvent.EventType.MESSAGE_STATUS,
            provider_event_id=event_id,
            payload={"raw": "x"},
        )
        _age(WhatsAppWebhookEvent, event.pk, "received_at", age_days)
        return event

    def test_events_past_the_horizon_are_removed(self):
        from apps.whatsapp.models import WhatsAppWebhookEvent

        old = self._event("old", 90)
        recent = self._event("recent", 1)

        retention.prune_whatsapp_webhook_events()

        self.assertFalse(WhatsAppWebhookEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(WhatsAppWebhookEvent.objects.filter(pk=recent.pk).exists())

    @override_settings(RETENTION_WEBHOOK_EVENT_DAYS=0)
    def test_zero_days_disables_the_rule_entirely(self):
        """
        0 means "keep for ever", the same convention the plan ceilings use.

        The alternative reading -- 0 as "delete everything older than now" --
        would make a mistyped setting destroy the table, so it is worth a test
        of its own.
        """
        from apps.whatsapp.models import WhatsAppWebhookEvent

        old = self._event("old", 3650)

        removed = retention.prune_whatsapp_webhook_events()

        self.assertEqual(removed, 0)
        self.assertTrue(WhatsAppWebhookEvent.objects.filter(pk=old.pk).exists())

    def test_pruning_an_event_does_not_touch_the_message_it_was_about(self):
        """
        The webhook row is the delivery receipt; the message is the record.

        Deleting the receipt must leave the message, its status and its
        timestamps exactly as the receipt set them.
        """
        from apps.contacts.models import Contact
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppWebhookEvent

        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+15551110001",
        )
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=contact,
            to_phone=contact.phone, body="hi", billed_to=self.user,
            status=Message.Status.DELIVERED,
        )
        self._event("receipt", 90)

        retention.prune_whatsapp_webhook_events()

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)
        self.assertEqual(WhatsAppWebhookEvent.objects.count(), 0)


class BillingPayloadPurgeTests(TestCase):
    """The payment webhook payload ages out; the event row never does."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Billing Co", slug="billing-retention", kind=Tenant.Kind.CUSTOMER,
        )

    def _event(self, event_id, age_days):
        from apps.billing.models import WebhookEvent

        event = WebhookEvent.objects.create(
            tenant=self.tenant, provider="razorpay", event_id=event_id,
            event_type="payment.captured", payload={"card": "sensitive"},
        )
        _age(WebhookEvent, event.pk, "received_at", age_days)
        return event

    def test_old_payloads_are_emptied_but_the_row_survives(self):
        from apps.billing.models import WebhookEvent

        old = self._event("evt_old", 200)

        retention.purge_billing_webhook_payloads()

        old.refresh_from_db()
        self.assertEqual(old.payload, {})
        self.assertIsNotNone(old.payload_purged_at)
        # The financial record itself is untouched -- this is a purge, not a
        # delete, and that distinction is the whole design.
        self.assertTrue(WebhookEvent.objects.filter(pk=old.pk).exists())
        self.assertEqual(old.event_type, "payment.captured")

    def test_recent_payloads_are_left_alone(self):
        recent = self._event("evt_recent", 1)

        retention.purge_billing_webhook_payloads()

        recent.refresh_from_db()
        self.assertEqual(recent.payload, {"card": "sensitive"})
        self.assertIsNone(recent.payload_purged_at)

    def test_purging_is_not_repeated_on_the_next_pass(self):
        """
        `payload_purged_at` is what makes the sweep idempotent.

        Without it every daily pass would rewrite every historical row for
        ever, which on a large table is a pointless write amplification.
        """
        self._event("evt_old", 200)

        first = retention.purge_billing_webhook_payloads()
        second = retention.purge_billing_webhook_payloads()

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)


class ScheduledJobRetentionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Sched Co", slug="sched-retention", kind=Tenant.Kind.CUSTOMER,
        )

    def _job(self, status, age_days):
        from apps.scheduling.models import ScheduledJob

        job = ScheduledJob.objects.create(
            tenant=self.tenant, kind=ScheduledJob.Kind.CAMPAIGN_DISPATCH,
            object_id=1, scheduled_for=timezone.now(), status=status,
        )
        _age(ScheduledJob, job.pk, "updated_at", age_days)
        return job

    def test_completed_and_cancelled_jobs_age_out(self):
        from apps.scheduling.models import ScheduledJob

        completed = self._job(ScheduledJob.Status.COMPLETED, 90)
        cancelled = self._job(ScheduledJob.Status.CANCELLED, 90)

        retention.prune_finished_scheduled_jobs()

        self.assertFalse(ScheduledJob.objects.filter(pk__in=[completed.pk, cancelled.pk]).exists())

    def test_failed_jobs_are_kept(self):
        """
        A failed scheduled job is the thing an operator goes looking for.

        "My Friday campaign never went out" is asked on Monday, or three weeks
        later, and pruning the evidence on the same schedule as the successes
        would throw away the only record of why.
        """
        from apps.scheduling.models import ScheduledJob

        failed = self._job(ScheduledJob.Status.FAILED, 365)

        retention.prune_finished_scheduled_jobs()

        self.assertTrue(ScheduledJob.objects.filter(pk=failed.pk).exists())

    def test_pending_jobs_are_never_touched(self):
        """A pending job is future work, whatever its updated_at says."""
        from apps.scheduling.models import ScheduledJob

        pending = self._job(ScheduledJob.Status.PENDING, 365)

        retention.prune_finished_scheduled_jobs()

        self.assertTrue(ScheduledJob.objects.filter(pk=pending.pk).exists())


class NothingImportantIsEverDeletedTests(TestCase):
    """
    A full sweep against a workspace holding one row of everything.

    This is the regression test for the failure that would matter: a rule
    added later, or a cascade nobody traced, quietly taking customer data,
    consent evidence, audit history or financial records with it. Everything
    below is listed in docs/DATA_RETENTION.md as never pruned by the
    application.
    """

    def setUp(self):
        from apps.audit.models import AuditEvent
        from apps.campaigns.models import Campaign
        from apps.compliance.models import ConsentEvent
        from apps.contacts.models import Contact
        from apps.credits.models import CreditTransaction
        from apps.messaging.models import Message
        from apps.whatsapp.models import WhatsAppAccount

        self.tenant = Tenant.objects.create(
            name="Keep Co", slug="keep-co", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="keep-owner", password="x", tenant=self.tenant, credits=100,
        )
        account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Line",
        )
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+15552220001",
        )
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Old campaign",
            whatsapp_account=account, message_body="hi",
        )
        message = Message.objects.create(
            tenant=self.tenant, campaign=campaign, whatsapp_account=account,
            contact=contact, to_phone=contact.phone, body="hi", billed_to=self.user,
        )
        consent = ConsentEvent.objects.create(
            tenant=self.tenant, contact=contact, phone=contact.phone,
            event=ConsentEvent.Event.OPT_OUT, source=ConsentEvent.Source.SYSTEM,
        )
        audit = AuditEvent.objects.create(
            tenant=self.tenant, action="test.event", actor=self.user,
        )
        credit = CreditTransaction.objects.create(
            account=self.user, amount=1, type=CreditTransaction.Type.CONSUME,
            reference="message:1", balance_before=100, balance_after=99,
        )

        # Backdate everything far past every horizon in the module, so that a
        # rule which wrongly covered one of these would certainly fire.
        for model, pk, field in (
            (Contact, contact.pk, "created_at"),
            (Campaign, campaign.pk, "created_at"),
            (Message, message.pk, "queued_at"),
            (ConsentEvent, consent.pk, "created_at"),
            (AuditEvent, audit.pk, "created_at"),
            (CreditTransaction, credit.pk, "created_at"),
        ):
            try:
                _age(model, pk, field, 3650)
            except Exception:  # noqa: BLE001 - a model without that field is fine
                pass

        self.expected = {
            Contact: contact.pk,
            Campaign: campaign.pk,
            Message: message.pk,
            ConsentEvent: consent.pk,
            AuditEvent: audit.pk,
            CreditTransaction: credit.pk,
        }

    def test_a_full_sweep_leaves_every_protected_record_in_place(self):
        retention.run_all()

        for model, pk in self.expected.items():
            with self.subTest(model=model.__name__):
                self.assertTrue(
                    model.objects.filter(pk=pk).exists(),
                    f"retention deleted a {model.__name__}, which it must never do",
                )

    def test_a_sweep_reports_a_result_for_every_rule(self):
        results = retention.run_all()

        self.assertEqual(set(results), {name for name, _ in retention.RULES})
        for name, value in results.items():
            with self.subTest(rule=name):
                self.assertIsNotNone(value, f"rule {name} raised instead of running")

    def test_one_failing_rule_does_not_stop_the_others(self):
        """
        Housekeeping rules are independent, and one being broken must not mean
        the webhook table stops being cleaned for a month.
        """
        from unittest.mock import patch

        def explode():
            raise RuntimeError("boom")

        with patch.object(retention, "RULES", (("broken", explode), retention.RULES[0])):
            results = retention.run_all()

        self.assertIsNone(results["broken"])
        self.assertIsNotNone(results[retention.RULES[0][0]])
