"""
Tests for event emission, signing, delivery and retries.

Two things are being defended here.

The first is that events fire from the *existing* pipeline rather than from a
parallel one -- so several of these drive a real `Message` through the real
signals and assert that a delivery row appeared, rather than calling `emit`
directly.

The second is the signature. A webhook signature that is merely present is
worth nothing; these pin that the documented verification recipe reproduces
what the signer produced, that a tampered body fails, and that an old timestamp
fails. If the docs and the signer ever disagree, one of these breaks.
"""

import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.campaigns.models import Campaign
from apps.contacts.models import Contact
from apps.messaging.models import Message
from apps.messaging.signals import message_status_changed, outbound_message_sent
from apps.whatsapp.models import WhatsAppAccount

from . import events
from .models import WebhookDelivery, WebhookEndpoint
from .tasks import FAILURES_BEFORE_DISABLE, MAX_ATTEMPTS, deliver_webhook, sweep_pending_webhooks
from .tests import API, make_account, make_key, make_tenant, make_user

SECRET = "whsec_0123456789abcdef0123456789abcdef"


def make_endpoint(tenant, *, url="https://receiver.example.com/hook", event_list=None, active=True):
    endpoint = WebhookEndpoint(
        tenant=tenant, url=url, events=event_list or [], is_active=active,
    )
    endpoint.set_secret(SECRET)
    endpoint.save()
    return endpoint


class SignatureTests(TestCase):
    def test_signature_verifies_with_the_documented_recipe(self):
        body = b'{"event":"message.delivered"}'
        timestamp = int(timezone.now().timestamp())

        presented = events.signature(body, SECRET, timestamp)

        self.assertTrue(presented.startswith("sha256="))
        self.assertTrue(events.verify(body, SECRET, timestamp, presented))

    def test_a_tampered_body_fails(self):
        timestamp = int(timezone.now().timestamp())
        presented = events.signature(b'{"amount":10}', SECRET, timestamp)

        self.assertFalse(events.verify(b'{"amount":1000}', SECRET, timestamp, presented))

    def test_the_wrong_secret_fails(self):
        body = b"{}"
        timestamp = int(timezone.now().timestamp())
        presented = events.signature(body, SECRET, timestamp)

        self.assertFalse(events.verify(body, "whsec_someone_elses", timestamp, presented))

    def test_an_old_timestamp_is_refused(self):
        body = b"{}"
        old = int(timezone.now().timestamp()) - 3600

        # Correctly signed, and still refused. This is the replay protection:
        # a captured delivery must stop being accepted.
        presented = events.signature(body, SECRET, old)
        self.assertFalse(events.verify(body, SECRET, old, presented))

    def test_the_timestamp_cannot_be_rewritten(self):
        body = b"{}"
        signed_at = int(timezone.now().timestamp()) - 3600
        presented = events.signature(body, SECRET, signed_at)

        # An attacker replaying the delivery moves the header forward to now.
        # Because the timestamp is inside the signed string, the signature no
        # longer matches -- which is the whole reason it is signed and not
        # merely sent alongside.
        now = int(timezone.now().timestamp())
        self.assertFalse(events.verify(body, SECRET, now, presented))

    def test_signature_is_hex_sha256(self):
        signature = events.signature(b"{}", SECRET, 1)

        algorithm, digest = signature.split("=", 1)
        self.assertEqual(algorithm, "sha256")
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises if it is not hex


class EventEmissionTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("evt-tenant")
        self.user = make_user(self.tenant, "evt-owner", credits=100)
        self.account = make_account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919999999999",
        )
        self.endpoint = make_endpoint(self.tenant)

    def message(self, **overrides):
        fields = {
            "tenant": self.tenant, "whatsapp_account": self.account,
            "contact": self.contact, "to_phone": "+919999999999", "body": "Hi",
        }
        fields.update(overrides)
        return Message.objects.create(**fields)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_outbound_send_emits_message_sent(self, _delay):
        message = self.message()

        outbound_message_sent.send(sender=Message, message=message)

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.MESSAGE_SENT)
        self.assertEqual(delivery.endpoint_id, self.endpoint.pk)
        self.assertEqual(delivery.payload["data"]["message_id"], f"msg_{message.pk}")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_status_change_emits_delivered(self, _delay):
        message = self.message(status=Message.Status.DELIVERED)

        message_status_changed.send(
            sender=Message, message=message, status=Message.Status.DELIVERED,
        )

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.MESSAGE_DELIVERED)
        self.assertEqual(delivery.payload["data"]["status"], "delivered")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_sent_is_not_emitted_twice(self, _delay):
        """
        A `SENT` receipt arriving after the send must not produce a second
        `message.sent` -- an integration counting them would count it twice.
        """
        message = self.message(status=Message.Status.SENT)

        outbound_message_sent.send(sender=Message, message=message)
        message_status_changed.send(sender=Message, message=message, status=Message.Status.SENT)

        self.assertEqual(WebhookDelivery.objects.count(), 1)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_skipped_is_reported_as_failed(self, _delay):
        message = self.message(
            status=Message.Status.SKIPPED,
            failure_reason=Message.FailureReason.DEVICE_DISCONNECTED,
        )

        message_status_changed.send(
            sender=Message, message=message, status=Message.Status.SKIPPED,
        )

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.MESSAGE_FAILED)
        # The distinction survives in the payload even though the event collapses.
        self.assertEqual(delivery.payload["data"]["failure_reason"], "device_disconnected")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_inbound_message_emits_message_received(self, _delay):
        from apps.inbox.signals import inbound_message_recorded

        message = self.message(direction=Message.Direction.INBOUND, body="Hello")

        inbound_message_recorded.send(
            sender=Message, message=message, is_new_conversation=False,
        )

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.MESSAGE_RECEIVED)
        self.assertEqual(delivery.payload["data"]["text"], "Hello")
        self.assertEqual(delivery.payload["data"]["from"], "+919999999999")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_button_reply_emits_button_clicked_instead(self, _delay):
        from apps.inbox.signals import inbound_message_recorded

        message = self.message(
            direction=Message.Direction.INBOUND, body="Interested",
            replied_button_id="interested",
            interaction_type=Message.Interaction.NATIVE_BUTTON,
        )

        inbound_message_recorded.send(
            sender=Message, message=message, is_new_conversation=False,
        )

        # One event, not two.
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.BUTTON_CLICKED)
        self.assertEqual(delivery.payload["data"]["button_id"], "interested")
        self.assertEqual(delivery.payload["data"]["button_text"], "Interested")
        self.assertEqual(delivery.payload["data"]["interaction_type"], "native_button")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_numbered_fallback_reaches_the_same_event(self, _delay):
        """
        A QR-linked number cannot show real buttons, so the contact types "2".
        The existing inbound path resolves that to the same `replied_button_id`;
        this proves the webhook layer reads that resolution rather than parsing
        anything itself.
        """
        from apps.inbox.signals import inbound_message_recorded

        message = self.message(
            direction=Message.Direction.INBOUND, body="Interested",
            replied_button_id="interested",
            interaction_type=Message.Interaction.NUMBERED_FALLBACK,
        )

        inbound_message_recorded.send(
            sender=Message, message=message, is_new_conversation=False,
        )

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.BUTTON_CLICKED)
        self.assertEqual(delivery.payload["data"]["button_id"], "interested")
        self.assertEqual(delivery.payload["data"]["interaction_type"], "numbered_fallback")

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_account_connection_emits_events(self, _delay):
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(
            status=WhatsAppAccount.Status.DISCONNECTED,
        )
        account = WhatsAppAccount.objects.get(pk=self.account.pk)

        account.status = WhatsAppAccount.Status.CONNECTED
        account.save(update_fields=["status"])

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.event_type, events.ACCOUNT_CONNECTED)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_intermediate_account_states_emit_nothing(self, _delay):
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(
            status=WhatsAppAccount.Status.CONNECTING,
        )
        account = WhatsAppAccount.objects.get(pk=self.account.pk)

        account.status = WhatsAppAccount.Status.QR_READY
        account.save(update_fields=["status"])

        # Neither state can send, so nothing an integration can act on changed.
        self.assertFalse(WebhookDelivery.objects.exists())

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_campaign_completion_emits_an_event(self, _delay):
        from apps.messaging.services import refresh_campaign_counters

        campaign = Campaign.objects.create(
            tenant=self.tenant, created_by=self.user, name="C",
            whatsapp_account=self.account, status=Campaign.Status.RUNNING,
        )
        self.message(campaign=campaign, status=Message.Status.SENT)

        refresh_campaign_counters(campaign.pk)

        completion = WebhookDelivery.objects.filter(event_type=events.CAMPAIGN_COMPLETED)
        self.assertEqual(completion.count(), 1)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_endpoints_only_receive_what_they_subscribed_to(self, _delay):
        self.endpoint.events = [events.MESSAGE_FAILED]
        self.endpoint.save(update_fields=["events"])
        message = self.message()

        outbound_message_sent.send(sender=Message, message=message)

        self.assertFalse(WebhookDelivery.objects.exists())

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_an_inactive_endpoint_receives_nothing(self, _delay):
        WebhookEndpoint.objects.filter(pk=self.endpoint.pk).update(is_active=False)

        outbound_message_sent.send(sender=Message, message=self.message())

        self.assertFalse(WebhookDelivery.objects.exists())

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_events_do_not_cross_tenants(self, _delay):
        other = make_tenant("evt-other")
        make_endpoint(other, url="https://other.example.com/hook")

        outbound_message_sent.send(sender=Message, message=self.message())

        self.assertEqual(
            list(WebhookDelivery.objects.values_list("tenant_id", flat=True)), [self.tenant.pk],
        )

    @override_settings(PUBLIC_API_WEBHOOKS_ENABLED=False)
    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_the_kill_switch_stops_emission(self, _delay):
        outbound_message_sent.send(sender=Message, message=self.message())

        self.assertFalse(WebhookDelivery.objects.exists())

    @patch("apps.publicapi.events.build", side_effect=RuntimeError("boom"))
    def test_a_broken_emit_never_breaks_the_send(self, _build):
        with patch("apps.publicapi.events.logger") as mock_logger:
            # The assertion is that this does not raise. A receiver sitting on
            # the send path that can throw is a receiver that can fail the
            # send, which is worse than having no webhooks at all.
            outbound_message_sent.send(sender=Message, message=self.message())

        # Swallowed, but loudly -- a silently dropped event is undebuggable.
        mock_logger.exception.assert_called_once()
        self.assertFalse(WebhookDelivery.objects.exists())


class DeliveryTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("del-tenant")
        self.endpoint = make_endpoint(self.tenant)
        envelope = events.build(events.MESSAGE_DELIVERED, {"message_id": "msg_1"})
        envelope["id"] = "evt_test"
        self.delivery = WebhookDelivery.objects.create(
            tenant=self.tenant, endpoint=self.endpoint,
            event_type=events.MESSAGE_DELIVERED,
            event_id="evt_test", payload=envelope,
            next_attempt_at=timezone.now(),
        )

    @patch("apps.publicapi.tasks.requests.post")
    def test_successful_delivery_is_signed_and_recorded(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text="ok")

        deliver_webhook(self.delivery.pk)

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.SUCCEEDED)
        self.assertEqual(self.delivery.attempt_count, 1)
        self.assertEqual(self.delivery.http_status, 200)
        self.assertIsNotNone(self.delivery.delivered_at)

        _, kwargs = mock_post.call_args
        headers = kwargs["headers"]
        body = kwargs["data"]
        self.assertTrue(events.verify(
            body, SECRET, headers[events.TIMESTAMP_HEADER], headers[events.SIGNATURE_HEADER],
        ))
        self.assertEqual(headers[events.EVENT_HEADER], events.MESSAGE_DELIVERED)
        self.assertEqual(headers[events.DELIVERY_HEADER], "evt_test")

    @patch("apps.publicapi.tasks.requests.post")
    def test_redirects_are_not_followed(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text="ok")

        deliver_webhook(self.delivery.pk)

        # Following a redirect would deliver to an address the SSRF guard never
        # checked.
        self.assertFalse(mock_post.call_args[1]["allow_redirects"])

    @patch("apps.publicapi.tasks.requests.post")
    def test_success_clears_the_failure_counter(self, mock_post):
        WebhookEndpoint.objects.filter(pk=self.endpoint.pk).update(consecutive_failures=5)
        mock_post.return_value = Mock(status_code=204, text="")

        deliver_webhook(self.delivery.pk)

        self.endpoint.refresh_from_db()
        self.assertEqual(self.endpoint.consecutive_failures, 0)
        self.assertIsNotNone(self.endpoint.last_success_at)

    @patch("apps.publicapi.tasks.requests.post")
    def test_a_4xx_is_terminal(self, mock_post):
        mock_post.return_value = Mock(status_code=404, text="no such route")

        deliver_webhook(self.delivery.pk)

        self.delivery.refresh_from_db()
        # Not retried: a receiver answering 404 will answer 404 again, and
        # retrying hides a wrong URL behind five identical rejections.
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.FAILED)
        self.assertEqual(self.delivery.attempt_count, 1)
        self.assertEqual(mock_post.call_count, 1)

    @patch("apps.publicapi.tasks.requests.post")
    def test_a_5xx_is_retried(self, mock_post):
        """
        `.apply()` under CELERY_TASK_ALWAYS_EAGER runs the retry ladder inline,
        so the whole thing happens in this call: the assertion is that the
        receiver was tried MAX_ATTEMPTS times before the delivery gave up.
        Contrast `test_a_4xx_is_terminal`, which is tried exactly once.
        """
        mock_post.return_value = Mock(status_code=503, text="down")

        deliver_webhook.apply(args=[self.delivery.pk])

        self.assertEqual(mock_post.call_count, MAX_ATTEMPTS)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.attempt_count, MAX_ATTEMPTS)
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.FAILED)

    @patch("apps.publicapi.tasks.requests.post")
    def test_a_429_is_retried(self, mock_post):
        mock_post.return_value = Mock(status_code=429, text="slow down")

        deliver_webhook.apply(args=[self.delivery.pk])

        # 429 and 408 are the two 4xx that are *not* terminal: both mean
        # "later", not "never".
        self.assertEqual(mock_post.call_count, MAX_ATTEMPTS)

    @patch("apps.publicapi.tasks.requests.post")
    def test_a_connection_error_is_retried(self, mock_post):
        import requests as requests_lib

        mock_post.side_effect = requests_lib.ConnectionError("refused")

        deliver_webhook.apply(args=[self.delivery.pk])

        self.assertEqual(mock_post.call_count, MAX_ATTEMPTS)

    @patch("apps.publicapi.tasks.requests.post")
    def test_retries_back_off_rather_than_hammering(self, mock_post):
        """Each retry is scheduled with a wait, not fired immediately."""
        mock_post.return_value = Mock(status_code=503, text="down")

        with patch("apps.publicapi.tasks.retry_delay", return_value=5.0) as mock_delay:
            deliver_webhook.apply(args=[self.delivery.pk])

        self.assertEqual(mock_delay.call_count, MAX_ATTEMPTS - 1)
        # Called with an increasing attempt number, which is what makes the
        # backoff exponential rather than flat.
        self.assertEqual(
            [call.args[0] for call in mock_delay.call_args_list],
            list(range(MAX_ATTEMPTS - 1)),
        )

    @patch("apps.publicapi.tasks.requests.post")
    def test_attempts_are_exhausted_then_it_fails(self, mock_post):
        mock_post.return_value = Mock(status_code=500, text="err")
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(
            attempt_count=MAX_ATTEMPTS - 1,
        )

        deliver_webhook(self.delivery.pk)

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.FAILED)

    @patch("apps.publicapi.tasks.requests.post")
    def test_an_endpoint_that_keeps_failing_is_disabled(self, mock_post):
        mock_post.return_value = Mock(status_code=404, text="gone")
        WebhookEndpoint.objects.filter(pk=self.endpoint.pk).update(
            consecutive_failures=FAILURES_BEFORE_DISABLE - 1,
        )

        deliver_webhook(self.delivery.pk)

        self.endpoint.refresh_from_db()
        self.assertFalse(self.endpoint.is_active)
        self.assertIsNotNone(self.endpoint.auto_disabled_at)

    @patch("apps.publicapi.tasks.requests.post")
    def test_an_internal_url_is_refused_at_delivery_time(self, mock_post):
        # A hostname that resolved publicly when it was saved can resolve to
        # loopback later. This is the check that catches a DNS rebind.
        WebhookEndpoint.objects.filter(pk=self.endpoint.pk).update(
            url="http://169.254.169.254/latest/meta-data/",
        )

        deliver_webhook(self.delivery.pk)

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.FAILED)
        mock_post.assert_not_called()

    @patch("apps.publicapi.tasks.requests.post")
    def test_a_disabled_endpoint_is_not_delivered_to(self, mock_post):
        WebhookEndpoint.objects.filter(pk=self.endpoint.pk).update(is_active=False)

        deliver_webhook(self.delivery.pk)

        mock_post.assert_not_called()
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, WebhookDelivery.Status.FAILED)

    @patch("apps.publicapi.tasks.requests.post")
    def test_an_already_succeeded_delivery_is_not_repeated(self, mock_post):
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(
            status=WebhookDelivery.Status.SUCCEEDED,
        )

        deliver_webhook(self.delivery.pk)

        mock_post.assert_not_called()

    def test_a_missing_delivery_is_not_an_error(self):
        # The row may not be visible yet -- `emit` can run inside an open
        # transaction. Returning quietly is what leaves it to the sweeper.
        deliver_webhook(999999)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_the_sweeper_requeues_stranded_deliveries(self, mock_delay):
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        count = sweep_pending_webhooks()

        self.assertEqual(count, 1)
        mock_delay.assert_called_once_with(self.delivery.pk)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_the_sweeper_leaves_finished_deliveries_alone(self, mock_delay):
        WebhookDelivery.objects.filter(pk=self.delivery.pk).update(
            status=WebhookDelivery.Status.SUCCEEDED,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=5),
        )

        self.assertEqual(sweep_pending_webhooks(), 0)
        mock_delay.assert_not_called()

    @patch("apps.publicapi.tasks.requests.post")
    def test_the_payload_is_the_documented_envelope(self, mock_post):
        mock_post.return_value = Mock(status_code=200, text="")

        deliver_webhook(self.delivery.pk)

        body = json.loads(mock_post.call_args[1]["data"])
        self.assertEqual(set(body), {"id", "event", "timestamp", "data"})


