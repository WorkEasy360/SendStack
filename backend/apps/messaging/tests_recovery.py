"""
The crash matrix: every boundary at which a worker can die during a send, and
proof that the message is recovered without a duplicate and without getting
stuck in SENDING.

Crashes are injected deterministically with core.failpoints (a
``SimulatedWorkerLost`` -- a BaseException, so it propagates exactly like a
SIGKILL and cleanup code cannot swallow it). Recovery is driven by the same
sweep that runs in production, ``apps.messaging.recovery.sweep_stuck_sends``.
"""

import random
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.credits.models import CreditTransaction
from apps.credits.services import reserve_credits
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult
from apps.whatsapp.signals import webhook_event_stored
from core.failpoints import SimulatedWorkerLost, failpoint

from .models import Message, MessageSendAttempt
from .recovery import (
    RecoveryActionError,
    classify_and_recover,
    resolve_message,
    sweep_stuck_sends,
)
from .services import attempt_send, refresh_campaign_counters
from .tasks import send_message_task

_OK = SendResult(success=True, provider_message_id="wamid.ok")


def _ok_send(*_args, **_kwargs):
    """A successful send with a unique provider id (Message.provider_message_id is UNIQUE)."""
    return SendResult(success=True, provider_message_id=f"wamid.{uuid4().hex}")


def _run_task(message_id, *, retries=0):
    """Invoke send_message_task like a worker would; return None or raise SimulatedWorkerLost."""
    send_message_task.push_request(retries=retries)
    try:
        return send_message_task.run(message_id)
    finally:
        send_message_task.pop_request()


class RecoveryBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Rec Co", slug="rec-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="rec-owner", password="x", tenant=self.tenant,
        )
        self.user.credits = 1000
        self.user.save(update_fields=["credits"])
        self.account = WhatsAppAccount(
            tenant=self.tenant, user=self.user, name="Line", phone_number_id="1",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        self.account.set_access_token("token")
        self.account.set_app_secret("secret")
        self.account.save()

    def _message(self, *, reserve=True, campaign=None, body="Hello"):
        msg = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, campaign=campaign,
            to_phone="+15551230000", body=body, billed_to=self.user,
        )
        if reserve:
            reserve_credits(user_id=self.user.pk, amount=1, reference=f"message:{msg.pk}")
        return msg

    def _expire_lease(self, message):
        Message.objects.filter(pk=message.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )

    def _reserved(self):
        self.user.refresh_from_db()
        return self.user.reserved_credits

    def _consumed(self):
        return CreditTransaction.objects.filter(
            account=self.user, type=CreditTransaction.Type.CONSUME,
        ).count()


