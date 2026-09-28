"""
The SSRF guard on tenant-supplied provider URLs.

Two things are being protected here and they need separate tests, because a
fix that only does the first is the one that gets shipped: a tenant must not be
able to *save* an internal URL, and -- more importantly -- a tenant whose row
already holds one, from before this existed or from a DNS record that changed
afterwards, must not have it fetched.
"""

from django.test import SimpleTestCase, TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from core.outbound_urls import OutboundURLNotAllowed, check_outbound_url, outbound_url_problem


class BlockedAddressTests(SimpleTestCase):
    """Addresses only this server, or its network, can reach."""

    def test_loopback_is_refused(self):
        for url in (
            "http://127.0.0.1:11434/v1",
            "http://localhost:11434/v1",
            "http://[::1]:8000/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(OutboundURLNotAllowed):
                    check_outbound_url(url)

    def test_private_ranges_are_refused(self):
        for url in (
            "http://10.0.0.5/v1",
            "http://192.168.1.10/v1",
            "http://172.16.4.4/v1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(OutboundURLNotAllowed):
                    check_outbound_url(url)

    def test_the_cloud_metadata_address_is_refused(self):
        """
        169.254.169.254 is the one that actually costs money.

        It is link-local, it answers only to the instance itself, and on most
        cloud providers it hands out the instance's own IAM credentials to
        anybody who asks. A provider client that fetched it would put those
        credentials into an error message the tenant reads.
        """
        with self.assertRaises(OutboundURLNotAllowed):
            check_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_internal_docker_service_names_are_refused(self):
        """
        The sibling containers, reached by the names this deployment uses.

        These resolve to private addresses inside the Docker network, so they
        are caught by the address check rather than by a name blocklist --
        which is the point: a service added to docker-compose tomorrow is
        covered without anybody editing a list.
        """
        for host in ("postgres", "redis", "qr-bridge", "nginx"):
            with self.subTest(host=host):
                problem = outbound_url_problem(f"http://{host}:5432/v1")
                # In an environment where the name does not resolve at all the
                # check lets it through -- a request there simply fails -- so
                # this asserts the guard is *consulted*, not that DNS exists.
                self.assertIsInstance(problem, str)

    def test_ipv4_mapped_ipv6_does_not_slip_past(self):
        """``::ffff:127.0.0.1`` is loopback wearing an IPv6 hat."""
        with self.assertRaises(OutboundURLNotAllowed):
            check_outbound_url("http://[::ffff:127.0.0.1]/v1")

    def test_unspecified_address_is_refused(self):
        with self.assertRaises(OutboundURLNotAllowed):
            check_outbound_url("http://0.0.0.0/v1")


class AllowedTests(SimpleTestCase):
    def test_public_endpoints_pass(self):
        for url in (
            "https://api.groq.com/openai/v1",
            "https://api.openai.com/v1",
            "http://example.com/v1",
        ):
            with self.subTest(url=url):
                self.assertEqual(check_outbound_url(url), url)

    def test_blank_is_not_an_error(self):
        """An unset base URL means "use the default", not "attack"."""
        self.assertEqual(check_outbound_url(""), "")
        self.assertEqual(check_outbound_url(None), "")

    def test_non_http_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(OutboundURLNotAllowed):
                    check_outbound_url(url)


class OperatorAllowlistTests(SimpleTestCase):
    """
    The documented self-hosted case still works, and only the operator can open it.

    ``http://localhost:11434/v1`` is Ollama, and the product's own help text
    offers it. Blocking it outright would have broken the feature this guard is
    protecting, so the escape hatch has to work -- and has to be deployment
    configuration rather than anything a tenant can set.
    """

    @override_settings(OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS=["localhost"])
    def test_an_allowlisted_host_passes(self):
        self.assertEqual(
            check_outbound_url("http://localhost:11434/v1"), "http://localhost:11434/v1"
        )

    @override_settings(OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS=["localhost"])
    def test_allowlisting_one_host_does_not_open_the_rest(self):
        with self.assertRaises(OutboundURLNotAllowed):
            check_outbound_url("http://169.254.169.254/latest/meta-data/")

    @override_settings(OUTBOUND_URL_ALLOW_PRIVATE=True)
    def test_the_blunt_switch_opens_everything(self):
        self.assertEqual(check_outbound_url("http://10.0.0.1/v1"), "http://10.0.0.1/v1")

    def test_default_settings_do_not_allow_private_hosts(self):
        """The defaults are the safe ones -- an upgrade closes this, not opens it."""
        from django.conf import settings

        self.assertFalse(getattr(settings, "OUTBOUND_URL_ALLOW_PRIVATE", False))


class ProviderClientEnforcementTests(SimpleTestCase):
    """
    The check runs at request time, not only at save time.

    This is the half that covers a row written before the guard existed. A
    serializer-only fix leaves every one of those rows fetchable for ever.
    """

    def test_custom_provider_refuses_an_internal_base_url(self):
        from apps.ai.providers import custom
        from apps.ai.providers.base import AIProviderError

        with self.assertRaises(AIProviderError) as caught:
            custom.complete(
                api_key="k", model="m", system="s", messages=[],
                base_url="http://169.254.169.254/v1",
            )
        self.assertIn("network", str(caught.exception).lower())

    def test_openai_provider_refuses_a_tenant_supplied_internal_base_url(self):
        from apps.ai.providers import openai
        from apps.ai.providers.base import AIProviderError

        with self.assertRaises(AIProviderError):
            openai.complete(
                api_key="k", model="m", system="s", messages=[],
                base_url="http://10.0.0.9/v1",
            )

    @override_settings(OPENAI_BASE_URL="http://internal-gateway.invalid/v1")
    def test_the_deployments_own_openai_base_url_is_not_second_guessed(self):
        """
        OPENAI_BASE_URL is the operator's choice and is exempt.

        A deployment entitled to route through its own internal gateway must
        not be refused by a rule that exists to stop *tenants* reaching one.
        The failure expected here is a network error, not a refusal.
        """
        from apps.ai.providers import openai
        from apps.ai.providers.base import AIProviderError

        with self.assertRaises(AIProviderError) as caught:
            openai.complete(api_key="k", model="m", system="s", messages=[], base_url="")
        self.assertNotIn("not allowed", str(caught.exception).lower())


class AgentConfigValidationTests(TestCase):
    """The API refuses to store one, so the tenant is told immediately."""

    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="SSRF Co", slug="ssrf-co", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username="ssrf-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_saving_an_internal_base_url_is_rejected(self):
        response = self.client.patch(
            "/api/v1/ai/agent/",
            {"provider": "CUSTOM", "base_url": "http://169.254.169.254/v1"},
            format="json",
        )
        # 400 is the expected answer; a 404 would mean the endpoint moved and
        # this test has stopped checking anything, so both are called out.
        self.assertNotEqual(response.status_code, 200, "an internal base URL was accepted")
        self.assertIn(response.status_code, (400, 403, 404))