@API
class WebhookManagementTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("whm-tenant")
        self.user = make_user(self.tenant, "whm-owner")
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    @override_settings(PUBLIC_API_REQUIRE_HTTPS_WEBHOOKS=True)
    def test_create_returns_the_secret_once(self):
        response = self.client.post(
            "/api/public/v1/webhooks",
            data={"url": "https://receiver.example.com/hook", "events": ["message.delivered"]},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 201)
        secret = response.json()["secret"]
        self.assertTrue(secret.startswith("whsec_"))

        # And never again on a read.
        endpoint_id = response.json()["id"]
        read = self.client.get(f"/api/public/v1/webhooks/{endpoint_id}", **self.auth())
        self.assertNotIn("secret", read.json())

    @override_settings(PUBLIC_API_REQUIRE_HTTPS_WEBHOOKS=True)
    def test_plaintext_webhook_url_is_refused(self):
        response = self.client.post(
            "/api/public/v1/webhooks",
            data={"url": "http://receiver.example.com/hook"},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 400)

    def test_an_internal_url_is_refused_at_creation(self):
        response = self.client.post(
            "/api/public/v1/webhooks",
            data={"url": "https://169.254.169.254/latest/meta-data/"},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_an_unknown_event_is_refused(self):
        response = self.client.post(
            "/api/public/v1/webhooks",
            data={"url": "https://receiver.example.com/hook", "events": ["message.telepathed"]},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 400)

    def test_rotate_replaces_the_secret(self):
        endpoint = make_endpoint(self.tenant)

        response = self.client.post(
            f"/api/public/v1/webhooks/{endpoint.pk}/rotate-secret", **self.auth(),
        )

        self.assertEqual(response.status_code, 200)
        new_secret = response.json()["secret"]
        self.assertNotEqual(new_secret, SECRET)
        endpoint.refresh_from_db()
        self.assertEqual(endpoint.get_secret(), new_secret)

    @patch("apps.publicapi.tasks.deliver_webhook.delay")
    def test_test_delivery_uses_the_real_path(self, mock_delay):
        endpoint = make_endpoint(self.tenant)

        response = self.client.post(
            f"/api/public/v1/webhooks/{endpoint.pk}/test",
            data={"event": "message.sent"},
            content_type="application/json", **self.auth(),
        )

        self.assertEqual(response.status_code, 202)
        delivery = WebhookDelivery.objects.get()
        self.assertTrue(delivery.payload["data"]["test"])
        mock_delay.assert_called_once_with(delivery.pk)

    def test_re_enabling_clears_the_circuit_breaker(self):
        endpoint = make_endpoint(self.tenant, active=False)
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
            consecutive_failures=30, auto_disabled_at=timezone.now(), last_error="gone",
        )

        self.client.patch(
            f"/api/public/v1/webhooks/{endpoint.pk}",
            data={"is_active": True},
            content_type="application/json", **self.auth(),
        )

        endpoint.refresh_from_db()
        self.assertTrue(endpoint.is_active)
        self.assertEqual(endpoint.consecutive_failures, 0)
        self.assertIsNone(endpoint.auto_disabled_at)

    def test_delivery_log_is_tenant_scoped(self):
        other = make_tenant("whm-other")
        other_endpoint = make_endpoint(other, url="https://other.example.com/hook")
        WebhookDelivery.objects.create(
            tenant=other, endpoint=other_endpoint, event_type=events.MESSAGE_SENT,
            event_id="evt_other", payload={},
        )

        response = self.client.get("/api/public/v1/webhooks/deliveries", **self.auth())

        self.assertEqual(response.json()["pagination"]["total"], 0)
