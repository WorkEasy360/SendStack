"""
The provider layer's lookups: number registration and group membership.

Only the QR bridge can answer either -- a linked device runs the same
contact-sync query the app itself runs, and sees the groups the phone is in
-- and the Cloud API has no equivalent at any version. The capability flags
say so, the Cloud API provider refuses by name, and the bridge adapter maps
the HTTP contract in qr_bridge.py's docblock to dataclasses without ever
letting a phone number lose its "+".
"""

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.tenants.models import Tenant

from .connections.manager import ConnectionManager
from .models import WhatsAppAccount
from .providers.base import ProviderError
from .providers.cloud_api import WhatsAppCloudAPIProvider
from .providers.qr_bridge import NotConfiguredProvider, WhatsAppQRBridgeProvider


def _response(payload, status_code=200):
    return Mock(status_code=status_code, ok=status_code < 400, json=Mock(return_value=payload))


class QRBridgeLookupTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Lookups", slug="lookups-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="lookups-owner", password="x", tenant=self.tenant)
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="QR",
            connection_method=WhatsAppAccount.ConnectionMethod.QR, status=WhatsAppAccount.Status.CONNECTED,
            provider_session_id="lookups-1",
        )
        self.provider = WhatsAppQRBridgeProvider(base_url="http://bridge.invalid", api_key="k", webhook_secret="s")

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_check_numbers_asks_by_digits_and_answers_every_input_in_order(self, request):
        request.return_value = _response({"results": [
            {"number": "15550000001", "exists": True, "jid": "15550000001@s.whatsapp.net"},
        ]})

        lookups = self.provider.check_numbers(self.account, ["+15550000001", "+1 555 000 0002", ""])

        self.assertEqual(
            [(lookup.phone, lookup.exists, lookup.jid) for lookup in lookups],
            [
                ("+15550000001", True, "15550000001@s.whatsapp.net"),
                ("+1 555 000 0002", False, None),
                # Nothing to ask about is answered, not raised: the caller
                # reports one row per input.
                ("", False, None),
            ],
        )
        self.assertEqual(request.call_args.args[0], "POST")
        self.assertTrue(request.call_args.args[1].endswith("/sessions/lookups-1/numbers/check"))
        self.assertEqual(request.call_args.kwargs["json"], {"numbers": ["15550000001", "15550000002"]})

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_check_numbers_splits_above_the_bridge_cap(self, request):
        request.return_value = _response({"results": []})
        with patch.object(WhatsAppQRBridgeProvider, "NUMBER_CHECK_BATCH", 2):
            lookups = self.provider.check_numbers(
                self.account, ["+15550000001", "+15550000002", "+15550000003"],
            )
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(lookups), 3)

    def test_check_numbers_needs_a_session(self):
        self.account.provider_session_id = None
        with self.assertRaises(ProviderError) as caught:
            self.provider.check_numbers(self.account, ["+15550000001"])
        self.assertEqual(caught.exception.code, "no_session")

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_a_bridge_refusal_carries_its_own_code(self, request):
        request.return_value = _response({"error": "This session is not connected.", "code": "not_connected"}, 409)
        with self.assertRaises(ProviderError) as caught:
            self.provider.check_numbers(self.account, ["+15550000001"])
        self.assertEqual(caught.exception.code, "not_connected")
        self.assertEqual(caught.exception.message, "This session is not connected.")

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_list_groups_parses_the_bridge_payload_and_skips_rows_without_an_id(self, request):
        request.return_value = _response({"groups": [
            {
                "id": "1@g.us", "subject": "Traders", "member_count": 12,
                "announce": True, "is_community": False, "created_at": "2026-01-02T03:04:05+00:00",
            },
            {"subject": "no id"},
        ]})

        groups = self.provider.list_groups(self.account)

        self.assertEqual(len(groups), 1)
        self.assertEqual((groups[0].id, groups[0].subject, groups[0].member_count), ("1@g.us", "Traders", 12))
        self.assertTrue(groups[0].announce)
        self.assertEqual(groups[0].created_at.year, 2026)
        self.assertTrue(request.call_args.args[1].endswith("/sessions/lookups-1/groups"))

    @patch("apps.whatsapp.providers.qr_bridge.requests.request")
    def test_group_members_come_back_in_e164(self, request):
        request.return_value = _response({"groups": [{
            "id": "1@g.us", "subject": "Traders", "hidden_count": 2,
            "members": [
                {"phone": "15550000001", "jid": "15550000001@s.whatsapp.net", "name": "Asha", "is_admin": True},
                {"phone": "", "jid": ""},
            ],
        }]})

        groups = self.provider.get_group_members(self.account, ["1@g.us"])

        self.assertEqual(request.call_args.kwargs["json"], {"group_ids": ["1@g.us"]})
        self.assertEqual(groups[0].hidden_count, 2)
        self.assertEqual(
            [(member.phone, member.name, member.is_admin) for member in groups[0].members],
            [("+15550000001", "Asha", True)],
        )


class LookupCapabilityTests(TestCase):
    def test_only_the_qr_bridge_can_look_numbers_and_groups_up(self):
        qr = WhatsAppQRBridgeProvider.capabilities()
        cloud = WhatsAppCloudAPIProvider.capabilities()
        unconfigured = NotConfiguredProvider.capabilities()

        self.assertTrue(qr["number_check"])
        self.assertTrue(qr["groups"])
        self.assertFalse(cloud["number_check"])
        self.assertFalse(cloud["groups"])
        self.assertFalse(unconfigured["number_check"])
        self.assertFalse(unconfigured["groups"])

    def test_the_cloud_api_refuses_lookups_by_name(self):
        for call in (
            lambda provider: provider.check_numbers(None, ["+15550000001"]),
            lambda provider: provider.list_groups(None),
            lambda provider: provider.get_group_members(None, ["1@g.us"]),
        ):
            with self.assertRaises(ProviderError) as caught:
                call(WhatsAppCloudAPIProvider())
            self.assertEqual(caught.exception.code, "not_supported")


class ConnectionManagerLookupTests(TestCase):
    def test_a_disconnected_number_is_refused_before_the_provider_is_asked(self):
        tenant = Tenant.objects.create(name="Mgr", slug="mgr-lookups", kind=Tenant.Kind.CUSTOMER)
        user = get_user_model().objects.create_user(username="mgr-lookups", password="x", tenant=tenant)
        account = WhatsAppAccount.objects.create(
            tenant=tenant, user=user, name="QR", connection_method=WhatsAppAccount.ConnectionMethod.QR,
            status=WhatsAppAccount.Status.DISCONNECTED, provider_session_id="mgr-1",
        )
        with patch("apps.whatsapp.connections.manager.get_provider") as get_provider:
            with self.assertRaises(ProviderError) as caught:
                ConnectionManager.check_numbers(account, ["+15550000001"])
            with self.assertRaises(ProviderError):
                ConnectionManager.list_groups(account)
            with self.assertRaises(ProviderError):
                ConnectionManager.get_group_members(account, ["1@g.us"])
        self.assertEqual(caught.exception.code, "not_connected")
        get_provider.assert_not_called()
