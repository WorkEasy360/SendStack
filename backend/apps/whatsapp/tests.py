from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant

from .connections.manager import ConnectionManager
from .connections.session import decrypt_secret, encrypt_secret
from .models import WhatsAppAccount, WhatsAppConnectionSession, WhatsAppWebhookEvent
from .providers.base import (
    ConnectionResult,
    ConnectionSessionResult,
    ConnectionStatusResult,
    ConnectionWebhookEvent,
    ProviderError,
    SendResult,
    WebhookEventType,
)
from .providers.cloud_api import WhatsAppCloudAPIProvider
from .providers.qr_bridge import NotConfiguredProvider, WhatsAppQRBridgeProvider


def _make_account(tenant, user, **overrides):
    defaults = {
        "tenant": tenant,
        "user": user,
        "name": "Support Line",
        "account_type": WhatsAppAccount.AccountType.API,
        "connection_method": WhatsAppAccount.ConnectionMethod.CLOUD_API,
        "phone_number_id": "1234567890",
        "webhook_verify_token": "verify-me",
    }
    defaults.update(overrides)
    account = WhatsAppAccount(**defaults)
    account.set_access_token("test-access-token")
    account.set_app_secret("test-app-secret")
    account.save()
    return account


def _make_qr_account(tenant, user, **overrides):
    defaults = {
        "account_type": WhatsAppAccount.AccountType.WHATSAPP,
        "connection_method": WhatsAppAccount.ConnectionMethod.QR,
        "name": "Sales WhatsApp",
    }
    defaults.update(overrides)
    account = WhatsAppAccount(tenant=tenant, user=user, **defaults)
    account.save()
    return account


class TokenEncryptionTests(TestCase):
    def test_round_trip_encrypts_at_rest_and_decrypts_back(self):
        encrypted = encrypt_secret("super-secret-token")
        self.assertNotIn("super-secret-token", encrypted)
        self.assertEqual(decrypt_secret(encrypted), "super-secret-token")

    def test_account_helpers_store_ciphertext_not_plaintext(self):
        tenant = Tenant.objects.create(name="Acme", slug="acme-wa", kind=Tenant.Kind.CUSTOMER)
        user = get_user_model().objects.create_user(username="acme-owner-wa", password="x", tenant=tenant)
        account = _make_account(tenant, user)

        self.assertNotIn("test-access-token", account.access_token_encrypted)
        self.assertEqual(account.get_access_token(), "test-access-token")
        self.assertEqual(account.get_app_secret(), "test-app-secret")


class AccountProviderComputationTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-provider-calc", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-provider-calc-owner", password="x", tenant=self.tenant)

    def test_cloud_api_account_gets_cloud_api_provider(self):
        account = _make_account(self.tenant, self.user)
        self.assertEqual(account.provider, WhatsAppAccount.Provider.CLOUD_API)

    def test_qr_account_gets_qr_provider_regardless_of_input(self):
        account = _make_qr_account(self.tenant, self.user, provider=WhatsAppAccount.Provider.CLOUD_API)
        # `provider` is always derived server-side from connection_method --
        # never trusted from a client-supplied value.
        self.assertEqual(account.provider, WhatsAppAccount.Provider.QR_PROVIDER)


class CloudAPIProviderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-provider", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-provider-owner", password="x", tenant=self.tenant)
        self.account = _make_account(self.tenant, self.user)
        self.provider = WhatsAppCloudAPIProvider()

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_get_account_info_success(self, mock_request):
        mock_request.return_value = Mock(
            ok=True,
            json=lambda: {"display_phone_number": "+1 555 0100", "verified_name": "Acme Support"},
        )

        result = self.provider.get_account_info(self.account)

        self.assertTrue(result.connected)
        self.assertEqual(result.phone_number, "+1 555 0100")

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_get_account_info_failure_surfaces_provider_error(self, mock_request):
        mock_request.return_value = Mock(
            ok=False,
            status_code=401,
            json=lambda: {"error": {"message": "Invalid OAuth access token", "code": 190}},
        )

        result = self.provider.get_account_info(self.account)

        self.assertFalse(result.connected)
        self.assertIn("Invalid OAuth", result.error)

    def test_get_qr_not_supported(self):
        with self.assertRaises(ProviderError) as ctx:
            self.provider.get_qr(self.account, None)
        self.assertEqual(ctx.exception.code, "not_supported")

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_send_text_message_returns_provider_message_id(self, mock_request):
        mock_request.return_value = Mock(
            ok=True,
            json=lambda: {"messages": [{"id": "wamid.abc123"}]},
        )

        result = self.provider.send_text_message(self.account, "15551234567", "Hello!")

        self.assertTrue(result.success)
        self.assertEqual(result.provider_message_id, "wamid.abc123")
        sent_kwargs = mock_request.call_args.kwargs
        self.assertEqual(sent_kwargs["json"]["text"]["body"], "Hello!")
        self.assertEqual(sent_kwargs["headers"]["Authorization"], "Bearer test-access-token")

    def test_send_media_message_rejects_unsupported_type(self):
        result = self.provider.send_media_message(self.account, "15551234567", "gif", "https://example.test/x.gif")

        self.assertFalse(result.success)
        self.assertIn("Unsupported", result.error)

    def test_verify_webhook_challenge_matches_token(self):
        params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"}
        self.assertEqual(self.provider.verify_webhook_challenge(self.account, params), "12345")

    def test_verify_webhook_challenge_rejects_wrong_token(self):
        params = {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"}
        self.assertIsNone(self.provider.verify_webhook_challenge(self.account, params))

    def test_validate_webhook_signature(self):
        import hashlib
        import hmac

        body = b'{"entry": []}'
        signature = "sha256=" + hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()

        self.assertTrue(self.provider.validate_webhook_signature(self.account, body, signature))
        self.assertFalse(self.provider.validate_webhook_signature(self.account, body, "sha256=deadbeef"))
        self.assertFalse(self.provider.validate_webhook_signature(self.account, body, None))

    def test_parse_webhook_payload_extracts_statuses_and_messages(self):
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"id": "wamid.1", "status": "delivered"}],
                                "messages": [{"id": "wamid.2", "from": "15551234567", "type": "text"}],
                                "contacts": [{"wa_id": "15551234567", "profile": {"name": "Priya"}}],
                            }
                        }
                    ]
                }
            ]
        }

        events = self.provider.parse_webhook_payload(payload)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, WebhookEventType.MESSAGE_STATUS)
        self.assertEqual(events[0].provider_event_id, "wamid.1:delivered")
        self.assertEqual(events[1].event_type, WebhookEventType.INBOUND_MESSAGE)
        self.assertEqual(events[1].provider_event_id, "wamid.2")
        self.assertEqual(events[1].raw["message"]["from"], "15551234567")
        self.assertEqual(events[1].raw["profile_name"], "Priya")


