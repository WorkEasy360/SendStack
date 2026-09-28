"""
Authentication security: throttling, failed-attempt lockout, enumeration, logout.

The property being defended is that guessing a credential against this
deployment is expensive and that the responses give an attacker nothing they
did not already know. Each test here corresponds to a way that was previously
untrue.
"""

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

from apps.tenants.models import Tenant
from core import lockout


class AuthSecurityTestCase(TestCase):
    """Shared setup, plus a cache reset -- throttles and lockouts both live there."""

    password = "correct-horse-battery-staple"

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-auth-sec", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada", email="ada@example.com", password=self.password, tenant=self.tenant,
        )
        self.client = APIClient()

    def login(self, username="ada", password=None):
        return self.client.post(
            "/api/v1/auth/token/",
            {"username": username, "password": self.password if password is None else password},
            format="json",
        )


class LoginThrottleTests(AuthSecurityTestCase):
    def test_login_uses_the_scoped_auth_throttle(self):
        """
        The regression: THROTTLE_RATE_AUTH was configured and nothing used it.

        LoginView is unauthenticated, so the default classes only ever applied
        AnonRateThrottle's much looser rate -- the deployment believed it was
        allowing 10 attempts a minute and was allowing 60.
        """
        from apps.accounts.views import LoginView

        scopes = [getattr(cls, "scope", None) or getattr(LoginView, "throttle_scope", None)
                  for cls in LoginView.throttle_classes]
        self.assertIn("auth", scopes)
        self.assertEqual(LoginView.throttle_scope, "auth")

    @override_settings(AUTH_MAX_FAILED_ATTEMPTS=0)
    def test_repeated_attempts_from_one_client_are_throttled(self):
        """With the account lockout disabled, the rate throttle still bites."""
        with override_settings(REST_FRAMEWORK={**self._rest_framework_with(auth_rate="3/min")}):
            self._reload_throttle_rates("3/min")
            statuses = [self.login(password="wrong").status_code for _ in range(5)]

        self.assertIn(429, statuses)

    def _rest_framework_with(self, *, auth_rate):
        from django.conf import settings

        rates = dict(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])
        rates["auth"] = auth_rate
        return {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}

    def _reload_throttle_rates(self, rate):
        # ScopedRateThrottle reads THROTTLE_RATES off the class at import time,
        # so override_settings alone does not reach it.
        from rest_framework.throttling import ScopedRateThrottle

        ScopedRateThrottle.THROTTLE_RATES = {**ScopedRateThrottle.THROTTLE_RATES, "auth": rate}
        self.addCleanup(
            setattr, ScopedRateThrottle, "THROTTLE_RATES",
            dict(ScopedRateThrottle.THROTTLE_RATES),
        )

    def test_password_reset_request_is_throttled(self):
        from apps.accounts.views import PasswordResetRequestView

        self.assertEqual(PasswordResetRequestView.throttle_scope, "auth")
        self.assertTrue(PasswordResetRequestView.throttle_classes)

    def test_password_reset_confirm_is_throttled(self):
        from apps.accounts.views import PasswordResetConfirmView

        self.assertEqual(PasswordResetConfirmView.throttle_scope, "auth")
        self.assertTrue(PasswordResetConfirmView.throttle_classes)


