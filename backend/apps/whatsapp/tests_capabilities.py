"""
The provider capability layer.

The matrix these tests pin is not symmetric, and that asymmetry is the whole
reason the layer exists:

    Cloud API   text, media, reply buttons, link buttons, lists     no polls
    QR bridge   text, media, polls                                  nothing else

Neither connection is simply "the better one". The official API cannot send a
poll at all; a linked device cannot render a button. A composer that assumed
either direction would be wrong half the time, which is why every screen asks
this layer instead of inferring from the provider name.

Each flag states what **reaches a phone**, not what the API accepts -- see
`WhatsAppQRBridgeProvider.supports_interactive_buttons` for the measurement
behind the most surprising False in the table.
"""

from contextlib import contextmanager
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from .connections.manager import ConnectionManager
from .models import WhatsAppAccount
from .providers.cloud_api import WhatsAppCloudAPIProvider
from .providers.qr_bridge import NotConfiguredProvider, WhatsAppQRBridgeProvider
from .providers.registry import provider_capabilities


@contextmanager
def configured_qr_provider():
    """
    Run a block with a *configured* QR bridge in the registry.

    Under test settings `QR_PROVIDER_BASE_URL` is deliberately blank, so the
    registry holds `NotConfiguredProvider` and every capability reads False --
    correct for a deployment with no bridge, and useless for testing what a
    deployment with one can do. This swaps in the real provider class without
    letting anything actually call it: the capability flags are class
    attributes, so no request is ever made.

    Shared with the templates, campaigns and messaging suites, which all need
    a number that genuinely cannot send buttons but genuinely can send polls.
    """
    provider = WhatsAppQRBridgeProvider(
        base_url="http://qr-bridge.invalid", api_key="k", webhook_secret="s",
    )
    with patch.dict(
        "apps.whatsapp.providers.registry._PROVIDERS", {"QR_PROVIDER": provider},
    ):
        yield provider


class ProviderCapabilityMatrixTests(TestCase):
    def test_cloud_api_sends_buttons_and_lists_but_not_polls(self):
        capabilities = WhatsAppCloudAPIProvider.capabilities()

        self.assertTrue(capabilities["buttons"])
        self.assertTrue(capabilities["url_buttons"])
        self.assertTrue(capabilities["lists"])
        # There is no poll message type in the Graph API at any version.
        self.assertFalse(capabilities["polls"])

    def test_the_qr_bridge_sends_polls_but_no_interactive_message(self):
        capabilities = WhatsAppQRBridgeProvider.capabilities()

        self.assertTrue(capabilities["polls"])
        self.assertFalse(capabilities["buttons"])
        self.assertFalse(capabilities["url_buttons"])
        self.assertFalse(capabilities["lists"])

    def test_no_provider_claims_call_buttons(self):
        """
        Meta offers a call button only inside an approved template, never on a
        free-form send. Claiming otherwise would suppress the fallback that
        turns it into a dial-able number.
        """
        for capabilities in provider_capabilities().values():
            self.assertFalse(capabilities["call_buttons"])

    def test_an_unconfigured_bridge_claims_nothing_at_all(self):
        """
        Including text and media, which every other provider declares True. A
        stand-in that cannot deliver anything must not have the composer offer
        message types against it.
        """
        capabilities = NotConfiguredProvider.capabilities()

        self.assertEqual(set(capabilities.values()), {False})

    def test_every_provider_answers_the_same_set_of_questions(self):
        """
        A capability that only some providers declare is one the composer
        cannot rely on. The dict is the contract.
        """
        expected = set(WhatsAppCloudAPIProvider.capabilities())
        for name, capabilities in provider_capabilities().items():
            with self.subTest(provider=name):
                self.assertEqual(set(capabilities), expected)


class ConnectionManagerCapabilityTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Caps", slug="caps-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="caps-owner", password="x", tenant=self.tenant,
        )
        self.cloud = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API, status=WhatsAppAccount.Status.CONNECTED,
        )
        self.qr = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="QR",
            connection_method=WhatsAppAccount.ConnectionMethod.QR, status=WhatsAppAccount.Status.CONNECTED,
        )

    def test_supports_answers_one_capability_by_name(self):
        with configured_qr_provider():
            self.assertTrue(ConnectionManager.supports(self.cloud, "lists"))
            self.assertFalse(ConnectionManager.supports(self.qr, "lists"))
            self.assertTrue(ConnectionManager.supports(self.qr, "polls"))
            self.assertFalse(ConnectionManager.supports(self.cloud, "polls"))

    def test_an_unknown_capability_is_false_rather_than_an_error(self):
        """
        A caller asking about something this deployment has never heard of is
        asking about something the connection certainly cannot do.
        """
        self.assertFalse(ConnectionManager.supports(self.cloud, "telepathy"))

    def test_the_old_button_helper_still_agrees_with_the_new_dict(self):
        """
        `supports_interactive_buttons` predates the capability dict and is
        still called from the send path and the opt-out affordance. The two
        must not be able to disagree.
        """
        with configured_qr_provider():
            for account in (self.cloud, self.qr):
                with self.subTest(account=account.name):
                    self.assertEqual(
                        ConnectionManager.supports_interactive_buttons(account),
                        ConnectionManager.capabilities(account)["buttons"],
                    )


class CapabilityApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="CapsApi", slug="caps-api", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="caps-api-owner", password="x", tenant=self.tenant,
        )
        self.account = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API, status=WhatsAppAccount.Status.CONNECTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_it_reports_each_account_and_the_provider_defaults(self):
        response = self.client.get("/api/v1/whatsapp/accounts/capabilities/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["accounts"]), 1)
        self.assertTrue(body["accounts"][0]["capabilities"]["lists"])
        # The provider defaults are there so a workspace with no Cloud API
        # number can still be told that a list *would* work on one -- a
        # purchase decision, not a refusal.
        self.assertIn("CLOUD_API", body["providers"])

    def test_it_does_not_report_another_workspaces_numbers(self):
        other = Tenant.objects.create(name="Other", slug="caps-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="caps-other-owner", password="x", tenant=other,
        )
        WhatsAppAccount.objects.create(
            tenant=other, user=other_user, name="Theirs",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API,
        )

        response = self.client.get("/api/v1/whatsapp/accounts/capabilities/")

        self.assertEqual([row["name"] for row in response.json()["accounts"]], ["Cloud"])

    def test_it_requires_authentication(self):
        self.assertEqual(
            APIClient().get("/api/v1/whatsapp/accounts/capabilities/").status_code, 401,
        )