class NotConfiguredQRProviderTests(TestCase):
    """The QR_PROVIDER registry entry when QR_PROVIDER_BASE_URL is unset -- must never fake a connection."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-qr-not-configured", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-qr-nc-owner", password="x", tenant=self.tenant)
        self.account = _make_qr_account(self.tenant, self.user)
        self.provider = NotConfiguredProvider()

    def test_start_connection_raises_not_configured(self):
        with self.assertRaises(ProviderError) as ctx:
            self.provider.start_connection(self.account)
        self.assertEqual(ctx.exception.code, "not_configured")

    def test_connection_manager_surfaces_friendly_error(self):
        result = ConnectionManager.start_connection(self.account)

        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "QR provider is not configured.")
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.ERROR)


class QRBridgeProviderTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-qr-bridge", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-qr-bridge-owner", password="x", tenant=self.tenant)
        self.account = _make_qr_account(self.tenant, self.user)
        self.provider = WhatsAppQRBridgeProvider(
            base_url="https://bridge.test",
            api_key="test-key",
            webhook_secret="test-webhook-secret",
        )

    @patch("apps.whatsapp.providers.qr_bridge.requests.Session.request")
    def test_start_connection_returns_qr_ready(self, mock_request):
        mock_request.return_value = Mock(
            ok=True,
            status_code=200,
            json=lambda: {
                "session_id": "sess-1",
                "status": "QR_READY",
                "qr": "data:image/png;base64,abc123",
                "expires_at": "2026-08-18T05:00:00+00:00",
            },
        )

        result = self.provider.start_connection(self.account)

        self.assertEqual(result.status, "QR_READY")
        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(result.qr_data, "data:image/png;base64,abc123")

    @patch("apps.whatsapp.providers.qr_bridge.requests.Session.request")
    def test_start_connection_sends_distinct_connection_and_message_webhook_urls(self, mock_request):
        # Regression test: pairing/lifecycle events and inbound
        # messages/statuses land on different Django views with different
        # signature header conventions -- the bridge must be told both URLs
        # explicitly rather than guessing one from the other.
        mock_request.return_value = Mock(ok=True, status_code=200, json=lambda: {"session_id": "sess-1", "status": "QR_READY"})

        self.provider.start_connection(self.account)

        payload = mock_request.call_args.kwargs["json"]
        self.assertTrue(payload["connection_webhook_url"].endswith("/api/v1/whatsapp/webhooks/qr_provider/"))
        self.assertTrue(payload["message_webhook_url"].endswith(f"/api/v1/whatsapp/webhook/{self.account.pk}/"))

    def test_signature_validation(self):
        import hashlib
        import hmac

        body = b'{"session_id": "sess-1", "status": "CONNECTED"}'
        signature = "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

        self.assertTrue(self.provider.validate_webhook_signature(self.account, body, signature))
        self.assertFalse(self.provider.validate_webhook_signature(self.account, body, "sha256=deadbeef"))

    def test_parse_connection_webhook_rejects_bad_signature(self):
        body = b'{"session_id": "sess-1", "status": "CONNECTED"}'
        event = self.provider.parse_connection_webhook(body, "sha256=deadbeef")
        self.assertIsNone(event)

    def test_parse_connection_webhook_parses_valid_payload(self):
        import hashlib
        import hmac

        body = b'{"session_id": "sess-1", "status": "CONNECTED", "phone_number": "+15550100"}'
        signature = "sha256=" + hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

        event = self.provider.parse_connection_webhook(body, signature)

        self.assertIsNotNone(event)
        self.assertEqual(event.session_id, "sess-1")
        self.assertEqual(event.status, "CONNECTED")
        self.assertEqual(event.phone_number, "+15550100")

    def test_send_template_message_not_supported(self):
        result_error = None
        try:
            self.provider.send_template_message(self.account, "15551234567", "hello_world", "en_US")
        except ProviderError as exc:
            result_error = exc
        self.assertIsNotNone(result_error)
        self.assertEqual(result_error.code, "not_supported")

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_send_strips_formatting_from_recipient(self, mock_request):
        # Regression test: we store E.164 ("+91..."), but the bridge appends
        # "@s.whatsapp.net" to whatever it is given, and WhatsApp's device
        # lookup for a JID with a leading "+" never resolves -- the send then
        # hung until the caller's timeout rather than failing. Every send must
        # reach the bridge as bare digits.
        self.account.provider_session_id = "sess-1"
        mock_request.return_value = Mock(
            ok=True, status_code=200, json=lambda: {"success": True, "provider_message_id": "wamid.1"}
        )

        for stored in ("+918429577706", "+91 84295 77706", "(918) 429-577706"):
            with self.subTest(stored=stored):
                result = self.provider.send_text_message(self.account, stored, "hi")

                self.assertTrue(result.success)
                self.assertEqual(mock_request.call_args.kwargs["json"]["to"], "918429577706")

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_send_rejects_recipient_with_no_digits(self, mock_request):
        self.account.provider_session_id = "sess-1"

        result = self.provider.send_text_message(self.account, "not-a-number", "hi")

        self.assertFalse(result.success)
        self.assertFalse(result.ambiguous)
        mock_request.assert_not_called()

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_read_timeout_is_ambiguous_but_connect_timeout_is_not(self, mock_request):
        # A read timeout means the request was already transmitted, so the
        # message may have gone out; a connect timeout means it never left.
        # Only the former must block a retry (see apps/messaging/tasks.py).
        self.account.provider_session_id = "sess-1"

        mock_request.side_effect = requests.ReadTimeout("read timed out")
        self.assertTrue(self.provider.send_text_message(self.account, "918429577706", "hi").ambiguous)

        mock_request.side_effect = requests.ConnectTimeout("connect timed out")
        self.assertFalse(self.provider.send_text_message(self.account, "918429577706", "hi").ambiguous)

        mock_request.side_effect = requests.ConnectionError("refused")
        self.assertFalse(self.provider.send_text_message(self.account, "918429577706", "hi").ambiguous)

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_bridge_reported_ambiguity_is_propagated(self, mock_request):
        # The bridge answers 504 + {"ambiguous": true} when it gave up on a
        # send WhatsApp had already been handed.
        self.account.provider_session_id = "sess-1"
        mock_request.return_value = Mock(
            ok=False, status_code=504, json=lambda: {"error": "no confirmation", "ambiguous": True}
        )

        result = self.provider.send_text_message(self.account, "918429577706", "hi")

        self.assertFalse(result.success)
        self.assertTrue(result.ambiguous)


class QRBridgeInboundPayloadTests(TestCase):
    """
    The bridge posts a flat message object; apps.inbox only understands the
    Cloud API's nested shape. Passing the bridge's object through unchanged
    made every inbound message parse to an empty sender and get dropped, which
    left the inbox permanently empty and meant no automation trigger ever fired.
    """

    def setUp(self):
        self.provider = WhatsAppQRBridgeProvider(
            base_url="https://bridge.test", api_key="test-key", webhook_secret="test-webhook-secret",
        )

    def _message(self, payload):
        events = self.provider.parse_webhook_payload({"messages": [payload]})
        self.assertEqual(len(events), 1)
        return events[0]

    def test_text_message_is_nested_under_text_body(self):
        event = self._message({"id": "3EB0", "from": "15551234567", "type": "text", "body": "hello", "push_name": "Priya"})

        self.assertEqual(event.event_type, WebhookEventType.INBOUND_MESSAGE)
        self.assertEqual(event.provider_event_id, "3EB0")
        self.assertEqual(event.raw["profile_name"], "Priya")
        self.assertEqual(event.raw["message"]["from"], "15551234567")
        self.assertEqual(event.raw["message"]["type"], "text")
        self.assertEqual(event.raw["message"]["text"]["body"], "hello")

    def test_media_message_carries_its_caption(self):
        event = self._message({"id": "3EB1", "from": "15551234567", "type": "image", "body": "look at this"})

        self.assertEqual(event.raw["message"]["type"], "image")
        self.assertEqual(event.raw["message"]["image"]["caption"], "look at this")

    def test_unsupported_type_keeps_its_name_and_carries_no_content(self):
        event = self._message({"id": "3EB2", "from": "15551234567", "type": "sticker", "body": None})

        self.assertEqual(event.raw["message"]["type"], "sticker")
        self.assertNotIn("text", event.raw["message"])
        self.assertNotIn("sticker", event.raw["message"])

    def test_missing_push_name_becomes_empty_profile_name(self):
        event = self._message({"id": "3EB3", "from": "15551234567", "type": "text", "body": "hi", "push_name": None})

        self.assertEqual(event.raw["profile_name"], "")


class ConnectionManagerCloudApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-manager", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-manager-owner", password="x", tenant=self.tenant)
        self.account = _make_account(self.tenant, self.user)

    @patch("apps.whatsapp.providers.cloud_api.WhatsAppCloudAPIProvider.get_account_info")
    def test_start_connection_marks_account_connected_on_success(self, mock_info):
        mock_info.return_value = ConnectionResult(connected=True, phone_number="+15550100")

        result = ConnectionManager.start_connection(self.account)

        self.assertTrue(result["success"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertEqual(self.account.phone_number, "+15550100")
        self.assertIsNotNone(self.account.last_connected_at)
        self.assertEqual(WhatsAppConnectionSession.objects.filter(whatsapp_account=self.account).count(), 1)

    @patch("apps.whatsapp.providers.cloud_api.WhatsAppCloudAPIProvider.get_account_info")
    def test_start_connection_marks_account_errored_on_failure(self, mock_info):
        mock_info.return_value = ConnectionResult(connected=False, error="bad token")

        result = ConnectionManager.start_connection(self.account)

        self.assertFalse(result["success"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.ERROR)
        self.assertEqual(self.account.error_message, "bad token")

    def test_send_blocked_when_not_connected(self):
        self.assertEqual(self.account.status, WhatsAppAccount.Status.DISCONNECTED)

        result = ConnectionManager.send_text_message(self.account, "15551234567", "hi")

        self.assertFalse(result.success)
        self.assertIn("not connected", result.error)

    @patch("apps.whatsapp.providers.cloud_api.WhatsAppCloudAPIProvider.send_text_message")
    def test_send_delegates_to_provider_when_connected(self, mock_send):
        self.account.status = WhatsAppAccount.Status.CONNECTED
        self.account.save(update_fields=["status"])
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.1")

        result = ConnectionManager.send_text_message(self.account, "15551234567", "hi")

        self.assertTrue(result.success)
        mock_send.assert_called_once_with(self.account, "15551234567", "hi", operation_id="")

    @patch("apps.whatsapp.providers.cloud_api.WhatsAppCloudAPIProvider.get_account_info")
    def test_test_credentials_does_not_mutate_status(self, mock_info):
        mock_info.return_value = ConnectionResult(connected=True, phone_number="+15550100")

        result = ConnectionManager.test_credentials(self.account)

        self.assertTrue(result["success"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.DISCONNECTED)


class ConnectionManagerQRTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-qr-manager", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-qr-manager-owner", password="x", tenant=self.tenant)
        self.account = _make_qr_account(self.tenant, self.user)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_qr_ready_session_created_and_cached(self, mock_start):
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:image/png;base64,abc", qr_expires_at=expires_at,
        )

        result = ConnectionManager.start_connection(self.account)

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], WhatsAppAccount.Status.QR_READY)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.QR_READY)
        self.assertEqual(self.account.provider_session_id, "sess-1")

        qr_result = ConnectionManager.get_qr(self.account)
        self.assertTrue(qr_result["success"])
        self.assertEqual(qr_result["qr_data"], "data:image/png;base64,abc")

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.get_connection_status")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_get_status_polling_handles_connection_status_result(self, mock_start, mock_get_status):
        # Regression test: get_status() calls _apply_session_result with a
        # ConnectionStatusResult (from provider.get_connection_status),
        # which -- unlike ConnectionSessionResult from start_connection/
        # refresh_qr -- has no session_id field at all. _apply_session_result
        # used to assume every result had .session_id and crashed with
        # AttributeError on every poll of a pending QR session, which is
        # exactly the request the frontend's wizard polls after the user
        # scans -- so a real, successful scan looked like a failure.
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:image/png;base64,abc", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)

        mock_get_status.return_value = ConnectionStatusResult(
            status="CONNECTED", phone_number="+15559990000", display_name="Acme", provider_account_id="device-1",
        )

        result = ConnectionManager.get_status(self.account)

        self.assertEqual(result["status"], WhatsAppAccount.Status.CONNECTED)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertEqual(self.account.phone_number, "+15559990000")

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_qr_expires_after_ttl(self, mock_start):
        expires_at = timezone.now() - timedelta(seconds=1)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:image/png;base64,abc", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)

        status_result = ConnectionManager.get_status(self.account)

        self.assertEqual(status_result["status"], WhatsAppAccount.Status.EXPIRED)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.EXPIRED)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_connection_webhook_marks_connected(self, mock_start):
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:image/png;base64,abc", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)

        event = ConnectionWebhookEvent(
            session_id="sess-1", status="CONNECTED", phone_number="+15559990000", provider_account_id="device-1",
        )
        session = ConnectionManager.handle_connection_webhook(WhatsAppAccount.Provider.QR_PROVIDER, event)

        self.assertIsNotNone(session)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertEqual(self.account.phone_number, "+15559990000")
        self.assertEqual(self.account.provider_account_id, "device-1")

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_expiry_of_a_superseded_session_leaves_a_connected_account_alone(self, mock_start):
        # The bug that made freshly connected numbers read "Expired" about
        # ninety seconds after pairing. A double-fired /connect/ left two
        # provider sessions for one account; only one was scanned, and the
        # unscanned twin's EXPIRED webhook was written onto the account row.
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-scanned", qr_data="data:x", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)
        WhatsAppConnectionSession.objects.create(
            whatsapp_account=self.account, provider=self.account.provider,
            session_id="sess-twin", status=WhatsAppConnectionSession.Status.QR_READY, qr_expires_at=expires_at,
        )
        ConnectionManager.handle_connection_webhook(
            WhatsAppAccount.Provider.QR_PROVIDER,
            ConnectionWebhookEvent(session_id="sess-scanned", status="CONNECTED", phone_number="+15559990000"),
        )

        twin = ConnectionManager.handle_connection_webhook(
            WhatsAppAccount.Provider.QR_PROVIDER,
            ConnectionWebhookEvent(session_id="sess-twin", status="EXPIRED"),
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)
        self.assertEqual(self.account.provider_session_id, "sess-scanned")
        # The twin's own row still closes -- only the account is protected.
        twin.refresh_from_db()
        self.assertEqual(twin.status, WhatsAppConnectionSession.Status.EXPIRED)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_error_and_disconnect_of_a_superseded_session_are_ignored_too(self, mock_start):
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-live", qr_data="data:x", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)
        ConnectionManager.handle_connection_webhook(
            WhatsAppAccount.Provider.QR_PROVIDER,
            ConnectionWebhookEvent(session_id="sess-live", status="CONNECTED", phone_number="+15559990000"),
        )
        WhatsAppConnectionSession.objects.create(
            whatsapp_account=self.account, provider=self.account.provider,
            session_id="sess-old", status=WhatsAppConnectionSession.Status.CONNECTED,
        )

        for status in ("ERROR", "DISCONNECTED"):
            ConnectionManager.handle_connection_webhook(
                WhatsAppAccount.Provider.QR_PROVIDER,
                ConnectionWebhookEvent(session_id="sess-old", status=status, error="replaced"),
            )
            self.account.refresh_from_db()
            self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED, status)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_expiry_of_the_current_session_still_expires_the_account(self, mock_start):
        # The guard must not turn off the real thing: a QR nobody scanned has
        # to read as expired, or the card sits on "Waiting for scan" forever.
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-only", qr_data="data:x", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)

        ConnectionManager.handle_connection_webhook(
            WhatsAppAccount.Provider.QR_PROVIDER,
            ConnectionWebhookEvent(session_id="sess-only", status="EXPIRED"),
        )

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.EXPIRED)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_connecting_twice_while_a_qr_is_waiting_reuses_it(self, mock_start):
        # The second of two simultaneous /connect/ calls: it must be handed the
        # first call's QR, not be given a provider session of its own.
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:x", qr_expires_at=expires_at,
        )
        first = ConnectionManager.start_connection(self.account)

        second = ConnectionManager.start_connection(self.account)

        self.assertEqual(mock_start.call_count, 1)
        self.assertTrue(second["success"])
        self.assertEqual(second["status"], WhatsAppAccount.Status.QR_READY)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["expires_at"], first["expires_at"])
        self.assertEqual(WhatsAppConnectionSession.objects.filter(whatsapp_account=self.account).count(), 1)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_connecting_after_a_qr_lapsed_starts_a_fresh_one(self, mock_start, mock_disconnect):
        # Reuse is for a QR that can still be scanned. A lapsed one is replaced
        # (and its provider session released), or Reconnect would hand back a
        # code that no longer works.
        mock_start.side_effect = [
            ConnectionSessionResult(
                status="QR_READY", session_id="sess-lapsed", qr_data="data:x",
                qr_expires_at=timezone.now() - timedelta(seconds=1),
            ),
            ConnectionSessionResult(
                status="QR_READY", session_id="sess-fresh", qr_data="data:y",
                qr_expires_at=timezone.now() + timedelta(seconds=120),
            ),
        ]
        ConnectionManager.start_connection(self.account)

        result = ConnectionManager.start_connection(self.account)

        self.assertEqual(result["session_id"], "sess-fresh")
        self.assertEqual(mock_start.call_count, 2)
        mock_disconnect.assert_called_once_with(self.account, "sess-lapsed")

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_reconnecting_releases_a_session_the_provider_still_holds(self, mock_start, mock_disconnect):
        # The mismatch behind three live links on one phone: the account read
        # EXPIRED while its provider session was still connected, the user
        # pressed Reconnect, and the surviving socket was never released.
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.side_effect = [
            ConnectionSessionResult(status="QR_READY", session_id="sess-held", qr_expires_at=expires_at),
            ConnectionSessionResult(status="QR_READY", session_id="sess-new", qr_expires_at=expires_at),
        ]
        ConnectionManager.start_connection(self.account)
        ConnectionManager.handle_connection_webhook(
            WhatsAppAccount.Provider.QR_PROVIDER,
            ConnectionWebhookEvent(session_id="sess-held", status="CONNECTED", phone_number="+15559990000"),
        )
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(status=WhatsAppAccount.Status.EXPIRED)
        self.account.refresh_from_db()

        result = ConnectionManager.start_connection(self.account)

        self.assertEqual(result["session_id"], "sess-new")
        mock_disconnect.assert_called_once_with(self.account, "sess-held")
        held = WhatsAppConnectionSession.objects.get(session_id="sess-held")
        self.assertEqual(held.status, WhatsAppConnectionSession.Status.DISCONNECTED)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_disconnect(self, mock_start, mock_disconnect):
        mock_start.return_value = ConnectionSessionResult(status="CONNECTED", phone_number="+15550100")
        ConnectionManager.start_connection(self.account)

        result = ConnectionManager.disconnect(self.account)

        self.assertTrue(result["success"])
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.DISCONNECTED)
        mock_disconnect.assert_called_once()

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_refresh_qr_disconnects_stale_provider_session(self, mock_start, mock_disconnect):
        # Regression test: refreshing a pending QR used to only mark the
        # Django-side session EXPIRED and start a brand new provider
        # session, leaking the old one (an orphaned Baileys socket on the
        # QR bridge that nobody would ever scan -- see qr_bridge.py).
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.side_effect = [
            ConnectionSessionResult(status="QR_READY", session_id="sess-old", qr_expires_at=expires_at),
            ConnectionSessionResult(status="QR_READY", session_id="sess-new", qr_expires_at=expires_at),
        ]
        ConnectionManager.start_connection(self.account)

        result = ConnectionManager.refresh_qr(self.account)

        self.assertTrue(result["success"])
        self.assertEqual(result["session_id"], "sess-new")
        mock_disconnect.assert_called_once_with(self.account, "sess-old")

        old_session = WhatsAppConnectionSession.objects.get(session_id="sess-old")
        self.assertEqual(old_session.status, WhatsAppConnectionSession.Status.EXPIRED)

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_refresh_qr_still_succeeds_if_old_session_disconnect_fails(self, mock_start, mock_disconnect):
        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.side_effect = [
            ConnectionSessionResult(status="QR_READY", session_id="sess-old", qr_expires_at=expires_at),
            ConnectionSessionResult(status="QR_READY", session_id="sess-new", qr_expires_at=expires_at),
        ]
        mock_disconnect.side_effect = ProviderError("boom", code="network_error")
        ConnectionManager.start_connection(self.account)

        result = ConnectionManager.refresh_qr(self.account)

        self.assertTrue(result["success"])
        self.assertEqual(result["session_id"], "sess-new")


class ExpireStaleConnectionTests(TestCase):
    """
    Abandoned pairing attempts must not stay pinned at QR_READY.

    Only the connection wizard polls get_status, so closing it without
    scanning left the account "in progress" forever -- the devices card then
    showed a permanent "Waiting for scan" spinner and offered neither
    Reconnect nor Delete, so the row could never be cleared.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-expire-stale", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-expire-stale-owner", password="x", tenant=self.tenant)
        self.account = _make_qr_account(self.tenant, self.user)
        self.accounts = WhatsAppAccount.objects.filter(tenant=self.tenant)

    def _start(self, qr_expires_at):
        with patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection") as mock_start:
            mock_start.return_value = ConnectionSessionResult(
                status="QR_READY", session_id="sess-1", qr_data="data:x", qr_expires_at=qr_expires_at,
            )
            ConnectionManager.start_connection(self.account)
        return WhatsAppConnectionSession.objects.get(whatsapp_account=self.account)

    def test_expires_qr_session_past_its_expiry(self):
        session = self._start(timezone.now() - timedelta(seconds=1))

        self.assertEqual(ConnectionManager.expire_stale(self.accounts), 1)

        self.account.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.EXPIRED)
        self.assertEqual(session.status, WhatsAppConnectionSession.Status.EXPIRED)

    def test_leaves_a_still_valid_qr_alone(self):
        session = self._start(timezone.now() + timedelta(seconds=120))

        self.assertEqual(ConnectionManager.expire_stale(self.accounts), 0)

        self.account.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.QR_READY)
        self.assertEqual(session.status, WhatsAppConnectionSession.Status.QR_READY)

    @override_settings(WHATSAPP_QR_SESSION_TTL_SECONDS=120)
    def test_ages_out_a_session_the_provider_gave_no_expiry_for(self):
        # Without qr_expires_at there is nothing to compare against, so the
        # session is aged out against the configured TTL instead.
        session = self._start(None)
        WhatsAppConnectionSession.objects.filter(pk=session.pk).update(
            created_at=timezone.now() - timedelta(seconds=300)
        )

        self.assertEqual(ConnectionManager.expire_stale(self.accounts), 1)

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.EXPIRED)

    @override_settings(WHATSAPP_QR_SESSION_TTL_SECONDS=99999999999999999999)
    def test_survives_an_effectively_infinite_ttl(self):
        # The shipped default is far too large for a timedelta; the age-out
        # fallback has to degrade to "never stale" rather than raise.
        session = self._start(None)
        WhatsAppConnectionSession.objects.filter(pk=session.pk).update(
            created_at=timezone.now() - timedelta(days=365)
        )

        self.assertEqual(ConnectionManager.expire_stale(self.accounts), 0)

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.QR_READY)

    def test_ignores_accounts_that_are_not_mid_connection(self):
        self._start(timezone.now() - timedelta(seconds=1))
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(status=WhatsAppAccount.Status.CONNECTED)

        self.assertEqual(ConnectionManager.expire_stale(self.accounts), 0)

        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)


class WhatsAppAccountApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-api", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-api", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-api-owner", password="x", tenant=self.tenant_a)
        self.user_b = get_user_model().objects.create_user(username="globex-api-owner", password="x", tenant=self.tenant_b)
        self.account_a = _make_account(self.tenant_a, self.user_a, name="Acme Line")
        self.account_b = _make_account(self.tenant_b, self.user_b, name="Globex Line")

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_list_only_returns_own_tenant_accounts(self):
        response = self.client.get("/api/v1/whatsapp/accounts/")

        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["results"]}
        self.assertEqual(names, {"Acme Line"})

    def test_listing_devices_expires_abandoned_pairing_attempts(self):
        # The devices page is the only thing that ever looks at these rows
        # again, so listing is where a never-scanned QR gets reconciled --
        # otherwise the card stays on "Waiting for scan" with no Delete.
        stale = _make_qr_account(self.tenant_a, self.user_a, name="Never Scanned")
        with patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection") as mock_start:
            mock_start.return_value = ConnectionSessionResult(
                status="QR_READY", session_id="sess-stale", qr_data="data:x",
                qr_expires_at=timezone.now() - timedelta(seconds=1),
            )
            ConnectionManager.start_connection(stale)

        response = self.client.get("/api/v1/whatsapp/accounts/")

        self.assertEqual(response.status_code, 200)
        statuses = {item["name"]: item["status"] for item in response.json()["results"]}
        self.assertEqual(statuses["Never Scanned"], WhatsAppAccount.Status.EXPIRED)

    def test_deleting_a_device_with_history_archives_it_instead_of_refusing(self):
        # Campaign/Message hold PROTECT references, so the row has to survive
        # for the reports that join through it -- but the user's delete still
        # goes through: the device is disconnected, archived, and gone from
        # every device query.
        from apps.campaigns.models import Campaign

        campaign = Campaign.objects.create(
            tenant=self.tenant_a, created_by=self.user_a, whatsapp_account=self.account_a,
            name="Winter promo", message_body="hi",
        )

        with patch("apps.whatsapp.services.WhatsAppConnectionService.disconnect") as mock_disconnect:
            response = self.client.delete(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/")

        self.assertEqual(response.status_code, 204)
        mock_disconnect.assert_called_once()
        self.assertFalse(WhatsAppAccount.objects.filter(pk=self.account_a.pk).exists())
        archived = WhatsAppAccount.all_objects.get(pk=self.account_a.pk)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.status, WhatsAppAccount.Status.DISCONNECTED)
        # The history it was kept for still resolves.
        campaign.refresh_from_db()
        self.assertEqual(campaign.whatsapp_account_id, archived.pk)

    def test_an_archived_device_is_gone_from_the_list_and_frees_its_plan_slot(self):
        from apps.messaging.models import Message

        Message.objects.create(
            tenant=self.tenant_a, whatsapp_account=self.account_a, to_phone="+15551230009",
            status=Message.Status.SENT,
        )

        with patch("apps.whatsapp.services.WhatsAppConnectionService.disconnect"):
            self.client.delete(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/")

        listed = self.client.get("/api/v1/whatsapp/accounts/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        self.assertNotIn(self.account_a.pk, [row["id"] for row in rows])
        # `objects` is the default manager, so the plan's device count (and
        # every picker) stops seeing it too.
        self.assertEqual(WhatsAppAccount.objects.filter(tenant=self.tenant_a).count(), 0)
        # ...and it can never be fetched or reconnected through the API again.
        self.assertEqual(
            self.client.get(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/").status_code, 404
        )

    def test_deleting_a_device_without_history_succeeds(self):
        with patch("apps.whatsapp.services.WhatsAppConnectionService.disconnect") as mock_disconnect:
            response = self.client.delete(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/")

        self.assertEqual(response.status_code, 204)
        mock_disconnect.assert_called_once()
        self.assertFalse(WhatsAppAccount.objects.filter(pk=self.account_a.pk).exists())

    def test_cannot_fetch_another_tenants_account(self):
        response = self.client.get(f"/api/v1/whatsapp/accounts/{self.account_b.pk}/")

        self.assertEqual(response.status_code, 404)

    def test_access_token_is_never_returned_by_the_api(self):
        response = self.client.get(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access_token", response.json())
        self.assertNotIn("access_token_encrypted", response.json())
        self.assertTrue(response.json()["has_access_token"])

    def test_create_whatsapp_account_requires_qr_connection_method(self):
        response = self.client.post(
            "/api/v1/whatsapp/accounts/",
            {"name": "Sales WhatsApp", "account_type": "WHATSAPP", "connection_method": "CLOUD_API"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("connection_method", response.json())

    def test_create_whatsapp_account_via_qr_succeeds(self):
        response = self.client.post(
            "/api/v1/whatsapp/accounts/",
            {"name": "Sales WhatsApp", "account_type": "WHATSAPP", "connection_method": "QR"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["provider"], "QR_PROVIDER")
        self.assertEqual(response.json()["status"], "DISCONNECTED")

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    def test_delete_account_tears_down_provider_session_first(self, mock_disconnect):
        # Regression test: deleting an account used to skip provider
        # teardown entirely (default ModelViewSet.destroy just deletes the
        # row), leaking the same kind of orphaned Baileys session on the QR
        # bridge that refresh_qr used to leak -- see perform_destroy in
        # views.py and ConnectionManager.refresh_qr.
        account = _make_account(
            self.tenant_a,
            self.user_a,
            name="QR Line",
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            provider=WhatsAppAccount.Provider.QR_PROVIDER,
            provider_session_id="sess-to-clean-up",
        )
        account_pk = account.pk

        # perform_destroy calls disconnect(instance) before instance.delete()
        # -- and delete() nulls out instance.pk on that same (shared, not
        # copied) object afterwards. Snapshot pk/session_id as seen *at call
        # time* via side_effect rather than inspecting call_args after the
        # request has finished, or this would spuriously see pk=None.
        seen = {}

        def _record(acc, session_id):
            seen["pk"] = acc.pk
            seen["session_id"] = session_id

        mock_disconnect.side_effect = _record

        response = self.client.delete(f"/api/v1/whatsapp/accounts/{account_pk}/")

        self.assertEqual(response.status_code, 204)
        mock_disconnect.assert_called_once()
        self.assertEqual(seen["pk"], account_pk)
        self.assertEqual(seen["session_id"], "sess-to-clean-up")
        self.assertFalse(WhatsAppAccount.objects.filter(pk=account_pk).exists())

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.disconnect")
    def test_delete_account_still_succeeds_if_provider_disconnect_fails(self, mock_disconnect):
        mock_disconnect.side_effect = ProviderError("boom", code="network_error")
        account = _make_account(
            self.tenant_a,
            self.user_a,
            name="QR Line",
            connection_method=WhatsAppAccount.ConnectionMethod.QR,
            provider=WhatsAppAccount.Provider.QR_PROVIDER,
            provider_session_id="sess-to-clean-up",
        )

        response = self.client.delete(f"/api/v1/whatsapp/accounts/{account.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(WhatsAppAccount.objects.filter(pk=account.pk).exists())

    def test_create_business_app_account_via_qr_succeeds(self):
        response = self.client.post(
            "/api/v1/whatsapp/accounts/",
            {"name": "Support Business", "account_type": "BUSINESS", "connection_method": "QR"},
        )

        self.assertEqual(response.status_code, 201)

    def test_create_api_account_requires_credentials(self):
        response = self.client.post(
            "/api/v1/whatsapp/accounts/",
            {"name": "Marketing API", "account_type": "API", "connection_method": "CLOUD_API"},
        )

        self.assertEqual(response.status_code, 400)

    def test_create_api_account_with_credentials_succeeds(self):
        response = self.client.post(
            "/api/v1/whatsapp/accounts/",
            {
                "name": "Marketing API",
                "account_type": "API",
                "connection_method": "CLOUD_API",
                "phone_number_id": "999",
                "access_token": "tok",
                "app_secret": "sec",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["provider"], "CLOUD_API")

    @patch("apps.whatsapp.services.ConnectionManager.start_connection")
    def test_connect_action_invokes_connection_manager(self, mock_start):
        mock_start.return_value = {"success": True, "status": "CONNECTED", "message": "ok", "session_id": None, "expires_at": None}

        response = self.client.post(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/connect/")

        self.assertEqual(response.status_code, 200)
        mock_start.assert_called_once_with(self.account_a)

    @patch("apps.whatsapp.providers.cloud_api.WhatsAppCloudAPIProvider.get_account_info")
    def test_test_api_action_validates_without_connecting(self, mock_info):
        mock_info.return_value = ConnectionResult(connected=True, phone_number="+15550100")

        response = self.client.post(f"/api/v1/whatsapp/accounts/{self.account_a.pk}/test-api/")

        self.assertEqual(response.status_code, 200)
        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.status, WhatsAppAccount.Status.DISCONNECTED)

    def test_test_api_action_rejected_for_qr_accounts(self):
        qr_account = _make_qr_account(self.tenant_a, self.user_a, name="QR Line")

        response = self.client.post(f"/api/v1/whatsapp/accounts/{qr_account.pk}/test-api/")

        self.assertEqual(response.status_code, 400)


class WhatsAppWebhookApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-webhook", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-webhook-owner", password="x", tenant=self.tenant)
        self.account = _make_account(self.tenant, self.user)
        self.client = APIClient()

    def test_get_verification_handshake(self):
        response = self.client.get(
            f"/api/v1/whatsapp/webhook/{self.account.pk}/",
            {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "999"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"999")

    def test_get_verification_handshake_rejects_bad_token(self):
        response = self.client.get(
            f"/api/v1/whatsapp/webhook/{self.account.pk}/",
            {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "999"},
        )

        self.assertEqual(response.status_code, 403)

    def _signed_post(self, payload_bytes):
        import hashlib
        import hmac

        signature = "sha256=" + hmac.new(b"test-app-secret", payload_bytes, hashlib.sha256).hexdigest()
        return self.client.post(
            f"/api/v1/whatsapp/webhook/{self.account.pk}/",
            data=payload_bytes,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=signature,
        )

    def test_post_rejects_invalid_signature(self):
        response = self.client.post(
            f"/api/v1/whatsapp/webhook/{self.account.pk}/",
            data=b'{"entry": []}',
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsAppWebhookEvent.objects.count(), 0)

    def test_post_stores_event_idempotently(self):
        payload = b'{"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]}}]}]}'

        first = self._signed_post(payload)
        second = self._signed_post(payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(WhatsAppWebhookEvent.objects.filter(account=self.account).count(), 1)

    def test_stored_event_type_matches_the_models_choices(self):
        """
        Storing the event is not enough -- every receiver dispatches on
        event_type, so a value outside EventType silently disables all of them.
        """
        self._signed_post(b'{"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]}}]}]}')
        self._signed_post(b'{"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.2", "from": "15551234567", "type": "text", "text": {"body": "hi"}}]}}]}]}')

        stored = dict(WhatsAppWebhookEvent.objects.values_list("provider_event_id", "event_type"))
        self.assertEqual(stored["wamid.1:delivered"], WhatsAppWebhookEvent.EventType.MESSAGE_STATUS)
        self.assertEqual(stored["wamid.2"], WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE)

    def test_posting_an_inbound_message_creates_a_conversation(self):
        """The whole point of the endpoint: a real POST has to reach the inbox."""
        payload = b'{"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.3", "from": "15551234567", "type": "text", "text": {"body": "Hello"}}], "contacts": [{"wa_id": "15551234567", "profile": {"name": "Priya"}}]}}]}]}'

        response = self._signed_post(payload)

        self.assertEqual(response.status_code, 200)
        conversation = Conversation.objects.get(whatsapp_account=self.account)
        self.assertEqual(conversation.contact.phone, "+15551234567")
        message = conversation.messages.get()
        self.assertEqual(message.body, "Hello")
        self.assertEqual(message.direction, Message.Direction.INBOUND)

    def test_posting_a_delivery_receipt_updates_the_message(self):
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, to_phone="+15551234567",
            direction=Message.Direction.OUTBOUND, message_type=Message.MessageType.TEXT, body="hi",
            status=Message.Status.SENT, provider_message_id="wamid.9",
        )

        response = self._signed_post(b'{"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.9", "status": "delivered"}]}}]}]}')

        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.DELIVERED)
        self.assertIsNotNone(message.delivered_at)


class WebhookEventTypeConstantsTests(TestCase):
    def test_provider_constants_match_the_model_choices(self):
        """
        The providers write these strings into the model field that the
        receivers dispatch on, so the two definitions must not drift apart.
        """
        self.assertEqual(WebhookEventType.MESSAGE_STATUS, WhatsAppWebhookEvent.EventType.MESSAGE_STATUS)
        self.assertEqual(WebhookEventType.INBOUND_MESSAGE, WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE)


class WhatsAppConnectionWebhookApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-conn-webhook", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-conn-webhook-owner", password="x", tenant=self.tenant)
        self.account = _make_qr_account(self.tenant, self.user)
        self.client = APIClient()

    @patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.start_connection")
    def test_valid_webhook_marks_account_connected(self, mock_start):
        import hashlib
        import hmac

        expires_at = timezone.now() + timedelta(seconds=120)
        mock_start.return_value = ConnectionSessionResult(
            status="QR_READY", session_id="sess-1", qr_data="data:x", qr_expires_at=expires_at,
        )
        ConnectionManager.start_connection(self.account)

        with patch("apps.whatsapp.providers.qr_bridge.NotConfiguredProvider.parse_connection_webhook") as mock_parse:
            mock_parse.return_value = ConnectionWebhookEvent(session_id="sess-1", status="CONNECTED", phone_number="+15550100")
            response = self.client.post(
                "/api/v1/whatsapp/webhooks/qr_provider/",
                data=b'{"session_id": "sess-1", "status": "CONNECTED"}',
                content_type="application/json",
                HTTP_X_BRIDGE_SIGNATURE="sha256=irrelevant-because-mocked",
            )

        self.assertEqual(response.status_code, 200)
        self.account.refresh_from_db()
        self.assertEqual(self.account.status, WhatsAppAccount.Status.CONNECTED)

    def test_unknown_provider_returns_404(self):
        response = self.client.post(
            "/api/v1/whatsapp/webhooks/not_a_real_provider/",
            data=b"{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class WhatsAppAccountBulkActionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-device-bulk", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-device-bulk-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _bulk(self, action, ids):
        return self.client.post("/api/v1/whatsapp/accounts/bulk/", {"action": action, "ids": ids}, format="json")

    @patch("apps.whatsapp.views.WhatsAppConnectionService.disconnect")
    def test_bulk_delete_removes_devices_with_no_send_history(self, mock_disconnect):
        first = _make_account(self.tenant, self.user, name="One")
        second = _make_account(self.tenant, self.user, name="Two")

        response = self._bulk("delete", [first.pk, second.pk])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual(WhatsAppAccount.objects.count(), 0)
        self.assertEqual(mock_disconnect.call_count, 2)

    @patch("apps.whatsapp.views.WhatsAppConnectionService.disconnect")
    def test_a_device_with_history_is_archived_and_the_batch_still_succeeds(self, mock_disconnect):
        clean = _make_account(self.tenant, self.user, name="Clean")
        used = _make_account(self.tenant, self.user, name="Used")
        Message.objects.create(
            tenant=self.tenant, whatsapp_account=used, to_phone="+15551230001", status=Message.Status.SENT,
        )

        response = self._bulk("delete", [clean.pk, used.pk])

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["succeeded"], body["failed"]), (2, 0))
        # Both are gone from the tenant's devices; only the used one is still
        # a row, archived, so its message keeps a device to report against.
        self.assertEqual(WhatsAppAccount.objects.count(), 0)
        self.assertFalse(WhatsAppAccount.all_objects.filter(pk=clean.pk).exists())
        self.assertIsNotNone(WhatsAppAccount.all_objects.get(pk=used.pk).archived_at)
        self.assertEqual(mock_disconnect.call_count, 2)

    @patch("apps.whatsapp.views.WhatsAppConnectionService.disconnect")
    def test_bulk_disconnect_leaves_the_devices_in_place(self, mock_disconnect):
        account = _make_account(self.tenant, self.user, name="Live")

        response = self._bulk("disconnect", [account.pk])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["detail"], "1 device disconnected.")
        self.assertTrue(WhatsAppAccount.objects.filter(pk=account.pk).exists())
        mock_disconnect.assert_called_once()


class MediaLinkRewritingTests(TestCase):
    """
    Baileys fetches an attachment from inside the qr-bridge container, so the
    browser-facing link has to be swapped for one that resolves there.
    """

    def test_rewrites_the_public_origin_for_the_bridge(self):
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost", MEDIA_INTERNAL_BASE_URL="http://nginx"
        ):
            self.assertEqual(
                _reachable_link("http://localhost/media/message_media/x.png"),
                "http://nginx/media/message_media/x.png",
            )

    def test_leaves_links_from_elsewhere_alone(self):
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost", MEDIA_INTERNAL_BASE_URL="http://nginx"
        ):
            self.assertEqual(
                _reachable_link("https://cdn.example.com/x.png"), "https://cdn.example.com/x.png"
            )

    def test_is_a_no_op_when_unconfigured(self):
        from .providers.qr_bridge import _reachable_link

        with override_settings(MEDIA_PUBLIC_BASE_URL="", MEDIA_INTERNAL_BASE_URL=""):
            self.assertEqual(
                _reachable_link("http://localhost/media/x.png"), "http://localhost/media/x.png"
            )

    def test_rewrites_a_link_frozen_with_a_dev_server_origin(self):
        """
        The regression this exists for.

        An attachment uploaded through a Django run outside Docker is stored
        with "http://127.0.0.1:8000" baked into the template and into every
        message copied from it. That origin is not MEDIA_PUBLIC_BASE_URL, so a
        literal prefix match handed it to the bridge untouched -- where
        127.0.0.1 is the bridge itself and the send failed with
        "connect ECONNREFUSED 127.0.0.1:8000" after burning every retry.
        """
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["localhost"],
        ):
            self.assertEqual(
                _reachable_link("http://127.0.0.1:8000/media/message_media/tenant_1/x.jpeg"),
                "http://nginx/media/message_media/tenant_1/x.jpeg",
            )

    def test_rewrites_a_link_carrying_a_previous_domain(self):
        """A domain the deployment still answers for names the same file."""
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="https://new.example.com",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["new.example.com", ".old.example.com"],
        ):
            self.assertEqual(
                _reachable_link("https://old.example.com/media/message_media/x.png"),
                "http://nginx/media/message_media/x.png",
            )

    def test_leaves_an_external_host_alone_even_under_the_media_path(self):
        """
        Ours is decided by the host, not by the path. Someone else's
        /media/ directory is not ours to redirect at our own nginx.
        """
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["localhost"],
        ):
            self.assertEqual(
                _reachable_link("https://cdn.example.com/media/x.png"),
                "https://cdn.example.com/media/x.png",
            )

    def test_a_wildcard_allowed_host_does_not_claim_the_whole_internet(self):
        """
        ALLOWED_HOSTS=["*"] is normal in development. Reading it as "every
        host is ours" would rewrite a customer's own CDN link into a 404.
        """
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["*"],
        ):
            self.assertEqual(
                _reachable_link("https://cdn.example.com/media/x.png"),
                "https://cdn.example.com/media/x.png",
            )

    def test_keeps_the_query_string(self):
        """A signed or versioned link loses its meaning without the query."""
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["localhost"],
        ):
            self.assertEqual(
                _reachable_link("http://localhost/media/x.png?v=2"),
                "http://nginx/media/x.png?v=2",
            )

    def test_leaves_a_non_media_path_on_our_own_host_alone(self):
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost",
            MEDIA_INTERNAL_BASE_URL="http://nginx",
            ALLOWED_HOSTS=["localhost"],
        ):
            self.assertEqual(
                _reachable_link("http://localhost/static/x.png"),
                "http://localhost/static/x.png",
            )

    def test_leaves_a_data_url_alone(self):
        from .providers.qr_bridge import _reachable_link

        with override_settings(
            MEDIA_PUBLIC_BASE_URL="http://localhost", MEDIA_INTERNAL_BASE_URL="http://nginx"
        ):
            self.assertEqual(_reachable_link("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA")


class CloudAPIInteractiveMessageTests(TestCase):
    """
    Reply buttons and a link button are two different Cloud API interactive
    types, not two shapes of one payload -- so a message carrying both cannot
    put both on the wire as buttons.

    It is still accepted, because refusing it helps nobody: ``plan_delivery``
    keeps one kind tappable and writes the rest into the body, and these tests
    pin *which* kind survives. Getting that backwards would drop the taps
    automation is matched on.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-interactive", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-interactive-owner", password="x", tenant=self.tenant
        )
        self.account = _make_account(self.tenant, self.user)
        self.provider = WhatsAppCloudAPIProvider()

    def test_the_provider_advertises_interactive_support(self):
        self.assertTrue(self.provider.supports_interactive_buttons)

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_reply_buttons_are_sent_as_an_interactive_button_message(self, mock_request):
        mock_request.return_value = Mock(ok=True, json=lambda: {"messages": [{"id": "wamid.i1"}]})

        result = self.provider.send_interactive_message(
            self.account, "15551234567", "Coming on Friday?",
            [{"type": "REPLY", "id": "yes", "title": "Yes", "url": ""},
             {"type": "REPLY", "id": "no", "title": "No", "url": ""}],
        )

        self.assertTrue(result.success)
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "interactive")
        self.assertEqual(payload["interactive"]["type"], "button")
        self.assertEqual(payload["interactive"]["body"]["text"], "Coming on Friday?")
        self.assertEqual(
            payload["interactive"]["action"]["buttons"],
            [
                {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "no", "title": "No"}},
            ],
        )

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_a_link_button_is_sent_as_a_call_to_action_message(self, mock_request):
        mock_request.return_value = Mock(ok=True, json=lambda: {"messages": [{"id": "wamid.i2"}]})

        result = self.provider.send_interactive_message(
            self.account, "15551234567", "New arrivals are up",
            [{"type": "URL", "id": "", "title": "Shop now", "url": "https://example.test/shop"}],
        )

        self.assertTrue(result.success)
        interactive = mock_request.call_args.kwargs["json"]["interactive"]
        self.assertEqual(interactive["type"], "cta_url")
        self.assertEqual(
            interactive["action"]["parameters"],
            {"display_text": "Shop now", "url": "https://example.test/shop"},
        )

    def test_no_buttons_fails_without_calling_the_provider(self):
        result = self.provider.send_interactive_message(self.account, "15551234567", "Hi", [])

        self.assertFalse(result.success)

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_a_mixed_list_keeps_the_reply_buttons_and_spells_out_the_rest(self, mock_request):
        mock_request.return_value = Mock(ok=True, json=lambda: {"messages": [{"id": "wamid.i3"}]})

        result = self.provider.send_interactive_message(
            self.account, "15551234567", "Coming on Friday?",
            [
                {"type": "REPLY", "id": "yes", "title": "Yes", "url": "", "phone": ""},
                {"type": "URL", "id": "", "title": "Directions", "url": "https://example.test/map", "phone": ""},
                {"type": "CALL", "id": "", "title": "Call us", "url": "", "phone": "+919876543210"},
            ],
        )

        self.assertTrue(result.success)
        interactive = mock_request.call_args.kwargs["json"]["interactive"]
        # The reply button keeps its tap: it is the only kind an answer comes
        # back from, so losing it would unhook the automation behind it.
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(
            interactive["action"]["buttons"],
            [{"type": "reply", "reply": {"id": "yes", "title": "Yes"}}],
        )
        self.assertEqual(
            interactive["body"]["text"],
            "Coming on Friday?\n\nDirections: https://example.test/map\nCall us: +919876543210",
        )

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_a_link_beside_a_call_button_still_takes_the_cta_envelope(self, mock_request):
        mock_request.return_value = Mock(ok=True, json=lambda: {"messages": [{"id": "wamid.i4"}]})

        result = self.provider.send_interactive_message(
            self.account, "15551234567", "New arrivals are up",
            [
                {"type": "URL", "id": "", "title": "Shop now", "url": "https://example.test/shop", "phone": ""},
                {"type": "CALL", "id": "", "title": "Call us", "url": "", "phone": "+919876543210"},
            ],
        )

        self.assertTrue(result.success)
        interactive = mock_request.call_args.kwargs["json"]["interactive"]
        self.assertEqual(interactive["type"], "cta_url")
        self.assertEqual(
            interactive["body"]["text"], "New arrivals are up\n\nCall us: +919876543210"
        )

    @patch("apps.whatsapp.providers.cloud_api.requests.Session.request")
    def test_a_call_button_alone_is_sent_as_text_rather_than_refused(self, mock_request):
        """
        There is no interactive envelope a call button fits in, and an
        interactive message with an empty action is a 400 from Meta. Sending
        the labelled number is what a call button was always going to arrive
        as -- failing the send instead would lose the whole message.
        """
        mock_request.return_value = Mock(ok=True, json=lambda: {"messages": [{"id": "wamid.i5"}]})

        result = self.provider.send_interactive_message(
            self.account, "15551234567", "Need help?",
            [{"type": "CALL", "id": "", "title": "Call now", "url": "", "phone": "+919876543210"}],
        )

        self.assertTrue(result.success)
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Need help?\n\nCall now: +919876543210")


class QRBridgeInteractiveMessageTests(TestCase):
    """
    QR/session accounts send buttons too -- the bridge relays them as a
    native-flow interactive message. Without this every button message on a
    QR account silently arrived as plain text.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-qr-interactive", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-qr-interactive-owner", password="x", tenant=self.tenant
        )
        self.account = _make_qr_account(self.tenant, self.user)
        self.provider = WhatsAppQRBridgeProvider(
            base_url="https://bridge.test",
            api_key="test-key",
            webhook_secret="test-webhook-secret",
        )

    def test_the_bridge_does_not_advertise_interactive_support(self):
        # The relay below works and is kept, but WhatsApp reserves interactive
        # messages for the Cloud API: a linked device can put one on the wire,
        # get an id back, and have the recipient's phone render the body with
        # no buttons under it. Measured live: 15 sent, zero taps ever.
        #
        # Advertising support we do not have is what suppressed the numbered
        # fallback, so the flag states what actually reaches a phone. Flipping
        # it back is the whole change if Meta ever opens this up.
        self.assertFalse(self.provider.supports_interactive_buttons)

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_buttons_are_relayed_to_the_bridge(self, mock_request):
        self.account.provider_session_id = "sess-1"
        mock_request.return_value = Mock(
            ok=True, status_code=200, json=lambda: {"success": True, "provider_message_id": "BAE5.1"}
        )
        buttons = [
            {"type": "REPLY", "id": "interested", "title": "Interested", "url": ""},
            {"type": "REPLY", "id": "not_now", "title": "Not now", "url": ""},
        ]

        result = self.provider.send_interactive_message(self.account, "15551234567", "hello", buttons)

        self.assertTrue(result.success)
        payload = mock_request.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "interactive")
        self.assertEqual(payload["interactive"], {"body": "hello", "buttons": buttons})

    def test_no_buttons_fails_without_calling_the_bridge(self):
        result = self.provider.send_interactive_message(self.account, "15551234567", "hello", [])

        self.assertFalse(result.success)


