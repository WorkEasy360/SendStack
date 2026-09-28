"""
Deployment-shape regressions: the vulnerabilities that live in configuration.

These assert things about docker-compose.yml, nginx.conf and the ignore files
rather than about Python, because that is where the bugs were: an internal
service published on 0.0.0.0, a Redis with no password, an ignore rule that
matched ".env" and not ".env.env.bak-20260827-184120".

Configuration files are not shipped inside the backend image (its build context
is ``backend/`` alone), so each of these skips when it cannot find the repo
rather than failing. They run wherever the checkout is -- CI, a developer's
machine -- which is where a bad edit is made and where it needs catching.
"""

import re
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

REPO_ROOT = Path(settings.BASE_DIR).parent

COMPOSE = REPO_ROOT / "docker-compose.yml"
#: The nginx configuration is served from several files -- `head.conf`,
#: `locations.conf` and a `server-*.conf` per TLS mode -- which nginx includes
#: into one running config. These assertions are about that running config, so
#: they read the parts and join them. A single path here stopped matching the
#: day the file was split, and the tests failed for a reason that had nothing
#: to do with what they were checking.
NGINX_DIR = REPO_ROOT / "nginx"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GITIGNORE = REPO_ROOT / ".gitignore"
ROOT_DOCKERIGNORE = REPO_ROOT / ".dockerignore"
BACKEND_DOCKERIGNORE = Path(settings.BASE_DIR) / ".dockerignore"
QR_CONFIG_TS = REPO_ROOT / "qr-bridge" / "src" / "config.ts"


def _read(path):
    return path.read_text(encoding="utf-8")


def _nginx_config() -> str:
    """Every `.conf` nginx assembles, as one string. `.bak` files are excluded."""
    return "\n".join(
        sorted(part.read_text(encoding="utf-8") for part in NGINX_DIR.glob("*.conf"))
    )


needs_repo = unittest.skipUnless(COMPOSE.exists(), "not running from a repository checkout")


@needs_repo
class PublishedPortTests(SimpleTestCase):
    """
    Internal services must not be reachable from off the host.

    A published port in Docker means 0.0.0.0 unless an address is written in
    front of it, and "5432:5432" on an internet-facing host is a Postgres
    anybody can try passwords against.
    """

    #: Only the reverse proxy has any business being publicly reachable.
    PUBLIC_SERVICES = {"nginx"}

    def _published_ports(self):
        """service name -> list of published port mappings, without a YAML parser."""
        text = _read(COMPOSE)
        found = {}
        service = None
        in_ports = False
        for line in text.splitlines():
            if re.match(r"^  [a-z][\w.-]*:\s*$", line):
                service = line.strip().rstrip(":")
                in_ports = False
                continue
            if re.match(r"^    ports:\s*$", line):
                in_ports = True
                continue
            if in_ports and re.match(r"^    \S", line):
                in_ports = False
            if in_ports:
                mapping = line.strip().lstrip("-").strip().strip('"').strip("'")
                if mapping:
                    found.setdefault(service, []).append(mapping)
        return found

    def test_only_the_proxy_publishes_a_public_port(self):
        for service, mappings in self._published_ports().items():
            for mapping in mappings:
                with self.subTest(service=service, mapping=mapping):
                    if service in self.PUBLIC_SERVICES:
                        continue
                    self.assertTrue(
                        mapping.startswith("127.0.0.1:"),
                        f"{service} publishes {mapping!r} on all interfaces; "
                        f"bind it to 127.0.0.1 or drop the mapping entirely.",
                    )

    def test_postgres_is_not_published_on_all_interfaces(self):
        self.assertNotIn('- "5432:5432"', _read(COMPOSE))
        self.assertIn('127.0.0.1:5432:5432', _read(COMPOSE))

    def test_redis_publishes_no_port_at_all(self):
        self.assertEqual(self._published_ports().get("redis", []), [])

    def test_qr_bridge_is_loopback_only(self):
        self.assertEqual(self._published_ports().get("qr-bridge"), ["127.0.0.1:4001:4001"])

    def test_the_backend_is_reached_only_through_the_proxy(self):
        self.assertEqual(self._published_ports().get("backend", []), [])