class CrashBoundaryTests(RecoveryBase):
    """Tests 1-5, 9, 11 of the mandated matrix."""

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_1_crash_immediately_after_claim(self, mock_send):
        message = self._message()
        with failpoint("AFTER_CLAIM"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENDING)
        attempt = message.send_attempts.get()
        self.assertEqual(attempt.status, MessageSendAttempt.Status.CLAIMED)
        self.assertIsNone(attempt.submission_started_at)
        self.assertEqual(mock_send.call_count, 0)  # nothing was sent

        # Recovery: never submitted -> safe to re-queue, which (eager) re-sends.
        self._expire_lease(message)
        result = sweep_stuck_sends()

        message.refresh_from_db()
        self.assertEqual(result["retried"], 1)
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(mock_send.call_count, 1)  # exactly one real send, no duplicate
        self.assertEqual(message.send_attempts.count(), 2)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_2_and_3_crash_before_provider_call_is_a_safe_retry(self, mock_send):
        message = self._message()
        with failpoint("BEFORE_PROVIDER_CALL"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)

        attempt = message.send_attempts.get()
        self.assertEqual(attempt.status, MessageSendAttempt.Status.SENDING)
        self.assertIsNone(attempt.submission_started_at)  # provider was never called
        self.assertEqual(mock_send.call_count, 0)

        self._expire_lease(message)
        sweep_stuck_sends()

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(mock_send.call_count, 1)

    @patch("apps.messaging.services.ConnectionManager.get_send_operation_status")
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_4_provider_accepted_then_worker_died_is_unknown_without_proof(self, mock_send, mock_status):
        # The provider returned success, but the worker died before persisting
        # it (AFTER_PROVIDER_ACCEPT). The provider cannot confirm afterwards.
        mock_status.return_value = None  # e.g. Cloud API: no status lookup
        message = self._message()
        with failpoint("AFTER_PROVIDER_ACCEPT"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)

        self.assertEqual(mock_send.call_count, 1)  # it WAS sent once
        self._expire_lease(message)
        result = sweep_stuck_sends()

        message.refresh_from_db()
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(mock_send.call_count, 1)  # NOT resent
        # Credit neither consumed nor released while the outcome is unknown.
        self.assertEqual(self._reserved(), 1)
        self.assertEqual(self._consumed(), 0)

    @patch("apps.messaging.services.ConnectionManager.get_send_operation_status")
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_5_reconciliation_recovers_via_provider_operation_lookup(self, mock_send, mock_status):
        # Same crash, but this provider (the QR bridge) CAN be asked, and it
        # confirms the send. Reconciliation moves the message to SENT.
        mock_status.return_value = {
            "known": True, "sent": True, "provider_message_id": "wamid.bridge", "ambiguous": False,
        }
        message = self._message()
        with failpoint("BEFORE_DB_UPDATE"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)

        self._expire_lease(message)
        result = sweep_stuck_sends()

        message.refresh_from_db()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(message.provider_message_id, "wamid.bridge")
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(self._consumed(), 1)  # settled exactly once
        self.assertEqual(self._reserved(), 0)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_9_container_restart_leaves_no_permanent_sending(self, mock_send):
        # A restart = every in-flight task vanishes. Model it as a crash right
        # after claim on several messages, then one sweep.
        messages = [self._message() for _ in range(5)]
        for m in messages:
            with failpoint("AFTER_CLAIM"):
                with self.assertRaises(SimulatedWorkerLost):
                    _run_task(m.pk)
            self._expire_lease(m)

        sweep_stuck_sends()

        stuck = Message.objects.filter(status=Message.Status.SENDING).count()
        self.assertEqual(stuck, 0)
        self.assertEqual(
            Message.objects.filter(status=Message.Status.SENT).count(), 5,
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_11_stale_worker_cannot_corrupt_reclaimed_state(self, mock_send):
        # Worker A claims and is about to call the provider. The sweep reclaims
        # the expired lease. Worker A then finishes and tries to persist.
        message = self._message()
        attempt = attempt_send.__wrapped__ if hasattr(attempt_send, "__wrapped__") else None  # noqa: F841

        from .leasing import claim_for_send
        from .services import _persist_send_result

        a = claim_for_send(message, task_id="A")
        # Sweep reclaims (message never submitted -> re-queued).
        Message.objects.filter(pk=message.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        sweep_stuck_sends()
        message.refresh_from_db()
        requeued_status = message.status  # QUEUED, then eager re-send -> SENT

        # Worker A wakes up and tries to record a success against its old lease.
        message.lease_owner = "A-was-here"  # not the current owner
        outcome = _persist_send_result(
            message, self.account, a, SendResult(success=True, provider_message_id="wamid.stale"),
        )
        self.assertEqual(outcome.disposition, "stale")
        message.refresh_from_db()
        # A's write did not land: provider_message_id is not the stale one.
        self.assertNotEqual(message.provider_message_id, "wamid.stale")
        self.assertIn(requeued_status, {Message.Status.QUEUED, Message.Status.SENT})


class ConcurrencyTests(RecoveryBase):
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_10_two_workers_one_message_exactly_one_attempt(self, mock_send):
        from .leasing import claim_for_send

        message = self._message()
        first = claim_for_send(message, task_id="w1")
        second = claim_for_send(message, task_id="w2")

        self.assertIsNotNone(first)
        self.assertIsNone(second)  # the conditional UPDATE matched nothing
        self.assertEqual(message.send_attempts.count(), 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_a_redelivered_task_after_send_is_a_noop(self, mock_send):
        message = self._message()
        _run_task(message.pk)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)

        _run_task(message.pk)  # Celery redelivery
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(self._consumed(), 1)


class WebhookTests(RecoveryBase):
    def _status_event(self, provider_message_id, status, *, correlation=None, event_id=None):
        """Mirror WhatsAppWebhookView: dedup on (account, provider_event_id), signal only if created."""
        payload = {}
        if correlation:
            payload["biz_opaque_callback_data"] = correlation
        event, created = WhatsAppWebhookEvent.objects.get_or_create(
            account=self.account,
            provider_event_id=event_id or f"{provider_message_id}:{status.lower()}",
            defaults={
                "event_type": WhatsAppWebhookEvent.EventType.MESSAGE_STATUS,
                "payload": payload,
                "related_message_id": provider_message_id or "",
                "status": status,
            },
        )
        if created:
            webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)
        return event

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_6_webhook_before_worker_response_wins_and_worker_does_not_clobber(self, mock_send):
        message = self._message()
        from .leasing import claim_for_send
        from .services import _persist_send_result

        a = claim_for_send(message, task_id="A")
        Message.objects.filter(pk=message.pk).update(provider_message_id="wamid.race")
        message.refresh_from_db()

        # Delivery receipt lands while the message is still SENDING.
        self._status_event("wamid.race", "DELIVERED")
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)

        # The worker's own response arrives late.
        outcome = _persist_send_result(
            message, self.account, a, SendResult(success=True, provider_message_id="wamid.race"),
        )
        self.assertEqual(outcome.disposition, "stale")
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)  # not walked back to SENT

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_7_duplicate_webhook_is_idempotent(self, mock_send):
        message = self._message()
        _run_task(message.pk)
        message.refresh_from_db()
        pmid = message.provider_message_id

        for _ in range(3):
            self._status_event(pmid, "DELIVERED", event_id=f"{pmid}:delivered")

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)
        self.assertEqual(
            WhatsAppWebhookEvent.objects.filter(related_message_id=pmid).count(), 1,
        )

    @patch("apps.messaging.services.ConnectionManager.get_send_operation_status", return_value=None)
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_webhook_settles_a_recovery_required_message_and_its_credit(self, mock_send, mock_status):
        message = self._message()
        with failpoint("AFTER_PROVIDER_ACCEPT"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)
        self._expire_lease(message)
        sweep_stuck_sends()
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(self._consumed(), 0)

        # A delivery receipt, correlated by the attempt's idempotency key,
        # proves it went out. The message advances and the credit is consumed.
        attempt = message.send_attempts.order_by("-attempt_number").first()
        self._status_event("wamid.late", "DELIVERED", correlation=attempt.idempotency_key)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)
        self.assertEqual(message.provider_message_id, "wamid.late")
        self.assertEqual(self._consumed(), 1)
        self.assertEqual(self._reserved(), 0)