class QRBridgeButtonReplyPayloadTests(TestCase):
    """
    The bridge flattens every shape of tapped button into one `button_id`;
    it must come back out as the Cloud API interactive reply apps.inbox reads.
    """

    def setUp(self):
        self.provider = WhatsAppQRBridgeProvider(
            base_url="https://bridge.test",
            api_key="test-key",
            webhook_secret="test-webhook-secret",
        )

    def test_a_tapped_button_becomes_an_interactive_reply(self):
        events = self.provider.parse_webhook_payload({
            "messages": [{
                "id": "BAE5.in", "from": "15551234567", "type": "interactive",
                "body": "Interested", "button_id": "interested", "push_name": "Priya",
            }]
        })

        self.assertEqual(len(events), 1)
        message = events[0].raw["message"]
        self.assertEqual(message["type"], "interactive")
        self.assertEqual(
            message["interactive"],
            {"type": "button_reply", "button_reply": {"id": "interested", "title": "Interested"}},
        )

    def test_a_plain_text_message_is_unchanged(self):
        events = self.provider.parse_webhook_payload({
            "messages": [{"id": "BAE5.in2", "from": "15551234567", "type": "text", "body": "hi"}]
        })

        message = events[0].raw["message"]
        self.assertEqual(message["type"], "text")
        self.assertEqual(message["text"], {"body": "hi"})