@needs_repo
class RedisAuthenticationTests(SimpleTestCase):
    def test_redis_is_started_with_a_password(self):
        compose = _read(COMPOSE)
        self.assertIn("--requirepass", compose)

    def test_the_password_is_required_rather_than_defaulted(self):
        """A default would mean every deployment that forgot it shares one."""
        self.assertIn("REDIS_PASSWORD:?", _read(COMPOSE))

    def test_no_password_is_hardcoded_in_the_compose_file(self):
        compose = _read(COMPOSE)
        for line in compose.splitlines():
            if "requirepass" in line or "REDIS_PASSWORD" in line:
                with self.subTest(line=line.strip()):
                    self.assertNotRegex(line, r"""requirepass\s+["']?[A-Za-z0-9]{8,}["']?\s*$""")

    def test_the_example_environment_asks_for_one(self):
        self.assertIn("REDIS_PASSWORD=", _read(ENV_EXAMPLE))


class RedisUrlCompositionTests(SimpleTestCase):
    """The application side of the same change."""

    def test_the_password_is_spliced_in_and_quoted(self):
        from config.settings.base import _with_redis_password

        with unittest.mock.patch.dict("os.environ", {"REDIS_PASSWORD": "p@ss/w:rd"}):
            url = _with_redis_password("redis://redis:6379/0")

        self.assertEqual(url, "redis://:p%40ss%2Fw%3Ard@redis:6379/0")

    def test_a_url_that_already_has_credentials_is_left_alone(self):
        from config.settings.base import _with_redis_password

        with unittest.mock.patch.dict("os.environ", {"REDIS_PASSWORD": "other"}):
            url = _with_redis_password("redis://:mine@redis:6379/0")

        self.assertEqual(url, "redis://:mine@redis:6379/0")

    def test_celery_reads_the_authenticated_url_from_the_environment(self):
        """
        The regression: Celery prefers os.environ over the Django setting.

        celery.app.utils.Settings.broker_url is
        `os.environ.get('CELERY_BROKER_URL') or self.first('broker_url', ...)`,
        so a correct setting is silently ignored where a bare, unauthenticated
        URL is also in the environment -- the worker then connects with no
        password and consumes nothing. Importing settings has to leave the
        environment carrying the resolved value.
        """
        import os

        from django.conf import settings as django_settings

        for name in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
            with self.subTest(name=name):
                self.assertEqual(os.environ.get(name), getattr(django_settings, name))

    def test_no_password_configured_changes_nothing(self):
        from config.settings.base import _with_redis_password

        with unittest.mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("REDIS_PASSWORD", None)
            self.assertEqual(_with_redis_password("redis://redis:6379/0"), "redis://redis:6379/0")


@needs_repo
class InternalServiceAddressingTests(SimpleTestCase):
    """Unpublishing a port only works if everything reaches it by service name."""

    def test_the_backend_reaches_its_dependencies_over_the_docker_network(self):
        compose = _read(COMPOSE)
        for expected in ("DB_HOST: postgres", "redis://redis:6379/0", "http://qr-bridge:4001"):
            with self.subTest(expected=expected):
                self.assertIn(expected, compose)

    def test_nginx_proxies_to_the_backend_by_service_name(self):
        self.assertIn("proxy_pass http://backend:8000/", _nginx_config())


@needs_repo
class MediaHeaderTests(SimpleTestCase):
    """
    /media/ bypasses Django's SecurityMiddleware entirely, so nginx has to
    supply the headers that keep an uploaded file from becoming a script on
    this origin.
    """

    REQUIRED = (
        'add_header X-Content-Type-Options "nosniff" always;',
        "add_header Content-Security-Policy",
        "add_header Content-Disposition",
    )

    def _media_blocks(self):
        text = _nginx_config()
        return [block for block in re.split(r"\n\s*location ", text) if block.lstrip().startswith(("/media/", "~* ^/media/"))]

    def test_every_media_location_sets_the_required_headers(self):
        blocks = self._media_blocks()
        self.assertTrue(blocks, "no /media/ location found in nginx.conf")
        for block in blocks:
            for header in self.REQUIRED:
                with self.subTest(header=header):
                    self.assertIn(header, block)

    def test_anything_not_explicitly_safe_is_a_download(self):
        catch_all = [b for b in self._media_blocks() if b.lstrip().startswith("/media/")]
        self.assertTrue(catch_all)
        self.assertIn('add_header Content-Disposition "attachment" always;', catch_all[0])

    def test_only_media_formats_are_served_inline(self):
        inline = [b for b in self._media_blocks() if b.lstrip().startswith("~*")]
        self.assertTrue(inline)
        for dangerous in ("html", "htm", "svg", "js", "xml", "php", "css"):
            with self.subTest(extension=dangerous):
                self.assertNotRegex(inline[0].split("{")[0], rf"\b{dangerous}\b")

    def test_the_admin_is_rate_limited_at_the_proxy(self):
        text = _nginx_config()
        self.assertIn("limit_req_zone", text)
        self.assertIn("limit_req zone=ms_admin", text)