class RedisIndependenceTests(RecoveryBase):
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_8_recovery_works_with_the_cache_cleared(self, mock_send):
        from django.core.cache import cache

        message = self._message()
        with failpoint("AFTER_CLAIM"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)

        cache.clear()  # stand-in for a Redis restart; the lease lives in Postgres
        self._expire_lease(message)
        sweep_stuck_sends()

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)


class UnknownPolicyTests(RecoveryBase):
    @patch("apps.messaging.services.ConnectionManager.get_send_operation_status", return_value=None)
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_12_retry_after_unknown_is_blocked_without_proof(self, mock_send, mock_status):
        message = self._message()
        with failpoint("AFTER_PROVIDER_ACCEPT"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)
        self._expire_lease(message)
        sweep_stuck_sends()
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)

        with self.assertRaises(RecoveryActionError):
            resolve_message(message, "retry_safe", actor=self.user)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(mock_send.call_count, 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_operator_can_confirm_sent_and_the_credit_is_charged_once(self, mock_send):
        message = self._message()
        with failpoint("BEFORE_DB_UPDATE"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)
        self._expire_lease(message)
        with patch(
            "apps.messaging.services.ConnectionManager.get_send_operation_status", return_value=None,
        ):
            sweep_stuck_sends()
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)

        resolve_message(message, "confirm_sent", actor=self.user)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(self._consumed(), 1)
        resolve_message.__call__  # keep import used
        # Idempotent: a second confirm cannot double-charge.
        message.status = Message.Status.RECOVERY_REQUIRED
        message.save(update_fields=["status"])
        resolve_message(message, "confirm_sent", actor=self.user)
        self.assertEqual(self._consumed(), 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_operator_can_mark_failed_and_the_credit_is_released(self, mock_send):
        message = self._message()
        with failpoint("BEFORE_DB_UPDATE"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)
        self._expire_lease(message)
        with patch(
            "apps.messaging.services.ConnectionManager.get_send_operation_status", return_value=None,
        ):
            sweep_stuck_sends()

        message.refresh_from_db()
        resolve_message(message, "mark_failed", actor=self.user)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.FAILED)
        self.assertEqual(self._reserved(), 0)
        self.assertEqual(self._consumed(), 0)


