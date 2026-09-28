"""
Tests for Settings -> Developer, the JWT-authenticated management screens.

The important ones are the two separations: an API key cannot reach these
endpoints (a key that could mint another key would make revocation
meaningless), and a secret is returned exactly once by the endpoint that
creates it and never by a read.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.audit.models import AuditEvent

from . import scopes
from .models import ApiKey, WebhookEndpoint
from .tests import make_account, make_key, make_tenant, make_user


class DashboardClientMixin:
    def signed_in(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client


class ApiKeyDashboardTests(DashboardClientMixin, TestCase):
    def setUp(self):
        self.tenant = make_tenant("dash-tenant")
        self.user = make_user(self.tenant, "dash-owner")
        self.client = self.signed_in(self.user)

    def test_create_returns_the_key_once(self):
        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Shopify store",
            "scopes": [scopes.MESSAGES_SEND, scopes.MESSAGES_STATUS],
        }, format="json")

        self.assertEqual(response.status_code, 201)
        body = response.data
        self.assertTrue(body["key"].startswith("ss_live_"))
        self.assertIn("key_notice", body)

        # And never again on a read.
        listed = self.client.get("/api/v1/developer/api-keys/")
        row = listed.data["results"][0]
        self.assertNotIn("key", row)
        self.assertNotIn("hashed_secret", row)
        self.assertTrue(row["masked_key"].endswith("…"))

    def test_create_writes_an_audit_event_without_the_key(self):
        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Zapier", "scopes": [scopes.MESSAGES_SEND],
        }, format="json")
        full_key = response.data["key"]

        event = AuditEvent.objects.get(action="api_key.created")
        self.assertEqual(event.actor_id, self.user.pk)
        self.assertNotIn(full_key, str(event.metadata))
        self.assertEqual(event.metadata["prefix"], response.data["key_prefix"])

    def test_unknown_scope_is_rejected(self):
        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Bad", "scopes": ["messages:destroy"],
        }, format="json")

        self.assertEqual(response.status_code, 400)

    def test_empty_scopes_are_rejected(self):
        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Nothing", "scopes": [],
        }, format="json")

        self.assertEqual(response.status_code, 400)

    def test_past_expiry_is_rejected(self):
        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Stale", "scopes": [scopes.MESSAGES_SEND],
            "expires_at": "2020-01-01T00:00:00Z",
        }, format="json")

        self.assertEqual(response.status_code, 400)

    def test_rotate_returns_a_new_key(self):
        api_key, original = make_key(self.tenant, self.user)

        response = self.client.post(f"/api/v1/developer/api-keys/{api_key.pk}/rotate/")

        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.data["key"], original)
        api_key.refresh_from_db()
        self.assertEqual(api_key.status, "revoked")

    def test_disable_and_enable(self):
        api_key, _ = make_key(self.tenant, self.user)

        self.client.post(f"/api/v1/developer/api-keys/{api_key.pk}/disable/")
        api_key.refresh_from_db()
        self.assertEqual(api_key.status, "disabled")

        self.client.post(f"/api/v1/developer/api-keys/{api_key.pk}/enable/")
        api_key.refresh_from_db()
        self.assertEqual(api_key.status, "active")

    def test_revoke_is_permanent(self):
        api_key, _ = make_key(self.tenant, self.user)

        self.client.post(f"/api/v1/developer/api-keys/{api_key.pk}/revoke/")
        response = self.client.post(f"/api/v1/developer/api-keys/{api_key.pk}/enable/")

        self.assertEqual(response.status_code, 400)

    def test_keys_are_tenant_scoped(self):
        other_tenant = make_tenant("dash-other")
        other_user = make_user(other_tenant, "dash-other-owner")
        other_key, _ = make_key(other_tenant, other_user)

        listed = self.client.get("/api/v1/developer/api-keys/")
        detail = self.client.get(f"/api/v1/developer/api-keys/{other_key.pk}/")

        self.assertEqual(listed.data["count"], 0)
        self.assertEqual(detail.status_code, 404)

    def test_scopes_endpoint_lists_the_vocabulary(self):
        response = self.client.get("/api/v1/developer/api-keys/scopes/")

        self.assertEqual(response.status_code, 200)
        values = {entry["value"] for entry in response.data["scopes"]}
        self.assertEqual(values, set(scopes.ALL_SCOPES))
        self.assertIn(scopes.MESSAGES_READ, response.data["read_only_preset"])

    def test_usage_reports_counts(self):
        api_key, _ = make_key(self.tenant, self.user)

        response = self.client.get(f"/api/v1/developer/api-keys/{api_key.pk}/usage/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("totals", response.data)
        self.assertIn("lifetime", response.data)
        self.assertIn("rate_limits", response.data)

    def test_an_api_key_cannot_reach_the_dashboard(self):
        """
        The separation that makes revocation mean something: a key must not be
        able to mint another key, or to read the request log of every other key
        in the workspace.
        """
        _, full = make_key(self.tenant, self.user)

        response = APIClient().get(
            "/api/v1/developer/api-keys/", HTTP_AUTHORIZATION=f"Bearer {full}",
        )

        self.assertEqual(response.status_code, 401)

    def test_anonymous_access_is_refused(self):
        response = APIClient().get("/api/v1/developer/api-keys/")

        self.assertEqual(response.status_code, 401)

    def test_key_cannot_be_patched_into_new_scopes(self):
        api_key, _ = make_key(self.tenant, self.user, scope_list=[scopes.MESSAGES_READ])

        response = self.client.patch(
            f"/api/v1/developer/api-keys/{api_key.pk}/",
            {"scopes": list(scopes.ALL_SCOPES)}, format="json",
        )

        # PATCH is not among http_method_names, so the escalation is not merely
        # ignored -- the verb does not exist.
        self.assertEqual(response.status_code, 405)
        api_key.refresh_from_db()
        self.assertEqual(api_key.scopes, [scopes.MESSAGES_READ])

    def test_allowed_accounts_are_reported(self):
        account = make_account(self.tenant, self.user)

        response = self.client.post("/api/v1/developer/api-keys/", {
            "name": "Restricted", "scopes": [scopes.MESSAGES_SEND],
            "allowed_account_ids": [account.pk],
        }, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["allowed_account_ids"], [account.pk])

    def test_logs_endpoint_is_tenant_scoped(self):
        response = self.client.get("/api/v1/developer/api-keys/logs/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])


class WebhookDashboardTests(DashboardClientMixin, TestCase):
    def setUp(self):
        self.tenant = make_tenant("whd-tenant")
        self.user = make_user(self.tenant, "whd-owner")
        self.client = self.signed_in(self.user)

    def test_create_returns_the_secret_once(self):
        response = self.client.post("/api/v1/developer/webhooks/", {
            "url": "https://receiver.example.com/hook",
            "events": ["message.delivered"],
        }, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["secret"].startswith("whsec_"))

        read = self.client.get(f"/api/v1/developer/webhooks/{response.data['id']}/")
        self.assertNotIn("secret", read.data)
        self.assertNotIn("secret_encrypted", read.data)

    def test_audit_event_never_carries_the_secret(self):
        response = self.client.post("/api/v1/developer/webhooks/", {
            "url": "https://receiver.example.com/hook",
        }, format="json")

        event = AuditEvent.objects.get(action="webhook.created")
        self.assertNotIn(response.data["secret"], str(event.metadata))
        self.assertEqual(event.metadata["url"], "https://receiver.example.com/hook")

    def test_internal_url_is_refused(self):
        response = self.client.post("/api/v1/developer/webhooks/", {
            "url": "https://127.0.0.1:8000/hook",
        }, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_events_endpoint_lists_the_vocabulary(self):
        from .events import ALL_EVENT_TYPES

        response = self.client.get("/api/v1/developer/webhooks/events/")

        values = {entry["value"] for entry in response.data["events"]}
        self.assertEqual(values, set(ALL_EVENT_TYPES))

    def test_webhooks_are_tenant_scoped(self):
        other = make_tenant("whd-other")
        endpoint = WebhookEndpoint.objects.create(
            tenant=other, url="https://other.example.com/hook",
        )

        listed = self.client.get("/api/v1/developer/webhooks/")
        detail = self.client.get(f"/api/v1/developer/webhooks/{endpoint.pk}/")

        self.assertEqual(listed.data["count"], 0)
        self.assertEqual(detail.status_code, 404)

    def test_deliveries_endpoint(self):
        response = self.client.get("/api/v1/developer/webhooks/deliveries/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])