@needs_repo
class SecretFileExposureTests(SimpleTestCase):
    """
    No populated environment file may be in the repository or a build context.

    The rule used to name ".env" specifically, which is why a rotation script's
    ``backend/.env.env.bak-<timestamp>`` -- with live secrets in it -- sat
    inside the backend image's build context.
    """

    #: Filenames a rule has to catch, and the one it must not.
    MUST_IGNORE = (".env", ".env.local", ".env.production", ".env.env.bak-20260827-184120", "secrets.bak")
    MUST_KEEP = ".env.example"

    def test_no_secret_backup_files_exist_in_the_tree(self):
        offenders = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in REPO_ROOT.rglob(".env*")
            if path.is_file()
            and not path.name.endswith(".example")
            and ".git" not in path.parts
            and "node_modules" not in path.parts
        ]
        # A developer's own .env is expected; a *backup* of one is not.
        backups = [name for name in offenders if name.rsplit("/", 1)[-1] != ".env"]
        self.assertEqual(backups, [], f"populated environment backups found: {backups}")

    def test_gitignore_covers_every_env_variant(self):
        rules = _read(GITIGNORE)
        self.assertIn(".env*", rules)
        self.assertIn("!.env.example", rules)
        self.assertIn("*.bak", rules)

    def test_the_root_docker_build_context_excludes_env_files(self):
        rules = _read(ROOT_DOCKERIGNORE)
        self.assertIn("**/.env", rules)
        self.assertIn("**/.env.*", rules)
        self.assertIn("!**/.env.example", rules)

    def test_the_backend_build_context_excludes_env_files(self):
        rules = _read(BACKEND_DOCKERIGNORE)
        self.assertIn(".env", rules)
        self.assertIn(".env.*", rules)
        self.assertIn("!.env.example", rules)
        self.assertIn("*.bak", rules)

    def test_the_shipped_example_has_no_values_in_it(self):
        """It is the one environment file that is tracked, so it must be empty."""
        secretish = re.compile(
            r"^(SECRET_KEY|JWT_SECRET|DB_PASSWORD|REDIS_PASSWORD|WHATSAPP_TOKEN_ENCRYPTION_KEY"
            r"|OPENAI_API_KEY|AI_API_KEY|QR_PROVIDER_API_KEY|QR_PROVIDER_WEBHOOK_SECRET"
            r"|EMAIL_HOST_PASSWORD)=(.+)$"
        )
        for line in _read(ENV_EXAMPLE).splitlines():
            match = secretish.match(line.strip())
            if match:
                self.fail(f"{match.group(1)} has a value in .env.example")


