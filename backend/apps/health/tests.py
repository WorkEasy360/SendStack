"""
Health probes, support diagnostics, production checks, and error-report scrubbing.

Everything here answers one of the operational questions in the brief: can we
tell the application is alive, can support see what is wrong, can a release be
stopped before it ships misconfigured, and does the error tracker leak.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.health import checks
from apps.tenants.models import Tenant
from core import monitoring


class ProbeTests(TestCase):
    def test_liveness_is_public_and_does_not_touch_dependencies(self):
        """
        If liveness checked PostgreSQL, a database blip would make the
        orchestrator kill every healthy container at exactly the moment the
        database was struggling.
        """
        response = APIClient().get("/health/live/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")
        self.assertNotIn("checks", response.json())

    def test_readiness_reports_its_dependencies(self):
        response = APIClient().get("/health/ready/")
        self.assertIn("checks", response.json())
        self.assertIn("database", response.json()["checks"])

    def test_the_probes_report_the_build(self):
        """During an incident, "which build is live" must be answerable."""
        body = APIClient().get("/health/live/").json()
        for key in ("version", "build", "environment"):
            self.assertIn(key, body)

    def test_the_probes_are_not_throttled(self):
        """
        The regression: DEFAULT_THROTTLE_CLASSES applies AnonRateThrottle to
        every view that does not say otherwise, and AnonRateThrottle reads and
        writes the cache -- which is Redis.

        So the probe whose entire purpose is to answer without consulting
        anything was consulting Redis on every call, paying a connect timeout
        during exactly the outage it needs to stay fast through. And it was
        rate-limited: probes poll every 30 seconds from more than one source,
        and a 429 reads to an orchestrator as "unhealthy".
        """
        from apps.health.views import LivenessView, ReadinessView

        self.assertEqual(LivenessView.throttle_classes, [])
        self.assertEqual(ReadinessView.throttle_classes, [])

    def test_liveness_answers_with_the_cache_unavailable(self):
        """A dead cache must not slow down or fail the liveness probe."""
        from unittest import mock

        with mock.patch("django.core.cache.cache.get", side_effect=RuntimeError("redis down")),                 mock.patch("django.core.cache.cache.set", side_effect=RuntimeError("redis down")):
            response = APIClient().get("/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "alive")

    def test_the_probes_expose_no_configuration(self):
        raw = str(APIClient().get("/health/ready/").json())
        for marker in ("SECRET", "PASSWORD", "TOKEN", "postgres://", "redis://"):
            self.assertNotIn(marker, raw)


class DiagnosticsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-diag", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada-diag", email="ada@diag.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
        )
        self.client = APIClient()

    def test_diagnostics_requires_authentication(self):
        self.assertIn(APIClient().get("/health/diagnostics/").status_code, (401, 403))

    def test_diagnostics_reports_connectivity_and_the_callers_account(self):
        self.client.force_authenticate(self.user)
        body = self.client.get("/health/diagnostics/").json()

        self.assertIn("connectivity", body)
        self.assertIn("database", body["connectivity"])
        self.assertEqual(body["account"]["workspace"], "Acme")

    def test_diagnostics_never_exposes_credentials(self):
        from django.conf import settings

        self.client.force_authenticate(self.user)
        raw = str(self.client.get("/health/diagnostics/").json())

        self.assertNotIn(settings.SECRET_KEY, raw)
        for marker in ("SECRET_KEY", "PASSWORD", "access_token", "DATABASE_URL", "REDIS_URL"):
            self.assertNotIn(marker, raw)

    def test_diagnostics_shows_no_other_tenant(self):
        other = Tenant.objects.create(name="Rival", slug="rival-diag", kind=Tenant.Kind.CUSTOMER)
        get_user_model().objects.create_user(
            username="eve-diag", email="eve@diag.example",
            password="correct-horse-battery-staple", tenant=other,
        )
        self.client.force_authenticate(self.user)
        self.assertNotIn("Rival", str(self.client.get("/health/diagnostics/").json()))

    def test_an_ordinary_user_does_not_see_the_readiness_report(self):
        """
        It names every misconfiguration in the deployment. Useful to whoever
        runs it, and a reconnaissance list for everybody else.
        """
        self.client.force_authenticate(self.user)
        self.assertNotIn("production_readiness", self.client.get("/health/diagnostics/").json())

    def test_a_superadmin_does_see_it(self):
        admin = get_user_model().objects.create_user(
            username="root-diag", email="root@diag.example",
            password="correct-horse-battery-staple", tenant=self.tenant,
            role=get_user_model().Role.SUPERADMIN,
        )
        self.client.force_authenticate(admin)
        body = self.client.get("/health/diagnostics/").json()
        self.assertIn("production_readiness", body)
        self.assertIn("summary", body["production_readiness"])


class ProductionCheckTests(TestCase):
    def test_every_check_returns_a_verdict_and_never_a_value(self):
        from django.conf import settings

        results = checks.run_all()
        self.assertTrue(results)
        for result in results:
            self.assertIn(result["status"], (checks.PASS, checks.WARN, checks.FAIL))
            # The whole point: this output goes into deploy logs, CI and a
            # screenshot in a support ticket.
            self.assertNotIn(settings.SECRET_KEY, result["detail"])

    def test_debug_on_is_a_failure(self):
        with override_settings(DEBUG=True):
            self.assertEqual(checks.check_debug()["status"], checks.FAIL)

    def test_debug_off_passes(self):
        with override_settings(DEBUG=False):
            self.assertEqual(checks.check_debug()["status"], checks.PASS)

    def test_a_placeholder_secret_key_is_a_failure(self):
        with override_settings(SECRET_KEY="django-insecure-development-only-key"):
            self.assertEqual(checks.check_secret_key()["status"], checks.FAIL)

    def test_a_short_secret_key_is_at_least_a_warning(self):
        """
        The empty case cannot be exercised through override_settings -- Django
        raises before the check ever runs -- so the check keeps its empty-string
        branch as defence and this asserts the next weakest case.
        """
        with override_settings(SECRET_KEY="short"):
            self.assertIn(checks.check_secret_key()["status"], (checks.WARN, checks.FAIL))

    def test_a_wildcard_allowed_host_is_a_failure(self):
        with override_settings(ALLOWED_HOSTS=["*"]):
            self.assertEqual(checks.check_allowed_hosts()["status"], checks.FAIL)

    def test_open_cors_is_a_failure(self):
        with override_settings(CORS_ALLOW_ALL_ORIGINS=True):
            self.assertEqual(checks.check_cors()["status"], checks.FAIL)

    def test_a_console_email_backend_is_a_failure(self):
        """A deployment that prints reset links to a log has no password recovery."""
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend"):
            self.assertEqual(checks.check_email()["status"], checks.FAIL)

    def test_a_localhost_frontend_url_is_a_failure(self):
        with override_settings(FRONTEND_URL="http://localhost:5173"):
            self.assertEqual(checks.check_frontend_url()["status"], checks.FAIL)

    def test_a_missing_encryption_key_is_a_failure(self):
        with override_settings(WHATSAPP_TOKEN_ENCRYPTION_KEY=""):
            self.assertEqual(checks.check_encryption_key()["status"], checks.FAIL)

    def test_unconfigured_legal_documents_are_only_a_warning(self):
        """They are served with a visible draft banner, which is survivable."""
        with override_settings(
            LEGAL_ENTITY_NAME="", LEGAL_ENTITY_ADDRESS="",
            LEGAL_CONTACT_EMAIL="", LEGAL_GOVERNING_LAW="",
        ):
            self.assertEqual(checks.check_legal_documents()["status"], checks.WARN)

    def test_a_broken_check_reports_as_a_failure_rather_than_crashing(self):
        def explode():
            raise RuntimeError("nope")

        original = checks.ALL_CHECKS
        try:
            checks.ALL_CHECKS = (explode,)
            results = checks.run_all()
        finally:
            checks.ALL_CHECKS = original

        self.assertEqual(results[0]["status"], checks.FAIL)

    def test_the_summary_counts_add_up(self):
        results = checks.run_all()
        counts = checks.summarise(results)
        self.assertEqual(counts["passed"] + counts["warnings"] + counts["failed"], counts["total"])


class ErrorScrubbingTests(TestCase):
    """
    Sentry's defaults send request bodies, headers, cookies and stack-frame
    locals. In this application those carry WhatsApp tokens, message bodies and
    phone numbers.
    """

    def test_secrets_are_redacted_by_key_name(self):
        scrubbed = monitoring.scrub({
            "password": "hunter2",
            "api_key": "sk-live-123",
            "authorization": "Bearer abc",
            "whatsapp_token": "EAA-secret",
            "harmless": "keep me",
        })
        for key in ("password", "api_key", "authorization", "whatsapp_token"):
            self.assertEqual(scrubbed[key], monitoring.REDACTED)
        self.assertEqual(scrubbed["harmless"], "keep me")

    def test_customer_content_is_redacted(self):
        scrubbed = monitoring.scrub({"body": "Hi Bob, your order is ready", "email": "bob@example.com"})
        self.assertEqual(scrubbed["body"], monitoring.REDACTED)
        self.assertEqual(scrubbed["email"], monitoring.REDACTED)

    def test_phone_numbers_in_free_text_are_masked(self):
        scrubbed = monitoring.scrub({"detail": "Send to +919876543210 failed"})
        self.assertNotIn("+919876543210", scrubbed["detail"])
        self.assertIn("3210", scrubbed["detail"])

    def test_nested_structures_are_scrubbed(self):
        scrubbed = monitoring.scrub({"outer": {"inner": [{"password": "x"}]}})
        self.assertEqual(scrubbed["outer"]["inner"][0]["password"], monitoring.REDACTED)

    def test_a_self_referential_structure_does_not_hang(self):
        """A scrubber that hangs the error handler is worse than no scrubber."""
        loop = {}
        loop["self"] = loop
        self.assertIsNotNone(monitoring.scrub(loop))

    def test_stack_frame_locals_are_scrubbed(self):
        event = {
            "exception": {
                "values": [{"stacktrace": {"frames": [{"vars": {"password": "hunter2"}}]}}]
            }
        }
        result = monitoring.before_send(event, None)
        self.assertEqual(
            result["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]["password"],
            monitoring.REDACTED,
        )

    def test_the_user_block_keeps_only_an_id(self):
        event = {"user": {"id": 7, "email": "ada@example.com", "ip_address": "1.2.3.4"}}
        result = monitoring.before_send(event, None)
        self.assertEqual(result["user"], {"id": 7})

    def test_monitoring_is_off_without_a_dsn(self):
        """Every development machine and every test run."""
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SENTRY_DSN", None)
            self.assertFalse(monitoring.configure())

    def test_masking_a_phone_keeps_the_last_four_digits(self):
        masked = monitoring.mask_phone("+919876543210")
        self.assertTrue(masked.endswith("3210"))
        self.assertNotIn("98765", masked)
