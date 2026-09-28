"""
Redirect safety for the two public, unauthenticated redirectors.

``/r/<code>`` and ``/c/<code>`` are the only endpoints on this deployment that
take a destination somebody else supplied and tell a browser to go there. A
scheme this origin will forward to is a scheme this origin lends its name to,
so the allowlist is checked at both ends: when a link is written, and again at
the moment of the redirect.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.tenants.models import Tenant
from apps.tracking import services
from apps.tracking.models import TrackedLink
from core.urls_safety import is_safe_redirect_url, normalize_redirect_url

DANGEROUS_URLS = [
    "javascript:alert(document.cookie)",
    "JavaScript:alert(1)",
    "java\tscript:alert(1)",
    "java\nscript:alert(1)",
    " javascript:alert(1)",
    "\x00javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "DATA:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "FILE:///c:/windows/win.ini",
    "vbscript:msgbox(1)",
    "blob:https://example.com/1234",
    "about:blank",
    "//evil.example.com",
    "/relative/path",
    "https://",
    "http:///no-host",
    "https://user:pass@evil.example.com/",
]


class RedirectUrlValidationTests(TestCase):
    def test_https_is_accepted(self):
        self.assertEqual(
            normalize_redirect_url("https://example.com/offer?a=1#top"),
            "https://example.com/offer?a=1#top",
        )

    def test_http_is_accepted(self):
        self.assertEqual(normalize_redirect_url("http://example.com/offer"), "http://example.com/offer")

    def test_an_internal_hostname_without_a_tld_is_accepted(self):
        """Deliberately looser than URLValidator: http://minio/bucket is real."""
        self.assertTrue(is_safe_redirect_url("http://intranet/page"))

    def test_dangerous_schemes_and_shapes_are_refused(self):
        for url in DANGEROUS_URLS:
            with self.subTest(url=url):
                self.assertIsNone(normalize_redirect_url(url))

    def test_empty_and_non_string_input_is_refused(self):
        for value in (None, "", "   ", 42, [], {}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_redirect_url(value))


class RedirectEndpointTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-redirect-sec", kind=Tenant.Kind.CUSTOMER)
        get_user_model().objects.create_user(
            username="acme-redirect-sec-owner", password="x", tenant=self.tenant
        )

    def _link(self, target):
        return TrackedLink.objects.create(
            tenant=self.tenant, code=f"c{TrackedLink.objects.count()}",
            original_url=target, target_url=target,
        )

    def test_an_https_link_redirects(self):
        link = self._link("https://example.com/sale")

        response = self.client.get(f"/r/{link.code}")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://example.com/sale")

    def test_an_http_link_redirects(self):
        link = self._link("http://example.com/sale")

        self.assertEqual(self.client.get(f"/r/{link.code}").status_code, 302)

    def test_a_dangerous_destination_is_a_dead_link_not_a_redirect(self):
        """
        A row can only get a dangerous target through a bug or a future change
        to the rewriter's pattern -- which is exactly the case this defends.
        """
        for url in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd"):
            with self.subTest(url=url):
                link = self._link(url)

                response = self.client.get(f"/r/{link.code}")

                self.assertEqual(response.status_code, 404)

    def test_an_unknown_code_is_a_404(self):
        self.assertEqual(self.client.get("/r/nope").status_code, 404)


class LinkRewritingTests(TestCase):
    """The other end: what does and does not become a short link."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-rewrite-sec", kind=Tenant.Kind.CUSTOMER)

    def test_an_http_url_is_shortened(self):
        with self.settings(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://app.example.com"):
            text, links = services.rewrite("See https://shop.example.com/x", tenant_id=self.tenant.pk)

        self.assertEqual(len(links), 1)
        self.assertNotIn("shop.example.com", text)

    def test_a_dangerous_scheme_is_left_alone_and_creates_no_link(self):
        with self.settings(LINK_TRACKING_ENABLED=True, LINK_TRACKING_BASE_URL="https://app.example.com"):
            text, links = services.rewrite(
                "Tap javascript:alert(1) or data:text/html,x", tenant_id=self.tenant.pk
            )

        self.assertEqual(links, [])
        # The tenant's text is a record of what the contact was shown, so it is
        # reported faithfully rather than silently edited.
        self.assertIn("javascript:alert(1)", text)
