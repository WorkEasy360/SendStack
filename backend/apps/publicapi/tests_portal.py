"""
Tests for the developer portal: health, diagnostics, the generated template
JSON, the Postman export and the documentation feed.

The single idea running through all of it is **the contract cannot be
described twice**. Most of these tests do not check that a string is what
somebody typed; they check that two independently-produced things agree --
the generated request body against the serializer that will receive it, the
Postman collection against the URL resolver, the docs page's scope against the
view's `required_scopes`. A test that only asserted "the example says
`messages:send`" would keep passing on the day the view starts requiring
something else, which is the exact failure the brief's point 24 is about.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import Resolver404, resolve
from rest_framework.test import APIClient

from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount

from . import scopes, template_api
from .models import ApiKey
from .postman import build_collection, build_environment
from .serializers import SendMessageSerializer
from .tests import API, make_account, make_key, make_tenant, make_user


class PortalTestCase(TestCase):
    """A workspace with a key, a number and a template -- the usual starting point."""

    def setUp(self):
        super().setUp()
        self.tenant = make_tenant(self.tenant_slug())
        self.user = make_user(self.tenant, f"{self.tenant_slug()}-owner")
        self.account = make_account(self.tenant, self.user)
        self.api_key, self.raw_key = make_key(self.tenant, self.user)
        # Named so it cannot collide with the illustrative
        # "order_confirmation" the OpenAPI spec carries in its own example --
        # otherwise the "no tenant data in the public docs" test below passes
        # on a string that was never this workspace's.
        self.template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="acme_order_receipt",
            body="Hello [name], your order [order_id] has been confirmed.",
            variables={"name": "there"},
        )

    @classmethod
    def tenant_slug(cls):
        return cls.__name__.lower()

    def auth(self, raw=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw_key}"}

    def dashboard(self):
        """A JWT-authenticated client, as the dashboard screens use."""
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client


# --- /health -------------------------------------------------------------


@API
class HealthEndpointTests(PortalTestCase):
    def test_reachable_without_any_credential(self):
        """
        The uptime-check case. A monitor with no key must get a 200 saying the
        API is up, or it cannot distinguish "we are down" from "you are not
        logged in" -- and it will page somebody for the wrong one.
        """
        response = self.client.get("/api/public/v1/health")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["api"], "online")
        self.assertFalse(body["authenticated"])
        # And it says nothing about any workspace.
        self.assertNotIn("workspace", body)
        self.assertNotIn("user", body)

    def test_valid_key_reports_the_workspace(self):
        response = self.client.get("/api/public/v1/health", **self.auth())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["workspace"]["name"], self.tenant.name)
        self.assertEqual(body["user"]["id"], str(self.user.pk))
        self.assertEqual(body["token"]["name"], self.api_key.name)

    def test_the_full_key_is_never_echoed(self):
        """
        This body is the one a customer pastes into a support ticket. It must
        carry the masked prefix and nothing that could be replayed.
        """
        response = self.client.get("/api/public/v1/health", **self.auth())

        payload = json.dumps(response.json())
        self.assertNotIn(self.raw_key, payload)
        self.assertEqual(response.json()["token"]["key"], self.api_key.masked_key)

    def test_permissions_are_reported_in_plain_english(self):
        api_key, raw = make_key(
            self.tenant, self.user, name="Sender", scope_list=[scopes.MESSAGES_SEND],
        )

        body = self.client.get("/api/public/v1/health", **self.auth(raw)).json()

        self.assertEqual(body["token"]["scopes"], [scopes.MESSAGES_SEND])
        self.assertEqual(body["token"]["permissions"], ["Send WhatsApp messages"])

    def test_a_revoked_key_is_401_not_a_cheerful_200(self):
        """
        The whole point of a health check is that it fails when something is
        wrong. A revoked key returning `api: online, authenticated: false`
        would be technically true and would hide the only fact being asked
        about.
        """
        from .services import revoke_api_key

        revoke_api_key(self.api_key)

        response = self.client.get("/api/public/v1/health", **self.auth())

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "REVOKED_API_KEY")

    def test_a_garbage_key_is_401(self):
        response = self.client.get("/api/public/v1/health", **self.auth("ss_live_deadbeefcafe"))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_API_KEY")

    def test_no_scope_is_required(self):
        """A key with nothing ticked must still be able to prove it is valid."""
        _, raw = make_key(self.tenant, self.user, name="Bare", scope_list=[])

        response = self.client.get("/api/public/v1/health", **self.auth(raw))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])

    def test_ping_still_works(self):
        """The older name is a published path and must not break."""
        self.assertEqual(self.client.get("/api/public/v1/ping", **self.auth()).status_code, 200)


# --- /diagnostics --------------------------------------------------------


@API
class DiagnosticsTests(PortalTestCase):
    def checks(self, raw=None):
        body = self.client.get("/api/public/v1/diagnostics", **self.auth(raw)).json()
        return body, {check["name"]: check for check in body["checks"]}

    def test_a_working_setup_is_ready_to_send(self):
        body, checks = self.checks()

        self.assertTrue(body["ready_to_send"])
        self.assertEqual(
            {name: check["status"] for name, check in checks.items()},
            {
                "authentication": "pass", "permissions": "pass", "workspace": "pass",
                "whatsapp_connection": "pass", "credits": "pass",
            },
        )

    def test_a_disconnected_number_fails_only_the_whatsapp_check(self):
        """
        The distinction the brief calls extremely important: authentication and
        permission still PASS, and only the WhatsApp link FAILs, so the
        customer is told to reconnect their phone rather than to check their
        key.
        """
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(
            status=WhatsAppAccount.Status.DISCONNECTED,
        )

        body, checks = self.checks()

        self.assertFalse(body["ready_to_send"])
        self.assertEqual(checks["authentication"]["status"], "pass")
        self.assertEqual(checks["permissions"]["status"], "pass")
        self.assertEqual(checks["whatsapp_connection"]["status"], "fail")
        self.assertIn("connected", checks["whatsapp_connection"]["detail"])

    def test_no_accounts_at_all_says_so_specifically(self):
        WhatsAppAccount.objects.filter(pk=self.account.pk).delete()

        _, checks = self.checks()

        self.assertEqual(checks["whatsapp_connection"]["status"], "fail")
        self.assertIn("no WhatsApp accounts", checks["whatsapp_connection"]["detail"])

    def test_a_key_without_send_permission_fails_the_permission_check(self):
        _, raw = make_key(self.tenant, self.user, name="Reader", scope_list=[scopes.MESSAGES_READ])

        body, checks = self.checks(raw)

        self.assertFalse(body["ready_to_send"])
        self.assertEqual(checks["permissions"]["status"], "fail")
        self.assertEqual(checks["whatsapp_connection"]["status"], "pass")

    def test_a_test_key_fails_the_workspace_check(self):
        _, raw = make_key(
            self.tenant, self.user, name="Staging", environment=ApiKey.Environment.TEST,
        )

        _, checks = self.checks(raw)

        self.assertEqual(checks["workspace"]["status"], "fail")
        self.assertIn("test token", checks["workspace"]["detail"].lower())

    def test_credits_report_available_not_total(self):
        """
        500 held in reserve is 500 that cannot be spent. Reporting the balance
        would say everything is fine right up to the 402.
        """
        get_user_model().objects.filter(pk=self.user.pk).update(credits=500, reserved_credits=500)

        _, checks = self.checks()

        self.assertEqual(checks["credits"]["status"], "fail")
        self.assertEqual(checks["credits"]["available"], 0)
        self.assertEqual(checks["credits"]["balance"], 500)

    def test_it_sends_nothing(self):
        from apps.messaging.models import Message

        self.client.get("/api/public/v1/diagnostics", **self.auth())

        self.assertEqual(Message.objects.count(), 0)
        self.user.refresh_from_db()
        self.assertEqual(self.user.reserved_credits, 0)

    def test_another_workspace_is_invisible(self):
        other = make_tenant("diagnostics-other")
        other_user = make_user(other, "diagnostics-other-owner")
        make_account(other, other_user, name="Theirs")
        _, raw = make_key(other, other_user, name="Theirs")

        _, checks = self.checks(raw)

        names = [row["name"] for row in checks["whatsapp_connection"]["accounts"]]
        self.assertEqual(names, ["Theirs"])


# --- the generated template JSON -----------------------------------------


class TemplateApiExampleTests(PortalTestCase):
    def test_variables_come_from_the_template_text(self):
        described = template_api.describe(self.template)

        self.assertEqual(
            [row["name"] for row in described["variables"]], ["name", "order_id"],
        )
        self.assertEqual(described["request_body"]["variables"]["order_id"], "string")

    def test_a_declared_fallback_is_shown_instead_of_the_placeholder(self):
        """
        `variables={"name": "there"}` is what the renderer substitutes when a
        contact has no name. It is a better example than the word "string", and
        it is guaranteed to be a value this template was designed for.
        """
        described = template_api.describe(self.template)
        by_name = {row["name"]: row for row in described["variables"]}

        self.assertEqual(by_name["name"]["example"], "there")
        self.assertFalse(by_name["name"]["required"])
        self.assertTrue(by_name["order_id"]["required"])

    def test_a_template_with_no_variables_omits_the_field(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, name="plain", body="We are closed on Sunday.",
        )

        described = template_api.describe(template)

        self.assertNotIn("variables", described["request_body"])
        self.assertIn(
            "no variables", " ".join(described["notes"]),
        )

    def test_media_is_reported_but_never_as_a_request_field(self):
        """
        Media on a template is sent automatically. Emitting `media_url` in the
        request body would invite a caller to set it, and sending by
        `template_id` ignores it -- so the field would be a documented lie.
        """
        template = MessageTemplate.objects.create(
            tenant=self.tenant, name="invoice", message_type=MessageTemplate.MessageType.IMAGE,
            body="Your invoice", media_url="https://example.com/invoice.jpg",
        )

        described = template_api.describe(template)

        self.assertNotIn("media_url", described["request_body"])
        self.assertEqual(described["sends_with"]["media"]["url"], "https://example.com/invoice.jpg")
        self.assertEqual(described["sends_with"]["type"], "image")

    def test_buttons_are_reported_but_never_as_a_request_field(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, name="confirm", body="Confirm?",
            buttons=[{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
        )

        described = template_api.describe(template)

        self.assertNotIn("buttons", described["request_body"])
        self.assertEqual(described["sends_with"]["buttons"], ["Yes", "No"])
        self.assertIn("cannot be overridden", " ".join(described["notes"]))

    def test_a_paused_template_says_so(self):
        MessageTemplate.objects.filter(pk=self.template.pk).update(is_active=False)
        self.template.refresh_from_db()

        described = template_api.describe(self.template)

        self.assertIn("paused", " ".join(described["notes"]).lower())

    def test_the_curl_carries_a_placeholder_token_not_a_plausible_one(self):
        curl = template_api.curl_example(self.template)

        self.assertIn("Bearer ss_live_YOUR_API_KEY", curl)
        self.assertIn("/messages/send", curl)

    def test_the_generated_body_is_accepted_by_the_real_serializer(self):
        """
        The load-bearing test of this whole feature.

        The JSON the UI shows is fed straight into the serializer the send
        endpoint uses. A documented body the backend rejects is worse than no
        example at all, and this is what makes that impossible to ship.
        """
        for template in [
            self.template,
            MessageTemplate.objects.create(tenant=self.tenant, name="plain", body="Hi"),
            MessageTemplate.objects.create(
                tenant=self.tenant, name="pic", message_type=MessageTemplate.MessageType.IMAGE,
                body="Look", media_url="https://example.com/a.png",
            ),
        ]:
            with self.subTest(template=template.name):
                serializer = SendMessageSerializer(data=template_api.send_body(template))
                self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_the_generated_body_actually_sends(self):
        """
        One level beyond validation: the body is posted to the live endpoint
        and must be accepted, so the example is proven end to end rather than
        proven to parse.
        """
        body = template_api.send_body(self.template, recipient="919162353602")

        with override_settings(PUBLIC_API_REQUIRE_HTTPS=False):
            with self.captureOnCommitCallbacks(execute=False):
                response = self.client.post(
                    "/api/public/v1/messages/send",
                    data=json.dumps(body), content_type="application/json", **self.auth(),
                )

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["status"], "queued")


@API
class TemplateApiExampleEndpointTests(PortalTestCase):
    def test_the_dashboard_endpoint_returns_the_generated_shape(self):
        response = self.dashboard().get(f"/api/v1/developer/api/templates/{self.template.pk}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["template_name"], "acme_order_receipt")
        self.assertEqual(body["endpoint"]["required_scope"], "messages:send")
        self.assertIn("variables", body["request_body"])

    def test_another_workspace_template_is_a_404(self):
        other = make_tenant("template-json-other")
        other_user = make_user(other, "template-json-other-owner")
        theirs = MessageTemplate.objects.create(tenant=other, name="secret", body="Hush [code]")

        response = self.dashboard().get(f"/api/v1/developer/api/templates/{theirs.pk}")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Hush", response.content.decode())
        # And the other user cannot see ours either.
        client = APIClient()
        client.force_authenticate(user=other_user)
        listing = client.get("/api/v1/developer/api/templates").json()
        self.assertEqual([row["template_name"] for row in listing["results"]], ["secret"])

    def test_it_needs_a_login(self):
        self.assertEqual(
            APIClient().get(f"/api/v1/developer/api/templates/{self.template.pk}").status_code, 401,
        )


# --- Postman -------------------------------------------------------------


class PostmanCollectionTests(TestCase):
    def setUp(self):
        super().setUp()
        self.collection = build_collection(base_url="https://example.com/api/public/v1")

    def requests(self):
        for folder in self.collection["item"]:
            for item in folder["item"]:
                yield folder["name"], item

    def test_every_request_resolves_to_a_real_view(self):
        """
        The point of generating the collection rather than writing it. A
        request pointing at a path Django does not serve is a 404 the customer
        meets before they have written a line of their own code.
        """
        for folder, item in self.requests():
            path = item["request"]["url"]["raw"].split("?")[0].replace("{{base_url}}", "/api/public/v1")
            # Path parameters are Postman placeholders; substitute something
            # the URL patterns will match so `resolve` is testing the *shape*.
            for placeholder in ("{message_id}", "{campaign_id}", "{template_id}", "{contact_id}", "{account_id}"):
                path = path.replace(placeholder, "abc_1")
            path = path.replace("{endpoint_id}", "1").replace("{action}", "start")

            with self.subTest(request=f"{folder}/{item['name']}"):
                try:
                    resolve(path)
                except Resolver404:  # pragma: no cover -- the failure message is the point
                    self.fail(f"Postman request '{item['name']}' points at {path}, which does not exist.")

    def test_every_request_uses_the_variables_not_literals(self):
        for folder, item in self.requests():
            with self.subTest(request=f"{folder}/{item['name']}"):
                self.assertTrue(item["request"]["url"]["raw"].startswith("{{base_url}}"))

    def test_authentication_is_collection_level_and_uses_the_token_variable(self):
        self.assertEqual(self.collection["auth"]["type"], "bearer")
        self.assertEqual(self.collection["auth"]["bearer"][0]["value"], "{{api_token}}")

    def test_the_first_request_is_the_health_check(self):
        """Step one of the quick start has to be step one of the collection."""
        first_folder = self.collection["item"][0]
        self.assertEqual(first_folder["name"], "Authentication")
        self.assertEqual(first_folder["item"][0]["name"], "Test authentication")
        self.assertIn("/health", first_folder["item"][0]["request"]["url"]["raw"])

    def test_the_folders_the_brief_names_are_all_present(self):
        names = {folder["name"] for folder in self.collection["item"]}
        self.assertLessEqual(
            {"Authentication", "Messages", "Templates", "Contacts", "Campaigns", "Accounts"},
            names,
        )

    def test_every_json_body_is_valid_json(self):
        for folder, item in self.requests():
            body = item["request"].get("body")
            if body:
                with self.subTest(request=f"{folder}/{item['name']}"):
                    json.loads(body["raw"])

    def test_the_send_body_is_accepted_by_the_real_serializer(self):
        """
        The collection's flagship request, checked against the code that will
        receive it. `{{phone}}` stands in for a number Postman substitutes.
        """
        item = next(item for _, item in self.requests() if item["name"] == "Send a message")
        body = json.loads(item["request"]["body"]["raw"])
        body["to"] = "919876543210"

        serializer = SendMessageSerializer(data=body)

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_no_example_hardcodes_an_account_id_nobody_owns(self):
        """
        `account_id: "account_3"` reads well in prose and 404s for every
        customer who imports it. Omitting it is supported and works.
        """
        for folder, item in self.requests():
            body = item["request"].get("body")
            if not body:
                continue
            with self.subTest(request=f"{folder}/{item['name']}"):
                self.assertNotIn("account_3", body["raw"])

    def test_no_body_contains_postmans_own_variable_syntax_by_accident(self):
        """
        A template body written as `{{name}}` is a valid SendStack placeholder
        *and* a Postman variable. Postman wins, substitutes empty, and the
        customer creates a template with the placeholders blanked out.
        """
        item = next(item for _, item in self.requests() if item["name"] == "Create a template")
        body = json.loads(item["request"]["body"]["raw"])

        self.assertNotIn("{{", body["body"])
        self.assertIn("[name]", body["body"])


class PostmanEnvironmentTests(TestCase):
    def test_it_carries_the_variables_the_brief_names(self):
        environment = build_environment(base_url="https://example.com/api/public/v1")
        keys = {value["key"] for value in environment["values"]}

        self.assertLessEqual({"base_url", "api_token", "phone", "template_name"}, keys)

    def test_the_base_url_is_prefilled_and_the_token_is_not(self):
        environment = build_environment(base_url="https://example.com/api/public/v1")
        values = {value["key"]: value for value in environment["values"]}

        self.assertEqual(values["base_url"]["value"], "https://example.com/api/public/v1")
        # Never pre-filled: it is not recoverable from the database, and a file
        # people commit to a repository is the wrong place for a live key.
        self.assertEqual(values["api_token"]["value"], "")
        self.assertEqual(values["api_token"]["type"], "secret")


@API
class PostmanEndpointTests(PortalTestCase):
    def test_the_public_collection_needs_no_credential(self):
        response = self.client.get("/api/public/v1/postman.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["name"], "WhatsApp Marketing API")

    def test_the_dashboard_export_prefills_this_deployments_base_url(self):
        response = self.dashboard().get("/api/v1/developer/api/postman?type=environment")

        self.assertEqual(response.status_code, 200)
        values = {value["key"]: value["value"] for value in response.json()["values"]}
        self.assertTrue(values["base_url"].endswith("/api/public/v1"))
        self.assertEqual(values["api_token"], "")

    def test_download_sets_a_filename(self):
        response = self.dashboard().get("/api/v1/developer/api/postman?type=collection&download=1")

        self.assertIn("postman_collection.json", response["Content-Disposition"])

    def test_an_unknown_type_is_refused(self):
        self.assertEqual(
            self.dashboard().get("/api/v1/developer/api/postman?type=nonsense").status_code, 400,
        )


# --- the portal's own screens --------------------------------------------


@API
class ApiOverviewTests(PortalTestCase):
    def test_the_base_url_is_a_real_absolute_url(self):
        """
        The brief's biggest complaint: customers do not know where to take the
        URL from. It must be absolute, must end in the mount point, and must
        not be a placeholder.
        """
        body = self.dashboard().get("/api/v1/developer/api/overview").json()

        self.assertTrue(body["base_url"].startswith("http"))
        self.assertTrue(body["base_url"].endswith("/api/public/v1"))
        self.assertNotIn("yourdomain", body["base_url"])

    def test_the_base_url_is_derived_from_the_request_when_nothing_is_declared(self):
        # ALLOWED_HOSTS widened alongside, because the point of this test is
        # the *Host header* path -- a DisallowedHost would fail it for an
        # unrelated reason and read like the fallback not working.
        with override_settings(PUBLIC_API_BASE_URL="", ALLOWED_HOSTS=["app.example.org"]):
            body = self.dashboard().get(
                "/api/v1/developer/api/overview", SERVER_NAME="app.example.org",
            ).json()

        self.assertEqual(body["base_url"], "http://app.example.org/api/public/v1")

    def test_the_declared_public_url_wins(self):
        with override_settings(PUBLIC_API_BASE_URL="https://api.acme.com"):
            body = self.dashboard().get("/api/v1/developer/api/overview").json()

        self.assertEqual(body["base_url"], "https://api.acme.com/api/public/v1")

    def test_the_internal_docker_hostname_is_never_shown_to_a_customer(self):
        """
        The bug this setting exists to prevent, pinned.

        Under docker-compose `BACKEND_PUBLIC_URL` is `http://backend:8000` --
        correct for the QR bridge calling us back, and unreachable from any
        browser. It was the value this screen printed before
        PUBLIC_API_BASE_URL existed, which made "where do I take the URL from?"
        answerable only with an address that does not work.
        """
        with override_settings(
            BACKEND_PUBLIC_URL="http://backend:8000", PUBLIC_API_BASE_URL="",
        ):
            body = self.dashboard().get("/api/v1/developer/api/overview").json()

        self.assertNotIn("backend:8000", body["base_url"])
        self.assertNotIn("backend:8000", json.dumps(body["links"]))
        # It fell back to the host this page was fetched over, which is by
        # construction one the reader can reach.
        self.assertEqual(body["base_url"], "http://testserver/api/public/v1")

    def test_the_docs_the_spec_and_the_collection_all_agree_on_the_url(self):
        """
        Four surfaces publish this address. They read one function, so they
        cannot disagree -- and a customer who takes the URL from any of them
        gets the same working one.
        """
        with override_settings(PUBLIC_API_BASE_URL="https://api.acme.com"):
            overview = self.dashboard().get("/api/v1/developer/api/overview").json()
            reference = self.dashboard().get("/api/v1/developer/api/reference").json()
            spec = self.client.get("/api/public/v1/openapi.json").json()
            collection = self.client.get("/api/public/v1/postman.json").json()
            docs = self.client.get("/api/public/v1/docs").content.decode()

        expected = "https://api.acme.com/api/public/v1"
        self.assertEqual(overview["base_url"], expected)
        self.assertEqual(reference["base_url"], expected)
        self.assertEqual(spec["servers"][0]["url"], expected)
        self.assertEqual(collection["variable"][0]["value"], expected)
        self.assertIn(expected, docs)

    def test_every_advertised_link_resolves(self):
        body = self.dashboard().get("/api/v1/developer/api/overview").json()

        for name, url in body["links"].items():
            with self.subTest(link=name):
                path = url.split("/api/public/v1", 1)[1]
                resolve(f"/api/public/v1{path}")

    def test_the_quick_start_is_ten_numbered_steps(self):
        body = self.dashboard().get("/api/v1/developer/api/overview").json()

        self.assertEqual([step["step"] for step in body["quick_start"]], list(range(1, 11)))

    def test_it_needs_a_login(self):
        self.assertEqual(APIClient().get("/api/v1/developer/api/overview").status_code, 401)


@API
class PermissionCatalogTests(PortalTestCase):
    def test_every_scope_has_a_plain_english_label_and_help_text(self):
        """
        A checkbox with no sentence under it is a permission the customer
        grants by guessing. Adding a scope without a label fails here rather
        than shipping a blank row.
        """
        body = self.dashboard().get("/api/v1/developer/api/permissions").json()
        described = {entry["value"] for entry in body["permissions"]}

        self.assertEqual(described, set(scopes.ALL_SCOPES))
        for entry in body["permissions"]:
            with self.subTest(scope=entry["value"]):
                self.assertTrue(entry["label"])
                self.assertTrue(entry["help"])
                self.assertTrue(entry["group"])
                # The technical string stays available: a developer reading a
                # 403 that names `messages:send` has to find the box for it.
                self.assertEqual(entry["scope"], entry["value"])
                self.assertNotEqual(entry["label"], entry["value"])

    def test_the_default_is_the_least_that_does_the_job_not_everything(self):
        body = self.dashboard().get("/api/v1/developer/api/permissions").json()

        self.assertNotEqual(set(body["defaults"]), set(scopes.ALL_SCOPES))
        self.assertIn(scopes.MESSAGES_SEND, body["defaults"])
        # Nothing destructive by default.
        for dangerous in (scopes.TEMPLATES_WRITE, scopes.CAMPAIGNS_SEND, scopes.WEBHOOKS_MANAGE):
            self.assertNotIn(dangerous, body["defaults"])

    def test_every_preset_lists_only_real_scopes(self):
        body = self.dashboard().get("/api/v1/developer/api/permissions").json()

        for preset in body["presets"]:
            with self.subTest(preset=preset["id"]):
                self.assertLessEqual(set(preset["scopes"]), set(scopes.ALL_SCOPES))
                self.assertTrue(preset["description"])

    def test_the_sensitive_flag_marks_what_costs_money_or_deletes(self):
        body = self.dashboard().get("/api/v1/developer/api/permissions").json()
        sensitive = {entry["value"] for entry in body["permissions"] if entry["sensitive"]}

        self.assertIn(scopes.MESSAGES_SEND, sensitive)
        self.assertIn(scopes.TEMPLATES_WRITE, sensitive)
        self.assertNotIn(scopes.MESSAGES_READ, sensitive)


@API
class ApiReferenceTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.body = self.dashboard().get("/api/v1/developer/api/reference").json()

    def endpoints(self):
        for group in self.body["groups"]:
            for endpoint in group["endpoints"]:
                yield endpoint

    def test_every_documented_endpoint_exists(self):
        for endpoint in self.endpoints():
            path = endpoint["path"]
            for placeholder in ("{message_id}", "{campaign_id}", "{template_id}", "{contact_id}", "{account_id}"):
                path = path.replace(placeholder, "abc_1")
            path = path.replace("{endpoint_id}", "1").replace("{action}", "start")
            with self.subTest(endpoint=f"{endpoint['method']} {endpoint['path']}"):
                resolve(f"/api/public/v1{path}")

    def test_the_documented_permission_matches_what_the_view_requires(self):
        """
        The contract check the brief's point 24 asks for, applied to the docs.

        Reads `required_scopes` and `method_scopes` off the resolved view and
        asserts the documented scope is genuinely one of them -- so renaming a
        scope in a view and forgetting the docs is a failing test rather than a
        support ticket.
        """
        for endpoint in self.endpoints():
            documented = endpoint["required_permission"]
            if not documented:
                continue
            path = endpoint["path"]
            for placeholder in ("{message_id}", "{campaign_id}", "{template_id}", "{contact_id}", "{account_id}"):
                path = path.replace(placeholder, "abc_1")
            path = path.replace("{endpoint_id}", "1").replace("{action}", "start")

            match = resolve(f"/api/public/v1{path}")
            view = getattr(match.func, "cls", None) or getattr(match.func, "view_class", None)
            if view is None:
                continue

            required = set(getattr(view, "required_scopes", None) or [])
            for per_method in (getattr(view, "method_scopes", None) or {}).values():
                required.update(per_method)
            required.update((getattr(view, "ACTION_SCOPES", None) or {}).values())

            with self.subTest(endpoint=f"{endpoint['method']} {endpoint['path']}"):
                self.assertIn(documented, required)

    def test_every_endpoint_carries_a_curl_and_a_url(self):
        for endpoint in self.endpoints():
            with self.subTest(endpoint=endpoint["path"]):
                self.assertTrue(endpoint["url"].startswith("http"))
                self.assertIn("curl", endpoint["curl"])
                self.assertIn(endpoint["method"], endpoint["curl"])

    def test_error_responses_are_documented_alongside_the_success(self):
        for endpoint in self.endpoints():
            with self.subTest(endpoint=endpoint["path"]):
                self.assertTrue(any(row["is_error"] for row in endpoint["responses"]))

    def test_the_published_error_vocabulary_is_complete(self):
        from .errors import ERROR_CODES

        documented = {row["code"] for row in self.body["error_codes"]}

        self.assertEqual(documented, set(ERROR_CODES))

    def test_authentication_is_the_first_group(self):
        self.assertEqual(self.body["groups"][0]["name"], "Authentication")

    def test_no_curl_leaks_a_real_looking_token(self):
        for endpoint in self.endpoints():
            with self.subTest(endpoint=endpoint["path"]):
                self.assertIn("YOUR_API_TOKEN", endpoint["curl"])


@API
class PostmanGuideTests(PortalTestCase):
    def test_it_documents_the_file_it_is_offering(self):
        body = self.dashboard().get("/api/v1/developer/api/postman/guide").json()
        documented = {row["key"] for row in body["variables"]}
        exported = {value["key"] for value in build_environment()["values"]}

        self.assertEqual(documented, exported)

    def test_the_base_url_example_is_this_deployments_real_one(self):
        body = self.dashboard().get("/api/v1/developer/api/postman/guide").json()
        base = {row["key"]: row["example"] for row in body["variables"]}["base_url"]

        self.assertEqual(base, body["base_url"])
        self.assertTrue(base.startswith("http"))

    def test_the_token_variable_is_marked_secret(self):
        body = self.dashboard().get("/api/v1/developer/api/postman/guide").json()
        by_key = {row["key"]: row for row in body["variables"]}

        self.assertTrue(by_key["api_token"]["secret"])
        self.assertEqual(by_key["api_token"]["example"], "")


# --- what the new surface must not do ------------------------------------


@API
class PortalSecurityTests(PortalTestCase):
    """
    The new endpoints inherit the app's guarantees rather than opting out of
    them. Each of these is a way a diagnostic endpoint typically becomes the
    hole in an otherwise careful API.
    """

    def test_health_and_diagnostics_are_rate_limited_like_everything_else(self):
        """
        Both are unauthenticated-friendly and cheap to call, which is exactly
        what makes an unlimited one a free amplifier. They go through
        PublicAPIView, so they charge the request bucket.
        """
        from django.core.cache import cache

        cache.clear()
        _, raw = make_key(self.tenant, self.user, name="Tight", requests_per_minute=2)

        codes = [
            self.client.get("/api/public/v1/health", **self.auth(raw)).status_code
            for _ in range(4)
        ]

        self.assertEqual(codes[:2], [200, 200])
        self.assertEqual(codes[2], 429)

    def test_the_new_endpoints_are_written_to_the_request_log(self):
        from .models import ApiRequestLog

        self.client.get("/api/public/v1/health", **self.auth())
        self.client.get("/api/public/v1/diagnostics", **self.auth())

        paths = set(ApiRequestLog.objects.values_list("path", flat=True))
        self.assertIn("/api/public/v1/health", paths)
        self.assertIn("/api/public/v1/diagnostics", paths)

    def test_the_request_log_never_stores_the_token(self):
        from .models import ApiRequestLog

        self.client.get("/api/public/v1/diagnostics", **self.auth())

        row = ApiRequestLog.objects.get(path="/api/public/v1/diagnostics")
        serialised = str(row.__dict__)
        self.assertNotIn(self.raw_key, serialised)
        # Only the public half is kept, which is what makes the log safe to
        # show to every admin on the workspace.
        self.assertEqual(row.key_prefix, self.api_key.key_prefix)

    def test_a_rejected_token_is_never_echoed_back(self):
        """
        A 401 that quotes the credential is a 401 that puts it in the
        customer's browser history, their proxy logs and their bug tracker.
        """
        bogus = "ss_live_1234abcd" + "f" * 32

        response = self.client.get("/api/public/v1/health", **self.auth(bogus))

        self.assertEqual(response.status_code, 401)
        self.assertNotIn(bogus, response.content.decode())

    def test_the_public_documentation_endpoints_expose_no_tenant_data(self):
        """
        /docs, /openapi.json and /postman.json are deliberately anonymous. That
        is only safe while they are static descriptions of the interface.
        """
        for path in ("/api/public/v1/openapi.json", "/api/public/v1/postman.json"):
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertNotIn(self.tenant.name, body)
                self.assertNotIn(self.user.username, body)
                self.assertNotIn(self.template.name, body)
                self.assertNotIn(self.api_key.key_prefix, body)

    def test_the_portal_endpoints_refuse_an_api_key(self):
        """
        The dashboard screens are JWT-only. A key that could read the portal
        could enumerate every template body in the workspace without holding
        `templates:read`, and could read the Postman export -- neither is
        something a scope was ever granted for.
        """
        for path in (
            "/api/v1/developer/api/overview",
            "/api/v1/developer/api/permissions",
            "/api/v1/developer/api/reference",
            "/api/v1/developer/api/templates",
            "/api/v1/developer/api/postman",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, **self.auth())
                self.assertIn(response.status_code, (401, 403), path)

    def test_diagnostics_needs_a_credential(self):
        response = self.client.get("/api/public/v1/diagnostics")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "INVALID_API_KEY")

    def test_an_expired_token_cannot_pass_the_health_check(self):
        from datetime import timedelta

        from django.utils import timezone

        _, raw = make_key(
            self.tenant, self.user, name="Old",
            expires_at=timezone.now() + timedelta(days=1),
        )
        ApiKey.objects.filter(name="Old").update(expires_at=timezone.now() - timedelta(seconds=1))

        response = self.client.get("/api/public/v1/health", **self.auth(raw))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "EXPIRED_API_KEY")

    def test_a_suspended_workspace_loses_the_health_check_too(self):
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=self.tenant.pk).update(is_active=False)

        response = self.client.get("/api/public/v1/health", **self.auth())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "WORKSPACE_SUSPENDED")


@API
class ApiLogPresentationTests(PortalTestCase):
    """The API Logs screen, which the brief's point 17 specifies field by field."""

    def test_a_call_is_reported_with_everything_the_screen_needs(self):
        self.client.get("/api/public/v1/health", **self.auth())

        rows = self.dashboard().get("/api/v1/developer/api-keys/logs/").json()["results"]

        row = next(row for row in rows if row["path"].endswith("/health"))
        for field in (
            "created_at", "method", "path", "status_code", "duration_ms",
            "ip_address", "key_name", "key_masked", "user", "error_code",
        ):
            self.assertIn(field, row)
        self.assertEqual(row["key_name"], self.api_key.name)
        self.assertEqual(row["user"], self.user.username)

    def test_the_log_shows_a_masked_token_never_a_whole_one(self):
        self.client.get("/api/public/v1/health", **self.auth())

        body = self.dashboard().get("/api/v1/developer/api-keys/logs/").content.decode()

        self.assertNotIn(self.raw_key, body)
        self.assertIn(self.api_key.masked_key, body)

    def test_a_send_is_linked_to_the_message_it_produced(self):
        body = template_api.send_body(self.template, recipient="919162353602")

        with self.captureOnCommitCallbacks(execute=False):
            self.client.post(
                "/api/public/v1/messages/send",
                data=json.dumps(body), content_type="application/json", **self.auth(),
            )

        rows = self.dashboard().get("/api/v1/developer/api-keys/logs/").json()["results"]

        row = next(row for row in rows if row["path"].endswith("/messages/send"))
        self.assertTrue(row["message_id"].startswith("msg_"))

    def test_another_workspaces_calls_are_invisible(self):
        other = make_tenant("logs-other")
        other_user = make_user(other, "logs-other-owner")
        _, other_raw = make_key(other, other_user, name="Theirs")

        # One call from each workspace, so the listing has something to keep
        # as well as something to drop.
        self.client.get("/api/public/v1/health", **self.auth())
        self.client.get("/api/public/v1/health", HTTP_AUTHORIZATION=f"Bearer {other_raw}")

        rows = self.dashboard().get("/api/v1/developer/api-keys/logs/").json()["results"]

        self.assertEqual({row["key_name"] for row in rows} - {""}, {self.api_key.name})
        self.assertNotIn("Theirs", [row["key_name"] for row in rows])