class CreditSafetyTests(RecoveryBase):
    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_13_reservation_plus_crash_never_double_charges(self, mock_send):
        message = self._message()
        with failpoint("AFTER_CLAIM"):
            with self.assertRaises(SimulatedWorkerLost):
                _run_task(message.pk)
        self._expire_lease(message)
        sweep_stuck_sends()  # re-queues -> eager re-send -> SENT

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(self._consumed(), 1)
        self.assertEqual(self._reserved(), 0)

        # Redelivery of the very first task must not charge again.
        _run_task(message.pk)
        self.assertEqual(self._consumed(), 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_crash_leaves_reservation_intact_for_unknown_sends(self, mock_send):
        mock_send.return_value = SendResult(
            success=False, error="timed out", ambiguous=True, code="timeout",
        )
        message = self._message()
        _run_task(message.pk)

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
        self.assertEqual(self._reserved(), 1)   # still held
        self.assertEqual(self._consumed(), 0)


class CampaignCompletionTests(RecoveryBase):
    def test_14_campaign_with_unknown_messages_does_not_hang(self):
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C", whatsapp_account=self.account,
            status=Campaign.Status.RUNNING,
        )
        sent = self._message(campaign=campaign, reserve=False)
        sent.status = Message.Status.SENT
        sent.save(update_fields=["status"])
        unknown = self._message(campaign=campaign, reserve=False)
        unknown.status = Message.Status.RECOVERY_REQUIRED
        unknown.save(update_fields=["status"])

        refresh_campaign_counters(campaign.pk)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, Campaign.Status.COMPLETED)
        self.assertEqual(campaign.recovery_required_count, 1)
        self.assertEqual(campaign.sent_count, 1)

    @patch("apps.messaging.services.ConnectionManager.send_text_message", side_effect=_ok_send)
    def test_15_many_messages_with_random_worker_failures(self, mock_send):
        random.seed(1234)
        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="Bulk", whatsapp_account=self.account,
            status=Campaign.Status.RUNNING,
        )
        points = [None, "AFTER_CLAIM", "BEFORE_PROVIDER_CALL", "AFTER_PROVIDER_ACCEPT"]
        messages = [self._message(campaign=campaign) for _ in range(40)]

        with patch(
            "apps.messaging.services.ConnectionManager.get_send_operation_status", return_value=None,
        ):
            for m in messages:
                point = random.choice(points)
                if point is None:
                    _run_task(m.pk)
                    continue
                with failpoint(point):
                    try:
                        _run_task(m.pk)
                    except SimulatedWorkerLost:
                        pass
                self._expire_lease(m)

            # Sweep until it settles (re-queued messages may need another pass).
            for _ in range(5):
                if not Message.objects.filter(status=Message.Status.SENDING).exists():
                    break
                sweep_stuck_sends()

        stuck = Message.objects.filter(
            campaign=campaign, status=Message.Status.SENDING,
        ).count()
        self.assertEqual(stuck, 0, "no message may be left permanently in SENDING")

        campaign.refresh_from_db()
        refresh_campaign_counters(campaign.pk)
        campaign.refresh_from_db()

        by_status = {
            s: Message.objects.filter(campaign=campaign, status=s).count()
            for s in (Message.Status.SENT, Message.Status.RECOVERY_REQUIRED)
        }
        # Every message is in a terminal / recoverable state, and the counts add up.
        self.assertEqual(by_status[Message.Status.SENT] + by_status[Message.Status.RECOVERY_REQUIRED], 40)
        self.assertEqual(campaign.sent_count, by_status[Message.Status.SENT])
        self.assertEqual(campaign.recovery_required_count, by_status[Message.Status.RECOVERY_REQUIRED])

        # Credits: one consume per SENT message, and every RECOVERY_REQUIRED
        # message still holds exactly its reservation. No double charges.
        self.assertEqual(self._consumed(), by_status[Message.Status.SENT])
        self.assertEqual(self._reserved(), by_status[Message.Status.RECOVERY_REQUIRED])


class LegacyBackfillTests(RecoveryBase):
    def test_a_pre_lease_stuck_sending_row_becomes_recovery_required(self):
        # No attempt row at all -- the state a message stranded before this
        # machinery existed is in. It must NOT be blindly re-queued.
        message = self._message()
        Message.objects.filter(pk=message.pk).update(
            status=Message.Status.SENDING,
            lease_expires_at=timezone.now() - timedelta(days=1),
        )
        message.refresh_from_db()

        result = classify_and_recover(message)

        message.refresh_from_db()
        self.assertEqual(result, "unknown")
        self.assertEqual(message.status, Message.Status.RECOVERY_REQUIRED)