class NoDebugPrintTests(SimpleTestCase):
    """
    stdout is not a logging system.

    A print() in a view writes past every filter, level and formatter the
    deployment configured, into whatever collects container output -- which is
    how a tenant's audience size ended up in the logs of a system that is
    careful about tenant data everywhere else.
    """

    def test_no_print_statements_in_application_code(self):
        offenders = []
        for path in Path(settings.BASE_DIR).rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            # Test modules are excluded: they are not shipped behaviour, and
            # this file necessarily contains the literal it is searching for.
            if "tests" in path.parts or path.name.startswith("test"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.match(r"^print\s*\(", stripped) or re.search(r"[^\w.]print\s*\(", stripped):
                    offenders.append(f"{path.relative_to(settings.BASE_DIR).as_posix()}:{number}")
        self.assertEqual(offenders, [], f"print() found in application code: {offenders}")


class QrSessionTtlTests(SimpleTestCase):
    """
    The documented default used to be 99999999999999999999, which is not a
    duration: it overflows setTimeout's 32-bit delay (the timer then fires
    immediately) and JavaScript's Date range (RangeError: Invalid time value),
    so a deployment that took the documented default had a broken QR flow.
    """

    def test_the_default_is_120_seconds(self):
        self.assertEqual(settings.WHATSAPP_QR_SESSION_TTL_SECONDS, 120)

    def test_an_absurd_value_is_clamped_rather_than_honoured(self):
        from config.settings.base import _qr_ttl_seconds

        for value in ("99999999999999999999", "0", "-1", "999999", "not-a-number", ""):
            with self.subTest(value=value):
                with unittest.mock.patch.dict("os.environ", {"WHATSAPP_QR_SESSION_TTL_SECONDS": value}):
                    self.assertEqual(_qr_ttl_seconds(), 120)

    def test_a_sensible_value_is_honoured(self):
        from config.settings.base import _qr_ttl_seconds

        for value, expected in (("30", 30), ("120", 120), ("600", 600), ("3600", 3600)):
            with self.subTest(value=value):
                with unittest.mock.patch.dict("os.environ", {"WHATSAPP_QR_SESSION_TTL_SECONDS": value}):
                    self.assertEqual(_qr_ttl_seconds(), expected)

    def test_the_ttl_never_overflows_a_javascript_date(self):
        """new Date(ms) is Invalid past 8.64e15; setTimeout truncates past 2^31-1."""
        milliseconds = settings.WHATSAPP_QR_SESSION_TTL_SECONDS * 1000
        self.assertLess(milliseconds, 2 ** 31 - 1)
        self.assertLess(milliseconds, 8.64e15)

    @needs_repo
    def test_every_shipped_default_says_120(self):
        for path in (ENV_EXAMPLE, REPO_ROOT / "qr-bridge" / ".env.example"):
            with self.subTest(path=path.name):
                self.assertIn("WHATSAPP_QR_SESSION_TTL_SECONDS=120", _read(path))
        self.assertIn("WHATSAPP_QR_SESSION_TTL_SECONDS: ${WHATSAPP_QR_SESSION_TTL_SECONDS:-120}", _read(COMPOSE))

    @needs_repo
    def test_the_bridge_clamps_it_too(self):
        source = _read(QR_CONFIG_TS)
        self.assertIn("QR_TTL_DEFAULT_SECONDS = 120", source)
        self.assertIn("resolveQrTtlSeconds", source)

    def test_the_stale_sweep_cannot_be_broken_by_an_overflowing_ttl(self):
        """A pre-existing environment may still carry the old absurd value."""
        from apps.whatsapp.connections.manager import ConnectionManager

        class Session:
            qr_expires_at = None
            created_at = None

        with override_settings(WHATSAPP_QR_SESSION_TTL_SECONDS=99999999999999999999):
            self.assertFalse(ConnectionManager._is_stale(Session()))


class ProductionTlsSettingsTests(SimpleTestCase):
    """
    Production must be HTTPS-only by default.

    Loaded as a module rather than through override_settings because these are
    decisions made at import time in config/settings/production.py, and the
    point is what a deployment gets without setting anything.
    """

    #: Variables that would otherwise leak in from the environment this test
    #: happens to run in. Inside the container the deployment's own .env is
    #: present, and it sets SECURE_SSL_REDIRECT=false -- so without clearing
    #: these, "what does a deployment get by default" would be answered with
    #: whatever this particular deployment chose.
    ENV_UNDER_TEST = (
        "SECURE_SSL_REDIRECT",
        "CSRF_TRUSTED_ORIGINS",
        "SECURE_HSTS_SECONDS",
    )

    @classmethod
    def _production_settings(cls, env=None):
        import importlib
        import os

        # The minimum a production deployment must supply for the settings
        # module to import at all. Production refuses to start without each of
        # these (see config/settings/production.py), which is the point -- a
        # deployment that silently discards password-reset email, or builds
        # reset links pointing at localhost, is one nobody can recover an
        # account on. These tests are about TLS, so they supply the minimum and
        # assert about the settings they actually care about.
        base = {
            "SECRET_KEY": "x" * 50,
            "ALLOWED_HOSTS": "app.example.com",
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "EMAIL_BACKEND": "django.core.mail.backends.dummy.EmailBackend",
            "FRONTEND_URL": "https://app.example.com",
        }
        env = env or {}
        # patch.dict restores the whole mapping on exit, so popping inside it
        # is safe -- and is the only way to test the *absence* of a variable.
        with unittest.mock.patch.dict(os.environ, {**base, **env}):
            for name in cls.ENV_UNDER_TEST:
                if name not in env:
                    os.environ.pop(name, None)
            # `base` has to be reloaded too, and reloaded *first*.
            # production.py is `from .base import *`, so on a plain
            # reload(production) the star-import pulls values from the already
            # cached base module -- values computed from the environment as it
            # was at first import, not the one patched above. Anything defined
            # in base (EMAIL_BACKEND, FRONTEND_URL, ALLOWED_HOSTS) would then
            # ignore this test's environment entirely.
            importlib.reload(importlib.import_module("config.settings.base"))
            module = importlib.import_module("config.settings.production")
            return importlib.reload(module)

    def test_ssl_redirect_is_on_by_default(self):
        self.assertTrue(self._production_settings().SECURE_SSL_REDIRECT)

    def test_ssl_redirect_can_be_turned_off_explicitly(self):
        """The one legitimate case: nothing terminates TLS in front of nginx yet."""
        self.assertFalse(self._production_settings({"SECURE_SSL_REDIRECT": "false"}).SECURE_SSL_REDIRECT)

    def test_health_checks_are_exempt_from_the_redirect(self):
        """Otherwise every container healthcheck follows a 301 to a closed port."""
        settings_module = self._production_settings()
        self.assertIn(r"^health/", settings_module.SECURE_REDIRECT_EXEMPT)

    def test_hsts_is_configured(self):
        module = self._production_settings()
        self.assertGreaterEqual(module.SECURE_HSTS_SECONDS, 31536000)
        self.assertTrue(module.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertTrue(module.SECURE_HSTS_PRELOAD)

    def test_cookies_are_secure_and_http_only(self):
        module = self._production_settings()
        self.assertTrue(module.SESSION_COOKIE_SECURE)
        self.assertTrue(module.SESSION_COOKIE_HTTPONLY)
        self.assertTrue(module.CSRF_COOKIE_SECURE)
        self.assertTrue(module.CSRF_COOKIE_HTTPONLY)

    def test_response_headers_are_set(self):
        module = self._production_settings()
        self.assertTrue(module.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(module.X_FRAME_OPTIONS, "DENY")
        self.assertEqual(module.SECURE_REFERRER_POLICY, "strict-origin-when-cross-origin")

    def test_the_proxy_header_is_trusted(self):
        self.assertEqual(
            self._production_settings().SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_csrf_trusted_origins_are_derived_from_allowed_hosts(self):
        """
        Django 4+ refuses an unsafe cross-origin POST from an unlisted origin,
        and the admin login behind a proxy is exactly that -- so an empty list
        here is a broken admin, not merely a missing setting.
        """
        module = self._production_settings()
        self.assertTrue(module.CSRF_TRUSTED_ORIGINS)
        for origin in module.CSRF_TRUSTED_ORIGINS:
            with self.subTest(origin=origin):
                self.assertTrue(origin.startswith("https://"))
                self.assertIn(origin.removeprefix("https://"), module.ALLOWED_HOSTS)

    def test_an_explicit_origin_list_wins(self):
        module = self._production_settings({"CSRF_TRUSTED_ORIGINS": "https://console.example.com"})
        self.assertEqual(module.CSRF_TRUSTED_ORIGINS, ["https://console.example.com"])

    def test_development_is_not_forced_onto_https(self):
        """
        Local development runs on plain http, and hardening production must not
        have reached back into it -- SECURE_SSL_REDIRECT there would redirect
        every request to a port nothing is listening on.
        """
        import config.settings.development as development

        self.assertFalse(getattr(development, "SECURE_SSL_REDIRECT", False))
        self.assertFalse(getattr(settings, "SECURE_SSL_REDIRECT", False))


class DependencyConnectivityTests(TestCase):
    """
    The services are reachable from the application, over the internal network
    only. Postgres is genuinely exercised (this test's own database is it);
    Redis and the QR bridge are stubbed in the test settings by design, so what
    is checked here is that the wiring points at the internal names.
    """

    def test_the_database_is_reachable(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))

    def test_the_health_endpoint_reports_the_dependencies(self):
        response = self.client.get("/health/ready/")

        self.assertIn(response.status_code, (200, 503))
        self.assertIn("database", response.json()["checks"])
        self.assertIn("redis", response.json()["checks"])

    def test_the_database_check_passes(self):
        self.assertTrue(self.client.get("/health/ready/").json()["checks"]["database"])

    def test_the_cache_backend_is_usable(self):
        from django.core.cache import cache

        cache.set("deployment-probe", "value", timeout=5)
        self.assertEqual(cache.get("deployment-probe"), "value")