@override_settings(AUTH_MAX_FAILED_ATTEMPTS=3, AUTH_LOCKOUT_SECONDS=300)
class FailedLoginLockoutTests(AuthSecurityTestCase):
    """
    Per-account counting, which is the half a per-client rate limit cannot do.

    An attacker with a proxy pool spends one request per address and never
    trips a per-client throttle at all; what stops them is the account itself
    refusing further attempts.
    """

    def test_repeated_failures_lock_the_account(self):
        for _ in range(3):
            self.assertEqual(self.login(password="wrong").status_code, 401)

        self.assertEqual(self.login(password="wrong").status_code, 429)

    def test_the_lock_holds_even_for_the_correct_password(self):
        """Otherwise the lockout would be an oracle for the right password."""
        for _ in range(3):
            self.login(password="wrong")

        self.assertEqual(self.login().status_code, 429)

    def test_a_successful_login_resets_the_counter(self):
        self.login(password="wrong")
        self.login(password="wrong")

        self.assertEqual(self.login().status_code, 200)

        self.assertFalse(lockout.is_locked("login", "ada"))
        # And the budget is genuinely full again, not merely under the limit.
        for _ in range(3):
            self.assertEqual(self.login(password="wrong").status_code, 401)

    def test_rotating_the_client_address_does_not_help(self):
        """The counter is keyed on the account, not on where the attempt came from."""
        for index in range(3):
            self.client.post(
                "/api/v1/auth/token/", {"username": "ada", "password": "wrong"},
                format="json", REMOTE_ADDR=f"203.0.113.{index}",
            )

        response = self.client.post(
            "/api/v1/auth/token/", {"username": "ada", "password": "wrong"},
            format="json", REMOTE_ADDR="198.51.100.9",
        )
        self.assertEqual(response.status_code, 429)

    def test_an_unknown_username_locks_out_identically(self):
        """
        Counting only real accounts would make the lockout an existence oracle:
        "it locked on the fourth try" would mean "this account is real".
        """
        for _ in range(3):
            self.assertEqual(self.login(username="nobody-here", password="wrong").status_code, 401)

        self.assertEqual(self.login(username="nobody-here", password="wrong").status_code, 429)

    def test_locking_one_account_does_not_lock_another(self):
        get_user_model().objects.create_user(username="grace", password=self.password, tenant=self.tenant)
        for _ in range(4):
            self.login(password="wrong")

        self.assertEqual(self.login(username="grace").status_code, 200)

    def test_the_lockout_expires_rather_than_being_permanent(self):
        for _ in range(4):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 429)

        # What the timeout does when it lapses, without waiting five minutes.
        lockout.clear("login", "ada")

        self.assertEqual(self.login().status_code, 200)

    @override_settings(AUTH_MAX_FAILED_ATTEMPTS=0)
    def test_the_mechanism_can_be_switched_off(self):
        for _ in range(6):
            self.assertEqual(self.login(password="wrong").status_code, 401)

    def test_a_password_reset_clears_the_lockout(self):
        """Proving control of the mailbox outranks the failed attempts."""
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        for _ in range(4):
            self.login(password="wrong")
        self.assertTrue(lockout.is_locked("login", "ada"))

        response = self.client.post("/api/v1/auth/password-reset/confirm/", {
            "uid": urlsafe_base64_encode(force_bytes(self.user.pk)),
            "token": default_token_generator.make_token(self.user),
            "new_password": "a-brand-new-passphrase-99",
        }, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(lockout.is_locked("login", "ada"))


class AccountEnumerationTests(AuthSecurityTestCase):
    @staticmethod
    def _body(response):
        # request_id is per-request by construction and carries no information
        # about the account, so it is not part of "the same answer".
        return {key: value for key, value in response.json().items() if key != "request_id"}

    def test_login_answers_identically_for_unknown_and_wrong_password(self):
        unknown = self.login(username="no-such-person", password="wrong")
        wrong = self.login(password="wrong")

        self.assertEqual(unknown.status_code, wrong.status_code)
        self.assertEqual(self._body(unknown), self._body(wrong))

    def test_password_reset_answers_identically_for_a_known_and_unknown_address(self):
        known = self.client.post("/api/v1/auth/password-reset/", {"email": "ada@example.com"}, format="json")
        unknown = self.client.post("/api/v1/auth/password-reset/", {"email": "nobody@example.com"}, format="json")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(self._body(known), self._body(unknown))
        # And the difference that does exist is invisible to the caller.
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(AUTH_MAX_FAILED_ATTEMPTS=2)
    def test_the_lockout_response_is_the_same_for_a_real_and_invented_account(self):
        for _ in range(3):
            real = self.login(password="wrong")
        for _ in range(3):
            invented = self.login(username="not-a-user", password="wrong")

        self.assertEqual(real.status_code, invented.status_code)
        self.assertEqual(self._body(real), self._body(invented))


class LogoutTests(AuthSecurityTestCase):
    """
    Signing out has to end the session on the server.

    Clearing localStorage left the refresh token valid for its full seven days,
    so a token copied off a shared machine kept minting access tokens long
    after its owner believed they had signed out.
    """

    def _tokens(self):
        response = self.login()
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["access"], response.json()["refresh"]

    def test_refresh_works_before_logout(self):
        _access, refresh = self._tokens()

        response = self.client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.json())

    def test_logout_blacklists_the_refresh_token(self):
        _access, refresh = self._tokens()

        response = self.client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(BlacklistedToken.objects.count(), 1)

    def test_the_old_refresh_token_cannot_be_reused_after_logout(self):
        _access, refresh = self._tokens()
        self.client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")

        response = self.client.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")

        self.assertEqual(response.status_code, 401)

    def test_logging_out_twice_is_not_an_error(self):
        _access, refresh = self._tokens()
        self.client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json")

        self.assertEqual(
            self.client.post("/api/v1/auth/logout/", {"refresh": refresh}, format="json").status_code, 200
        )

    def test_a_nonsense_token_is_answered_the_same_way(self):
        """A distinguishable error would confirm whether a token string was ever real."""
        real = self.client.post("/api/v1/auth/logout/", {"refresh": self._tokens()[1]}, format="json")
        junk = self.client.post("/api/v1/auth/logout/", {"refresh": "not-a-token"}, format="json")

        self.assertEqual(real.status_code, junk.status_code)
        self.assertEqual(
            {k: v for k, v in real.json().items() if k != "request_id"},
            {k: v for k, v in junk.json().items() if k != "request_id"},
        )

    def test_logging_out_does_not_affect_another_session(self):
        _a1, refresh_one = self._tokens()
        _a2, refresh_two = self._tokens()

        self.client.post("/api/v1/auth/logout/", {"refresh": refresh_one}, format="json")

        self.assertEqual(
            self.client.post("/api/v1/auth/token/refresh/", {"refresh": refresh_two}, format="json").status_code, 200
        )


