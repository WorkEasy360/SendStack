"""
Tests for the key system, authentication and scopes.

The through-line: every one of these is about a *refusal*. A messaging API's
authentication layer is only as good as the things it says no to, and each of
these pins one of them -- an expired key, a revoked key, a key from another
workspace, a key without the scope, a key whose owner was deactivated.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import keys as key_utils
from . import scopes
from .models import ApiKey, ApiRequestLog
from .services import create_api_key, revoke_api_key, rotate_api_key, set_api_key_active


def make_tenant(slug, name=None):
    return Tenant.objects.create(name=name or slug, slug=slug, kind=Tenant.Kind.CUSTOMER)


def make_user(tenant, username, credits=1000):
    return get_user_model().objects.create_user(
        username=username, password="x", tenant=tenant, credits=credits,
    )


def make_account(tenant, user, name="Line", status=WhatsAppAccount.Status.CONNECTED):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name=name, phone_number_id="1", status=status,
        phone_number="+919162353602",
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


def make_key(tenant, owner, *, scope_list=None, **kwargs):
    return create_api_key(
        tenant=tenant, owner=owner, name=kwargs.pop("name", "Test key"),
        scopes=scope_list if scope_list is not None else list(scopes.ALL_SCOPES),
        **kwargs,
    )


# HTTPS is required by default outside DEBUG; the test client speaks plain HTTP.
# Turned off here rather than making every request `secure=True`, so that the
# one test that *does* care about HTTPS is unmistakably about HTTPS.
API = override_settings(PUBLIC_API_REQUIRE_HTTPS=False)


class KeyGenerationTests(TestCase):
    def test_generated_key_round_trips(self):
        full, prefix, hashed = key_utils.generate("live")

        self.assertTrue(full.startswith("ss_live_"))
        parsed = key_utils.parse(full)
        self.assertIsNotNone(parsed)
        environment, parsed_prefix, secret = parsed
        self.assertEqual(environment, "live")
        self.assertEqual(parsed_prefix, prefix)
        self.assertTrue(key_utils.verify(secret, hashed))

    def test_secret_is_never_stored(self):
        tenant = make_tenant("keys-store")
        api_key, full = make_key(tenant, make_user(tenant, "keys-store-owner"))

        # The stored hash must not contain, and must not be, the secret.
        secret = key_utils.parse(full)[2]
        self.assertNotIn(secret, api_key.hashed_secret)
        self.assertNotEqual(api_key.hashed_secret, secret)
        self.assertEqual(api_key.hashed_secret, key_utils.hash_secret(secret))
        # And nothing on the row can reconstruct it.
        self.assertNotIn(secret, str(api_key.__dict__))

    def test_two_keys_never_collide(self):
        prefixes = {key_utils.generate("live")[1] for _ in range(200)}
        self.assertEqual(len(prefixes), 200)

    def test_parse_rejects_a_jwt(self):
        self.assertIsNone(key_utils.parse("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"))

    def test_parse_rejects_a_truncated_key(self):
        full, _, _ = key_utils.generate("live")
        self.assertIsNone(key_utils.parse(full[:-4]))

    def test_test_environment_has_its_own_prefix(self):
        full, _, _ = key_utils.generate("test")
        self.assertTrue(full.startswith("ss_test_"))
        self.assertEqual(key_utils.parse(full)[0], "test")


class ScopeValidationTests(TestCase):
    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            scopes.validate(["messages:send", "messages:destroy"])
        self.assertIn("messages:destroy", str(caught.exception))

    def test_scopes_are_normalised_to_canonical_order(self):
        result = scopes.validate([scopes.REPORTS_READ, scopes.MESSAGES_SEND, scopes.MESSAGES_SEND])
        self.assertEqual(result, [scopes.MESSAGES_SEND, scopes.REPORTS_READ])


@API
class AuthenticationTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("auth-tenant")
        self.user = make_user(self.tenant, "auth-owner")
        self.api_key, self.full_key = make_key(self.tenant, self.user)

    def get(self, path, key=None):
        headers = {}
        if key is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {key}"
        return self.client.get(path, **headers)

    def test_valid_key_is_accepted(self):
        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["workspace"], self.tenant.name)

    def test_missing_credential_is_401(self):
        response = self.get("/api/public/v1/ping")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_API_KEY")

    def test_wrong_secret_is_401(self):
        forged = f"ss_live_{self.api_key.key_prefix}{'0' * 32}"

        response = self.get("/api/public/v1/ping", forged)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_API_KEY")

    def test_a_jwt_is_not_accepted_here(self):
        from rest_framework_simplejwt.tokens import AccessToken

        response = self.get("/api/public/v1/ping", str(AccessToken.for_user(self.user)))

        # The point of this test: the SPA's own credential must not open the
        # public API, even though both arrive as `Authorization: Bearer`.
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_API_KEY")

    def test_session_authentication_does_not_work(self):
        self.client.force_login(self.user)

        response = self.client.get("/api/public/v1/ping")

        self.assertEqual(response.status_code, 401)

    def test_expired_key_says_so(self):
        ApiKey.objects.filter(pk=self.api_key.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "EXPIRED_API_KEY")

    def test_revoked_key_says_so(self):
        revoke_api_key(self.api_key)

        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "REVOKED_API_KEY")

    def test_disabled_key_says_so(self):
        set_api_key_active(self.api_key, False)

        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "DISABLED_API_KEY")

    def test_suspended_workspace_loses_access_immediately(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(is_active=False)

        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "WORKSPACE_SUSPENDED")

    def test_deactivated_owner_invalidates_the_key(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self.get("/api/public/v1/ping", self.full_key)

        self.assertEqual(response.status_code, 401)

    def test_malformed_authorization_header(self):
        response = self.client.get(
            "/api/public/v1/ping", HTTP_AUTHORIZATION=self.full_key,  # no "Bearer "
        )

        self.assertEqual(response.status_code, 401)

    def test_usage_is_recorded(self):
        self.get("/api/public/v1/ping", self.full_key)

        self.api_key.refresh_from_db()
        self.assertEqual(self.api_key.request_count, 1)
        self.assertIsNotNone(self.api_key.last_used_at)

        log = ApiRequestLog.objects.get()
        self.assertEqual(log.path, "/api/public/v1/ping")
        self.assertEqual(log.status_code, 200)
        self.assertEqual(log.tenant_id, self.tenant.pk)

    def test_request_log_never_stores_the_key(self):
        self.get("/api/public/v1/ping", self.full_key)

        log = ApiRequestLog.objects.get()
        secret = key_utils.parse(self.full_key)[2]
        self.assertNotIn(secret, str(log.__dict__))
        self.assertEqual(log.key_prefix, self.api_key.key_prefix)


@API
class ScopeEnforcementTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("scope-tenant")
        self.user = make_user(self.tenant, "scope-owner")

    def test_endpoint_refuses_a_key_without_its_scope(self):
        _, full = make_key(self.tenant, self.user, scope_list=[scopes.MESSAGES_SEND])

        response = self.client.get(
            "/api/public/v1/accounts", HTTP_AUTHORIZATION=f"Bearer {full}",
        )

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["error"]["code"], "INSUFFICIENT_PERMISSION")
        self.assertEqual(body["error"]["missing_scopes"], [scopes.ACCOUNTS_READ])

    def test_endpoint_accepts_a_key_with_exactly_its_scope(self):
        _, full = make_key(self.tenant, self.user, scope_list=[scopes.ACCOUNTS_READ])

        response = self.client.get(
            "/api/public/v1/accounts", HTTP_AUTHORIZATION=f"Bearer {full}",
        )

        self.assertEqual(response.status_code, 200)

    def test_read_scope_cannot_write(self):
        _, full = make_key(self.tenant, self.user, scope_list=[scopes.TEMPLATES_READ])

        response = self.client.post(
            "/api/public/v1/templates",
            data={"name": "x", "body": "hi"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {full}",
        )

        self.assertEqual(response.status_code, 403)

    def test_ping_needs_no_scope(self):
        _, full = make_key(self.tenant, self.user, scope_list=[scopes.REPORTS_READ])

        response = self.client.get(
            "/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {full}",
        )

        self.assertEqual(response.status_code, 200)


@API
class TenantIsolationTests(TestCase):
    """
    The property this whole app exists to preserve.

    Each of these makes a real object in tenant B and proves tenant A's key
    cannot see it, cannot address it, and -- crucially -- cannot tell from the
    response that it exists at all.
    """

    def setUp(self):
        self.tenant_a = make_tenant("iso-a", "Workspace A")
        self.user_a = make_user(self.tenant_a, "iso-a-owner")
        self.account_a = make_account(self.tenant_a, self.user_a, name="A line")
        _, self.key_a = make_key(self.tenant_a, self.user_a)

        self.tenant_b = make_tenant("iso-b", "Workspace B")
        self.user_b = make_user(self.tenant_b, "iso-b-owner")
        self.account_b = make_account(self.tenant_b, self.user_b, name="B line")

    def as_a(self, method, path, **kwargs):
        return getattr(self.client, method)(
            path, HTTP_AUTHORIZATION=f"Bearer {self.key_a}", **kwargs
        )

    def test_account_list_shows_only_own_accounts(self):
        response = self.as_a("get", "/api/public/v1/accounts")

        ids = [account["id"] for account in response.json()["accounts"]]
        self.assertEqual(ids, [f"account_{self.account_a.pk}"])

    def test_cannot_read_another_tenants_account(self):
        response = self.as_a("get", f"/api/public/v1/accounts/account_{self.account_b.pk}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

    def test_cannot_read_another_tenants_message(self):
        from apps.messaging.models import Message

        message = Message.objects.create(
            tenant=self.tenant_b, whatsapp_account=self.account_b,
            to_phone="+919999999999", body="secret",
        )

        response = self.as_a("get", f"/api/public/v1/messages/msg_{message.pk}")

        # 404, not 403: a 403 would confirm the id exists.
        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_tenants_template(self):
        from apps.templates_app.models import MessageTemplate

        template = MessageTemplate.objects.create(
            tenant=self.tenant_b, name="b_template", body="hi",
        )

        response = self.as_a("get", f"/api/public/v1/templates/tpl_{template.pk}")

        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_tenants_campaign(self):
        from apps.campaigns.models import Campaign

        campaign = Campaign.objects.create(
            tenant=self.tenant_b, created_by=self.user_b, name="B campaign",
            whatsapp_account=self.account_b, message_body="hi",
        )

        response = self.as_a("get", f"/api/public/v1/campaigns/cmp_{campaign.pk}")

        self.assertEqual(response.status_code, 404)

    def test_cannot_read_another_tenants_contact(self):
        from apps.contacts.models import Contact

        contact = Contact.objects.create(
            tenant=self.tenant_b, created_by=self.user_b, phone="+919111111111",
        )

        response = self.as_a("get", f"/api/public/v1/contacts/cnt_{contact.pk}")

        self.assertEqual(response.status_code, 404)

    def test_cannot_send_from_another_tenants_account(self):
        response = self.as_a(
            "post", "/api/public/v1/messages/send",
            data={"to": "919999999999", "text": "hi", "account_id": f"account_{self.account_b.pk}"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "ACCOUNT_NOT_FOUND")

    def test_cannot_read_another_tenants_webhook(self):
        from .models import WebhookEndpoint

        endpoint = WebhookEndpoint.objects.create(
            tenant=self.tenant_b, url="https://b.example.com/hook",
        )

        response = self.as_a("get", f"/api/public/v1/webhooks/{endpoint.pk}")

        self.assertEqual(response.status_code, 404)


@API
class AllowedAccountsTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("allowed-tenant")
        self.user = make_user(self.tenant, "allowed-owner")
        self.permitted = make_account(self.tenant, self.user, name="Permitted")
        self.other = make_account(self.tenant, self.user, name="Other")
        _, self.full = make_key(
            self.tenant, self.user, allowed_account_ids=[self.permitted.pk],
        )

    def test_list_hides_accounts_outside_the_allowlist(self):
        response = self.client.get(
            "/api/public/v1/accounts", HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

        ids = [account["id"] for account in response.json()["accounts"]]
        self.assertEqual(ids, [f"account_{self.permitted.pk}"])

    def test_cannot_send_from_an_account_outside_the_allowlist(self):
        response = self.client.post(
            "/api/public/v1/messages/send",
            data={"to": "919999999999", "text": "hi", "account_id": f"account_{self.other.pk}"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

        self.assertEqual(response.status_code, 404)


class KeyLifecycleTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("lifecycle-tenant")
        self.user = make_user(self.tenant, "lifecycle-owner")

    def test_rotation_revokes_the_old_key_and_keeps_the_row(self):
        original, original_full = make_key(self.tenant, self.user, name="Shopify")

        replacement, replacement_full = rotate_api_key(original, actor=self.user)
        original.refresh_from_db()

        self.assertIsNotNone(original.revoked_at)
        self.assertFalse(original.is_active)
        self.assertEqual(replacement.rotated_from_id, original.pk)
        self.assertEqual(replacement.name, original.name)
        self.assertEqual(replacement.scopes, original.scopes)
        self.assertNotEqual(replacement_full, original_full)
        # The old row survives, because the request log points at it.
        self.assertTrue(ApiKey.objects.filter(pk=original.pk).exists())

    def test_rotation_carries_the_allowlist_over(self):
        account = make_account(self.tenant, self.user)
        original, _ = make_key(self.tenant, self.user, allowed_account_ids=[account.pk])

        replacement, _ = rotate_api_key(original, actor=self.user)

        self.assertEqual(
            list(replacement.allowed_accounts.values_list("pk", flat=True)), [account.pk],
        )

    def test_revoked_key_cannot_be_re_enabled(self):
        from .errors import PublicAPIError

        api_key, _ = make_key(self.tenant, self.user)
        revoke_api_key(api_key)
        api_key.refresh_from_db()

        with self.assertRaises(PublicAPIError):
            set_api_key_active(api_key, True)

    def test_status_reports_the_refusal_reason(self):
        api_key, _ = make_key(self.tenant, self.user)
        self.assertEqual(api_key.status, "active")

        set_api_key_active(api_key, False)
        api_key.refresh_from_db()
        self.assertEqual(api_key.status, "disabled")

        ApiKey.objects.filter(pk=api_key.pk).update(
            is_active=True, expires_at=timezone.now() - timedelta(days=1),
        )
        api_key.refresh_from_db()
        self.assertEqual(api_key.status, "expired")

        revoke_api_key(api_key)
        api_key.refresh_from_db()
        # Revoked outranks expired: it is the one somebody did on purpose.
        self.assertEqual(api_key.status, "revoked")

    def test_key_cannot_be_created_with_another_tenants_account(self):
        from .errors import PublicAPIError

        other_tenant = make_tenant("lifecycle-other")
        other_user = make_user(other_tenant, "lifecycle-other-owner")
        foreign = make_account(other_tenant, other_user)

        with self.assertRaises(PublicAPIError):
            make_key(self.tenant, self.user, allowed_account_ids=[foreign.pk])


@API
class HttpsEnforcementTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("https-tenant")
        self.user = make_user(self.tenant, "https-owner")
        _, self.full = make_key(self.tenant, self.user)

    @override_settings(PUBLIC_API_REQUIRE_HTTPS=True)
    def test_plaintext_is_refused_when_required(self):
        response = self.client.get(
            "/api/public/v1/ping", HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "HTTPS_REQUIRED")

    @override_settings(PUBLIC_API_REQUIRE_HTTPS=True)
    def test_https_is_allowed_when_required(self):
        response = self.client.get(
            "/api/public/v1/ping", secure=True, HTTP_AUTHORIZATION=f"Bearer {self.full}",
        )

        self.assertEqual(response.status_code, 200)


@API
class ErrorEnvelopeTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("errors-tenant")
        self.user = make_user(self.tenant, "errors-owner")
        make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def test_validation_failure_uses_the_envelope(self):
        response = self.client.post(
            "/api/public/v1/messages/send",
            data={"text": "no recipient"},
            content_type="application/json",
            **self.auth(),
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "INVALID_REQUEST")
        self.assertIn("to", body["error"]["details"])

    def test_404_uses_the_envelope(self):
        response = self.client.get("/api/public/v1/messages/msg_999999", **self.auth())

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")

    def test_method_not_allowed_uses_the_envelope(self):
        response = self.client.delete("/api/public/v1/accounts", **self.auth())

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"], "METHOD_NOT_ALLOWED")

    def test_every_response_carries_a_request_id(self):
        response = self.client.get("/api/public/v1/ping", **self.auth())

        self.assertIn("X-Request-Id", response)

    def test_error_codes_are_all_documented(self):
        from .errors import ERROR_CODES

        # A code raised but not documented is a code a developer cannot look
        # up. This pins the two together.
        import inspect

        from . import errors

        raised = {
            value for name, value in inspect.getmembers(errors)
            if name.isupper() and isinstance(value, str) and name != "HEADER"
        }
        self.assertTrue(raised.issubset(set(ERROR_CODES) | {""}))


@API
class DocumentationTests(TestCase):
    def test_openapi_schema_is_public_and_valid(self):
        response = self.client.get("/api/public/v1/openapi.json")

        self.assertEqual(response.status_code, 200)
        spec = response.json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/messages/send", spec["paths"])
        self.assertIn("ApiKeyAuth", spec["components"]["securitySchemes"])

    def test_schema_documents_every_scope_and_event(self):
        spec = self.client.get("/api/public/v1/openapi.json").json()

        from .events import ALL_EVENT_TYPES

        self.assertEqual(
            {entry["name"] for entry in spec["x-scopes"]}, set(scopes.ALL_SCOPES),
        )
        self.assertEqual(set(spec["webhooks"]), set(ALL_EVENT_TYPES))

    def test_docs_page_renders(self):
        response = self.client.get("/api/public/v1/docs")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SendStack Messaging API")
        self.assertContains(response, "X-SendStack-Signature")


@API
class ReportsTests(TestCase):
    def setUp(self):
        self.tenant = make_tenant("rep-tenant")
        self.user = make_user(self.tenant, "rep-owner")
        self.account = make_account(self.tenant, self.user)
        _, self.full = make_key(self.tenant, self.user)

    def auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.full}"}

    def test_summary_reports_the_workspace(self):
        from apps.messaging.models import Message

        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account,
            to_phone="+919999999999", body="hi", status=Message.Status.DELIVERED,
        )

        response = self.client.get("/api/public/v1/reports/summary", **self.auth())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["messages"]["delivered"], 1)
        self.assertEqual(body["accounts"]["connected"], 1)

    def test_summary_does_not_count_another_workspace(self):
        from apps.messaging.models import Message

        other = make_tenant("rep-other")
        other_user = make_user(other, "rep-other-owner")
        other_account = make_account(other, other_user)
        Message.objects.create(
            tenant=other, whatsapp_account=other_account,
            to_phone="+919999999999", body="hi", status=Message.Status.DELIVERED,
        )

        response = self.client.get("/api/public/v1/reports/summary", **self.auth())

        self.assertEqual(response.json()["messages"]["delivered"], 0)

    def test_unknown_range_is_a_400_not_a_silent_all_time(self):
        response = self.client.get("/api/public/v1/reports/summary?range=last_year", **self.auth())

        # Falling through to all-time would hand back the workspace's entire
        # history under a label the caller did not ask for.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_daily_breakdown(self):
        response = self.client.get("/api/public/v1/reports/daily?range=last_7_days", **self.auth())

        self.assertEqual(response.status_code, 200)
        self.assertIn("days", response.json())

    def test_reports_need_the_scope(self):
        _, limited = make_key(self.tenant, self.user, scope_list=[scopes.MESSAGES_SEND])

        response = self.client.get(
            "/api/public/v1/reports/summary", HTTP_AUTHORIZATION=f"Bearer {limited}",
        )

        self.assertEqual(response.status_code, 403)


class ScopeCoverageTests(TestCase):
    """
    Every declared scope is required by at least one endpoint.

    A scope nobody asks for is dead configuration: it appears as a checkbox on
    the create-key screen, a customer ticks it believing it grants something,
    and it grants nothing. This is how `reports:read` sat unused after the
    scope list was written before the endpoints were.

    Fully generic. It reads `required_scopes`, `method_scopes` and
    `ACTION_SCOPES` off the resolved views, so a scope assigned only inside a
    method body would *fail* this test rather than being quietly excused by a
    hardcoded list here -- which is exactly why those assignments were made
    declarative.
    """

    def collected_scopes(self):
        from django.urls import get_resolver

        found = set()

        def walk(patterns):
            for entry in patterns:
                nested = getattr(entry, "url_patterns", None)
                if nested is not None:
                    walk(nested)
                    continue
                view = getattr(entry.callback, "cls", None) or getattr(
                    entry.callback, "view_class", None,
                )
                if view is None:
                    continue
                found.update(getattr(view, "required_scopes", None) or [])
                for per_method in (getattr(view, "method_scopes", None) or {}).values():
                    found.update(per_method)
                found.update((getattr(view, "ACTION_SCOPES", None) or {}).values())

        walk(get_resolver().url_patterns)
        return found

    def test_no_scope_is_unused(self):
        unused = set(scopes.ALL_SCOPES) - self.collected_scopes()

        self.assertEqual(
            unused, set(),
            f"These scopes are offered but no endpoint requires them: {sorted(unused)}",
        )

    def test_no_endpoint_requires_an_undeclared_scope(self):
        """The other direction: a typo'd scope name would deny everything."""
        undeclared = self.collected_scopes() - set(scopes.ALL_SCOPES)

        self.assertEqual(
            undeclared, set(),
            f"These endpoints require scopes that do not exist: {sorted(undeclared)}",
        )
