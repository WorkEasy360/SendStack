"""
Password recovery, end to end, and the ways it can be turned against a customer.

The question this file answers is the first one in the Phase 3 brief: *what
happens if a customer forgets their password?* Each test corresponds to a way
the answer was previously "something worse than nothing".
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import Tenant

REQUEST_URL = "/api/v1/auth/password-reset/"
CONFIRM_URL = "/api/v1/auth/password-reset/confirm/"
CHANGE_URL = "/api/v1/auth/password-change/"
TOKEN_URL = "/api/v1/auth/token/"

NEW_PASSWORD = "a-brand-new-passphrase-9271"


class PasswordResetTestCase(TestCase):
    password = "correct-horse-battery-staple"

    def setUp(self):
        # Throttles, the per-email counter and the login lockout all live in
        # the cache; a test that inherits another's counters is a flaky test.
        cache.clear()
        self.addCleanup(cache.clear)
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-reset", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="ada", email="ada@example.com", password=self.password, tenant=self.tenant,
        )
        self.client = APIClient()

    def request_reset(self, email="ada@example.com"):
        return self.client.post(REQUEST_URL, {"email": email}, format="json")

    def link_parts(self):
        """The uid and token from the most recent reset email."""
        self.assertTrue(mail.outbox, "No reset email was sent.")
        body = mail.outbox[-1].body
        _, _, tail = body.partition("uid=")
        uid, _, rest = tail.partition("&token=")
        token = rest.split()[0].strip()
        return uid, token


class ResetRequestTests(PasswordResetTestCase):
    def test_a_reset_link_is_emailed(self):
        response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ada@example.com", mail.outbox[0].to)

    def test_the_email_carries_both_a_text_and_an_html_part(self):
        self.request_reset()
        message = mail.outbox[0]
        self.assertTrue(message.body.strip(), "The plain-text part is empty.")
        types = [content_type for _, content_type in message.alternatives]
        self.assertIn("text/html", types)

    def test_the_email_says_when_the_link_expires_and_warns_about_misuse(self):
        self.request_reset()
        body = mail.outbox[0].body.lower()
        self.assertIn("expires", body)
        self.assertIn("did not ask", body)

    def test_the_email_never_contains_a_password(self):
        self.request_reset()
        message = mail.outbox[0]
        haystack = message.body + "".join(part for part, _ in message.alternatives)
        self.assertNotIn(self.password, haystack)
        # Nor the stored hash, which is the other thing that must never travel.
        self.assertNotIn(self.user.password, haystack)

    def test_an_unknown_address_gets_the_same_response_and_no_email(self):
        """
        The enumeration property, which is the whole reason this endpoint is
        worded the way it is: a different status, a different body, or an
        email that arrives only for real accounts all answer "does this person
        have an account here" for anybody who asks.
        """
        known = self.request_reset("ada@example.com")
        cache.clear()
        unknown = self.request_reset("nobody@example.com")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)  # only the real account was mailed

    def test_the_response_does_not_say_whether_the_account_exists(self):
        detail = self.request_reset("nobody@example.com").json()["detail"].lower()
        for leak in ("not found", "does not exist", "no account", "unknown"):
            self.assertNotIn(leak, detail)

    def test_an_inactive_account_is_not_mailed_but_answers_the_same(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PASSWORD_RESET_MAX_PER_EMAIL=3)
    def test_one_address_cannot_be_mail_bombed(self):
        """
        The DRF throttle counts per client address, so somebody rotating
        addresses trips nothing and every request still sends a mail. This
        counts against the mailbox instead.
        """
        for _ in range(3):
            self.assertEqual(self.request_reset().status_code, 200)
        self.assertEqual(len(mail.outbox), 3)

        # Over the limit: still a 200 with the identical body -- the limit must
        # not become the oracle the generic response exists to prevent -- but
        # no further mail.
        response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 3)

    @override_settings(PASSWORD_RESET_MAX_PER_EMAIL=2)
    def test_the_per_email_limit_is_counted_per_address(self):
        other = get_user_model().objects.create_user(
            username="bob", email="bob@example.com", password=self.password, tenant=self.tenant,
        )
        for _ in range(3):
            self.request_reset("ada@example.com")
        self.assertEqual(len(mail.outbox), 2)

        # Ada's exhausted budget must not stop Bob recovering his account.
        self.request_reset("bob@example.com")
        self.assertEqual(len(mail.outbox), 3)
        self.assertIn(other.email, mail.outbox[-1].to)

    def test_the_endpoint_is_throttled_by_client_address_too(self):
        from apps.accounts.views import PasswordResetRequestView

        self.assertEqual(PasswordResetRequestView.throttle_scope, "auth")


class ResetConfirmTests(PasswordResetTestCase):
    def confirm(self, uid, token, password=NEW_PASSWORD, **extra):
        payload = {"uid": uid, "token": token, "new_password": password, **extra}
        return self.client.post(CONFIRM_URL, payload, format="json")

    def test_the_link_sets_a_new_password(self):
        self.request_reset()
        uid, token = self.link_parts()

        self.assertEqual(self.confirm(uid, token).status_code, 200)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_the_old_password_stops_working(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.confirm(uid, token)

        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(self.password))

        refused = self.client.post(
            TOKEN_URL, {"username": "ada", "password": self.password}, format="json",
        )
        self.assertEqual(refused.status_code, 401)

    def test_the_new_password_works_for_signing_in(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.confirm(uid, token)

        response = self.client.post(
            TOKEN_URL, {"username": "ada", "password": NEW_PASSWORD}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())

    def test_a_token_cannot_be_used_twice(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token).status_code, 200)

        # The second attempt fails because the token is derived from the
        # password hash, which the first attempt changed.
        again = self.confirm(uid, token, password="yet-another-passphrase-4410")
        self.assertEqual(again.status_code, 400)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_a_token_is_dead_after_any_later_password_change(self):
        """A link issued before a change must not still work after it."""
        self.request_reset()
        uid, token = self.link_parts()

        self.user.set_password("changed-out-of-band-8823")
        self.user.save(update_fields=["password"])

        self.assertEqual(self.confirm(uid, token).status_code, 400)

    def test_an_expired_token_is_refused(self):
        """
        A link nobody used must stop working. Django stamps the token with the
        moment it was made and compares against PASSWORD_RESET_TIMEOUT at check
        time, so the way to age a token is to make it in the past rather than
        to try to move the clock forward at the far end.
        """
        from django.contrib.auth.tokens import PasswordResetTokenGenerator

        real_now = PasswordResetTokenGenerator._now
        two_hours_ago = real_now(default_token_generator) - timedelta(hours=2)

        with mock.patch.object(PasswordResetTokenGenerator, "_now", return_value=two_hours_ago):
            stale = default_token_generator.make_token(self.user)

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))

        # PASSWORD_RESET_TIMEOUT is one hour (see settings), so a token made two
        # hours ago is past it.
        self.assertEqual(self.confirm(uid, stale).status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_the_reset_window_is_an_hour_not_djangos_three_day_default(self):
        """
        Django's own default is three days. A link that is a full account
        takeover for anybody who reads the mailbox should not sit there that
        long.
        """
        from django.conf import settings

        self.assertLessEqual(settings.PASSWORD_RESET_TIMEOUT, 60 * 60)

    def test_an_invalid_token_is_refused_without_revealing_why(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        response = self.confirm(uid, "not-a-real-token")
        self.assertEqual(response.status_code, 400)

        detail = response.json()["detail"].lower()
        self.assertIn("invalid or has expired", detail)
        # No hint about which half was wrong, and nothing about the mechanism.
        for leak in ("signature", "timestamp", "hash", "user id", "generator"):
            self.assertNotIn(leak, detail)

    def test_an_unknown_uid_is_refused(self):
        response = self.confirm(urlsafe_base64_encode(force_bytes(999999)), "whatever")
        self.assertEqual(response.status_code, 400)

    def test_a_malformed_uid_does_not_raise(self):
        self.assertEqual(self.confirm("!!!not-base64!!!", "whatever").status_code, 400)

    def test_a_weak_password_is_refused(self):
        self.request_reset()
        uid, token = self.link_parts()
        response = self.confirm(uid, token, password="password123")
        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_a_short_password_is_refused(self):
        self.request_reset()
        uid, token = self.link_parts()
        self.assertEqual(self.confirm(uid, token, password="short").status_code, 400)

    def test_a_mismatched_confirmation_is_refused(self):
        self.request_reset()
        uid, token = self.link_parts()
        response = self.confirm(uid, token, confirm_password="something-else-entirely")
        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))


class SessionRevocationTests(PasswordResetTestCase):
    """
    The property the audit asked for by name: a stolen refresh token must not
    outlive the password reset that was performed *because* it was stolen.
    """

    def test_a_reset_revokes_every_outstanding_refresh_token(self):
        stolen = RefreshToken.for_user(self.user)
        self.assertFalse(BlacklistedToken.objects.filter(token__user=self.user).exists())

        self.request_reset()
        uid, token = self.link_parts()
        self.confirm_ok(uid, token)

        outstanding = OutstandingToken.objects.filter(user=self.user).count()
        blacklisted = BlacklistedToken.objects.filter(token__user=self.user).count()
        self.assertEqual(blacklisted, outstanding)
        self.assertGreater(blacklisted, 0)

        # And the token itself is refused rather than merely recorded.
        response = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": str(stolen)}, format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_a_password_change_revokes_sessions_too(self):
        stolen = RefreshToken.for_user(self.user)
        self.client.force_authenticate(self.user)

        response = self.client.post(
            CHANGE_URL,
            {"current_password": self.password, "new_password": NEW_PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        refused = APIClient().post(
            "/api/v1/auth/token/refresh/", {"refresh": str(stolen)}, format="json",
        )
        self.assertEqual(refused.status_code, 401)

    def confirm_ok(self, uid, token):
        response = self.client.post(
            CONFIRM_URL, {"uid": uid, "token": token, "new_password": NEW_PASSWORD}, format="json",
        )
        self.assertEqual(response.status_code, 200)


class SecurityNotificationTests(PasswordResetTestCase):
    def test_a_completed_reset_emails_the_account(self):
        self.request_reset()
        uid, token = self.link_parts()
        mail.outbox.clear()

        self.client.post(
            CONFIRM_URL, {"uid": uid, "token": token, "new_password": NEW_PASSWORD}, format="json",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("changed", mail.outbox[0].subject.lower())

    def test_the_notification_carries_no_credential(self):
        self.request_reset()
        uid, token = self.link_parts()
        mail.outbox.clear()
        self.client.post(
            CONFIRM_URL, {"uid": uid, "token": token, "new_password": NEW_PASSWORD}, format="json",
        )

        message = mail.outbox[0]
        haystack = message.body + "".join(part for part, _ in message.alternatives)
        self.assertNotIn(NEW_PASSWORD, haystack)
        self.assertNotIn(self.password, haystack)
        self.assertNotIn(token, haystack)

    def test_a_mail_failure_does_not_fail_the_reset(self):
        """
        The password is already changed by the time the notification is sent.
        Reporting failure would leave somebody unable to sign in with either
        password, which is a far worse outcome than a missing email.
        """
        self.request_reset()
        uid, token = self.link_parts()

        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down"),
        ):
            response = self.client.post(
                CONFIRM_URL, {"uid": uid, "token": token, "new_password": NEW_PASSWORD}, format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))


class PasswordChangeTests(PasswordResetTestCase):
    def test_changing_requires_authentication(self):
        response = self.client.post(
            CHANGE_URL, {"current_password": self.password, "new_password": NEW_PASSWORD}, format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_the_current_password_is_required(self):
        """
        A stolen access token must not be enough to take the account
        permanently. Knowing the current password is the thing whoever holds
        that token does not.
        """
        self.client.force_authenticate(self.user)
        response = self.client.post(
            CHANGE_URL, {"current_password": "wrong", "new_password": NEW_PASSWORD}, format="json",
        )
        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_a_correct_current_password_changes_it(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            CHANGE_URL, {"current_password": self.password, "new_password": NEW_PASSWORD}, format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))

    def test_the_new_password_must_differ(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            CHANGE_URL, {"current_password": self.password, "new_password": self.password}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_the_new_password_is_validated(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            CHANGE_URL, {"current_password": self.password, "new_password": "password1234"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_a_password_too_like_the_account_is_refused(self):
        """
        UserAttributeSimilarityValidator compares the new password against the
        username and email, and can only do so when it is handed the user --
        which is why the serializer passes one. Without that, "adaexample.com"
        would sail past every other rule in the list.
        """
        self.client.force_authenticate(self.user)
        response = self.client.post(
            CHANGE_URL, {"current_password": self.password, "new_password": "adaexample.com"}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_the_link_in_the_plain_text_email_is_not_html_escaped(self):
        """
        The regression: Django autoescapes every template, extension included,
        so the "&" joining uid and token became "&amp;" in the text part. A
        recipient pasting that link sends a parameter called "amp;token", and
        the reset fails as invalid -- for somebody who did everything right.
        """
        self.client.post(REQUEST_URL, {"email": "ada@example.com"}, format="json")
        body = mail.outbox[0].body
        self.assertIn("&token=", body)
        self.assertNotIn("&amp;", body)