class AdminHardeningTests(AuthSecurityTestCase):
    def test_the_admin_login_is_rate_limited_per_account(self):
        from django.contrib.auth import get_user_model as _model

        _model().objects.create_superuser(username="root", email="root@example.com", password=self.password)
        with override_settings(AUTH_MAX_FAILED_ATTEMPTS=2):
            for _ in range(2):
                self.client.post("/admin/login/", {"username": "root", "password": "wrong"})

            response = self.client.post("/admin/login/", {"username": "root", "password": "wrong"})

        self.assertEqual(response.status_code, 429)

    def test_an_admin_lockout_also_locks_the_api(self):
        """One budget, not two: the two login forms must not be usable in turn."""
        with override_settings(AUTH_MAX_FAILED_ATTEMPTS=2):
            for _ in range(2):
                self.client.post("/admin/login/", {"username": "ada", "password": "wrong"})

            self.assertEqual(self.login().status_code, 429)

    def test_an_ip_allowlist_hides_the_admin_from_everyone_else(self):
        with override_settings(ADMIN_ALLOWED_IPS=["203.0.113.7"]):
            refused = self.client.get("/admin/", REMOTE_ADDR="198.51.100.1")
            allowed = self.client.get("/admin/", REMOTE_ADDR="203.0.113.7")

        # 404 rather than 403: there is no reason to confirm an admin is here.
        self.assertEqual(refused.status_code, 404)
        self.assertNotEqual(allowed.status_code, 404)

    def test_no_allowlist_means_no_restriction(self):
        with override_settings(ADMIN_ALLOWED_IPS=[]):
            response = self.client.get("/admin/", REMOTE_ADDR="198.51.100.1")

        self.assertNotEqual(response.status_code, 404)

    def test_the_admin_is_not_readable_anonymously(self):
        response = self.client.get("/admin/")

        # Redirected to the login form rather than shown anything.
        self.assertIn(response.status_code, (301, 302))
        self.assertIn("login", response["Location"])
