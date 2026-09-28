"""
The template types beyond plain text, and the variable system underneath them.

Three things are worth stating about what these cover.

The **two variable syntaxes** are not a nicety. Every template ever saved here
uses ``[Name]``; everybody arriving from another WhatsApp tool types
``{{name}}``, and until now those braces were sent to the customer verbatim.
Both have to work, and the old one has to keep working exactly as it did.

The **delivery preview** is the honesty mechanism. It is produced by the same
functions the send path calls, so a test that the composer's "as sent" text
matches the sender's is a test that the product cannot lie about what a
recipient will receive.

**Tenant isolation** is asserted on every new endpoint rather than assumed
from the base class, because a new action is exactly where scoping gets
forgotten.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount
from apps.whatsapp.tests_capabilities import configured_qr_provider

from .models import MessageTemplate, extract_variables
from .services import available_variables, build_render_context, render_template, render_text

LIST_CONTENT = {
    "header": "Choose a service",
    "button": "View services",
    "sections": [
        {
            "title": "Services",
            "rows": [
                {"title": "Web development", "description": "Sites and apps"},
                {"title": "WhatsApp marketing"},
            ],
        }
    ],
}

POLL_CONTENT = {
    "question": "Which service are you interested in?",
    "options": ["Web development", "WhatsApp marketing", "AI automation"],
    "allow_multiple": False,
}


class VariableSyntaxTests(TestCase):
    def test_both_syntaxes_are_recognised(self):
        self.assertEqual(
            extract_variables("Hi [Name], order {{order_id}} for [Name] and {{amount}}"),
            ["Name", "order_id", "amount"],
        )

    def test_the_original_bracket_syntax_is_unchanged(self):
        """Every template ever saved here uses it."""
        self.assertEqual(extract_variables("Hi [Name], your order [OrderID]"), ["Name", "OrderID"])

    def test_whitespace_inside_the_braces_is_tolerated(self):
        self.assertEqual(extract_variables("Hi {{ name }}"), ["name"])

    def test_a_brace_that_is_not_a_variable_is_left_alone(self):
        self.assertEqual(extract_variables("A {single} brace and {{ok}}"), ["ok"])


class RenderContextTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Vars", slug="vars-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="vars-owner", password="x", tenant=self.tenant,
        )

    def _contact(self, **kwargs):
        defaults = {
            "tenant": self.tenant, "created_by": self.user, "name": "Aman Gupta",
            "phone": "+919876543210", "company": "Gupta Traders",
        }
        return Contact.objects.create(**{**defaults, **kwargs})

    def test_a_name_is_split_into_first_and_last(self):
        context = build_render_context(self._contact())

        self.assertEqual(context["first_name"], "Aman")
        self.assertEqual(context["last_name"], "Gupta")

    def test_a_single_word_name_yields_no_last_name(self):
        """
        The honest answer. A fallback in `variables` is how an author covers
        it, not an invented surname.
        """
        context = build_render_context(self._contact(name="Aman", phone="+919876543211"))

        self.assertEqual(context["first_name"], "Aman")
        self.assertEqual(context["last_name"], "")

    def test_number_is_an_alias_for_the_phone(self):
        self.assertEqual(build_render_context(self._contact())["number"], "+919876543210")

    def test_company_is_available_as_a_variable(self):
        self.assertEqual(build_render_context(self._contact())["company"], "Gupta Traders")

    def test_date_and_time_are_present(self):
        context = build_render_context(self._contact())

        self.assertTrue(context["date"])
        self.assertTrue(context["time"])

    def test_a_custom_field_outranks_a_computed_one(self):
        """
        Somebody who imported a column called "date" meant *their* date.
        Substituting today's would be the platform overruling the customer.
        """
        contact = self._contact(custom_fields={"date": "next Tuesday"})

        self.assertEqual(build_render_context(contact)["date"], "next Tuesday")

    def test_arbitrary_custom_fields_are_usable_by_name(self):
        contact = self._contact(custom_fields={"order_id": "ORD123", "amount": "2500"})
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Order",
            body="Hello {{name}},\n\nYour order {{order_id}} worth Rs{{amount}} is ready.",
        )

        rendered = render_template(template, contact)

        self.assertEqual(
            rendered["body"], "Hello Aman Gupta,\n\nYour order ORD123 worth Rs2500 is ready.",
        )

    def test_a_fallback_matches_however_it_was_capitalised(self):
        contact = self._contact(custom_fields={})

        self.assertEqual(
            render_text("Order {{order_id}}", build_render_context(contact), {"OrderID": "x", "Order_ID": "N/A"}),
            "Order N/A",
        )

    def test_a_variable_with_no_value_and_no_fallback_renders_empty(self):
        """
        The pre-existing behaviour, kept: a customer receiving "Hello [Name]"
        is worse than one receiving "Hello".
        """
        self.assertEqual(render_text("Hi [Nickname]!", {}, {}), "Hi !")


class AvailableVariablesTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Avail", slug="avail-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="avail-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_custom_fields_come_from_this_workspaces_own_contacts(self):
        Contact.objects.create(
            tenant=self.tenant, created_by=self.user, phone="+919876543210",
            custom_fields={"order_id": "A1", "city": "Mumbai"},
        )

        result = available_variables(self.tenant_id())

        self.assertIn("order_id", result["custom"])
        self.assertIn("city", result["custom"])
        self.assertIn("first_name", result["builtin"])

    def test_another_workspaces_fields_are_not_offered(self):
        other = Tenant.objects.create(name="Other", slug="avail-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="avail-other-owner", password="x", tenant=other,
        )
        Contact.objects.create(
            tenant=other, created_by=other_user, phone="+919876543299",
            custom_fields={"secret_field": "x"},
        )

        result = available_variables(self.tenant_id())

        self.assertNotIn("secret_field", result["custom"])

    def test_the_endpoint_answers_for_the_caller(self):
        response = self.client.get("/api/v1/templates/templates/variables/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("builtin", response.json())

    def tenant_id(self):
        return self.tenant.pk


class TemplateContentApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Content", slug="content-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="content-owner", password="x", tenant=self.tenant,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **overrides):
        payload = {"name": "T", "body": "Please choose."}
        payload.update(overrides)
        return self.client.post("/api/v1/templates/templates/", payload, format="json")

    def test_a_list_template_is_saved_and_reports_its_kind(self):
        response = self._post(name="Menu", list_content=LIST_CONTENT)

        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual(body["content_kind"], "LIST")
        self.assertEqual(
            [row["id"] for row in body["list_content"]["sections"][0]["rows"]],
            ["web_development", "whatsapp_marketing"],
        )

    def test_a_poll_template_is_saved_and_reports_its_kind(self):
        response = self._post(name="Poll", body="", poll_content=POLL_CONTENT)

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["content_kind"], "POLL")

    def test_a_poll_needs_no_body_text(self):
        """Its question is the message; WhatsApp renders a poll as its own bubble."""
        self.assertEqual(self._post(name="Poll2", body="", poll_content=POLL_CONTENT).status_code, 201)

    def test_a_list_does_need_body_text(self):
        """The menu is behind a tap, so a message with no body is a bare button."""
        response = self._post(name="Menu2", body="", list_content=LIST_CONTENT)

        self.assertEqual(response.status_code, 400)
        self.assertIn("body", str(response.json()))

    def test_a_message_cannot_be_two_interactive_kinds_at_once(self):
        response = self._post(name="Both", list_content=LIST_CONTENT, poll_content=POLL_CONTENT)

        self.assertEqual(response.status_code, 400)
        self.assertIn("not both", str(response.json()))

    def test_buttons_and_a_list_cannot_be_combined_either(self):
        response = self._post(
            name="Mixed", buttons=[{"title": "Yes"}], list_content=LIST_CONTENT,
        )

        self.assertEqual(response.status_code, 400)

    def test_a_call_button_template_saves_with_a_normalised_number(self):
        response = self._post(
            name="Call", buttons=[{"type": "CALL", "title": "Call now", "phone": "+91 98765 43210"}],
        )

        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["buttons"][0]["phone"], "+919876543210")

    def test_variables_inside_list_rows_are_detected(self):
        response = self._post(
            name="VarMenu",
            list_content={
                "sections": [{"rows": [{"title": "Reorder {{last_item}}"}]}],
            },
        )

        self.assertIn("last_item", response.json()["detected_variables"])

    def test_a_template_of_another_workspace_is_not_reachable(self):
        other = Tenant.objects.create(name="Other", slug="content-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="content-other-owner", password="x", tenant=other,
        )
        foreign = MessageTemplate.objects.create(
            tenant=other, created_by=other_user, name="Theirs", body="x", poll_content=POLL_CONTENT,
        )

        self.assertEqual(
            self.client.get(f"/api/v1/templates/templates/{foreign.pk}/").status_code, 404,
        )


class DraftPreviewApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Preview", slug="preview-co", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="preview-owner", password="x", tenant=self.tenant,
        )
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Aman", phone="+919876543210",
            custom_fields={"order_id": "ORD123"},
        )
        self.cloud = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="Cloud line",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API, status=WhatsAppAccount.Status.CONNECTED,
        )
        self.qr = WhatsAppAccount.objects.create(
            tenant=self.tenant, user=self.user, name="QR line",
            connection_method=WhatsAppAccount.ConnectionMethod.QR, status=WhatsAppAccount.Status.CONNECTED,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _preview(self, **payload):
        return self.client.post("/api/v1/templates/templates/draft-preview/", payload, format="json")

    def test_it_renders_variables_against_a_real_contact(self):
        response = self._preview(
            body="Hello {{name}}, order {{order_id}} is ready.", contact_id=self.contact.pk,
        )

        self.assertEqual(response.json()["body"], "Hello Aman, order ORD123 is ready.")

    def test_it_renders_against_supplied_sample_data_when_there_is_no_contact(self):
        response = self._preview(body="Hello [Name]", sample_data={"Name": "Sample"})

        self.assertEqual(response.json()["body"], "Hello Sample")

    def test_a_contact_from_another_workspace_is_a_404(self):
        other = Tenant.objects.create(name="Other", slug="preview-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="preview-other-owner", password="x", tenant=other,
        )
        foreign = Contact.objects.create(tenant=other, created_by=other_user, phone="+919876543299")

        self.assertEqual(self._preview(body="x", contact_id=foreign.pk).status_code, 404)

    def test_a_list_on_a_cloud_number_is_reported_as_native(self):
        response = self._preview(
            body="Please choose.", list_content=LIST_CONTENT, whatsapp_account_id=self.cloud.pk,
        )

        delivery = response.json()["delivery"]
        self.assertTrue(delivery["native"])
        self.assertEqual(delivery["as_sent"], "")

    def test_a_list_on_a_qr_number_reports_the_exact_text_that_will_be_sent(self):
        """
        The composer's "as sent" is produced by the same function the sender
        calls, so this is a test that the product cannot misdescribe what a
        recipient receives.
        """
        from core.interactive import clean_list_content, list_as_text

        with configured_qr_provider():
            response = self._preview(
                body="Please choose.", list_content=LIST_CONTENT, whatsapp_account_id=self.qr.pk,
            )

        delivery = response.json()["delivery"]
        self.assertFalse(delivery["native"])
        self.assertTrue(delivery["note"])
        self.assertEqual(
            delivery["as_sent"], list_as_text("Please choose.", clean_list_content(LIST_CONTENT)),
        )

    def test_a_poll_is_native_on_a_qr_number_and_not_on_a_cloud_one(self):
        """The inverse of buttons and lists, and the reason the layer exists."""
        with configured_qr_provider():
            on_qr = self._preview(poll_content=POLL_CONTENT, whatsapp_account_id=self.qr.pk).json()
            on_cloud = self._preview(poll_content=POLL_CONTENT, whatsapp_account_id=self.cloud.pk).json()

        self.assertTrue(on_qr["delivery"]["native"])
        self.assertFalse(on_cloud["delivery"]["native"])

    def test_a_mixed_button_list_reports_what_stays_tappable_on_a_cloud_number(self):
        """
        The one case where "not native" does not mean "flattened to text".
        WhatsApp has no envelope carrying reply and link buttons at once, so
        the reply buttons keep their tap and the link is written into the body
        -- and the composer has to show exactly that, because it is what the
        Cloud API provider builds from the same `plan_delivery` call.
        """
        response = self._preview(
            body="Coming on Friday?",
            buttons=[
                {"type": "REPLY", "title": "Yes"},
                {"type": "URL", "title": "Directions", "url": "https://example.test/map"},
            ],
            whatsapp_account_id=self.cloud.pk,
        )

        delivery = response.json()["delivery"]
        self.assertFalse(delivery["native"])
        self.assertEqual(
            delivery["as_sent"], "Coming on Friday?" + chr(10) * 2 + "Directions: https://example.test/map",
        )
        self.assertEqual([button["title"] for button in delivery["as_sent_buttons"]], ["Yes"])
        self.assertIn("reply buttons stay tappable", delivery["note"])

    def test_a_mixed_button_list_on_a_qr_number_is_flattened_whole(self):
        """Nothing survives as a button there, so nothing is reported as one."""
        with configured_qr_provider():
            response = self._preview(
                body="Coming on Friday?",
                buttons=[
                    {"type": "REPLY", "title": "Yes"},
                    {"type": "CALL", "title": "Call us", "phone": "+919876543210"},
                ],
                whatsapp_account_id=self.qr.pk,
            )

        delivery = response.json()["delivery"]
        self.assertFalse(delivery["native"])
        self.assertEqual(delivery["as_sent_buttons"], [])
        self.assertIn("1. Yes", delivery["as_sent"])
        self.assertIn("Call us: +919876543210", delivery["as_sent"])

    def test_reply_buttons_alone_are_still_native_on_a_cloud_number(self):
        response = self._preview(
            body="Coming on Friday?",
            buttons=[{"type": "REPLY", "title": "Yes"}, {"type": "REPLY", "title": "No"}],
            whatsapp_account_id=self.cloud.pk,
        )

        delivery = response.json()["delivery"]
        self.assertTrue(delivery["native"])
        self.assertEqual(delivery["as_sent"], "")
        self.assertEqual(delivery["note"], "")

    def test_it_says_whether_any_connected_number_could_send_this_natively(self):
        response = self._preview(body="Choose.", list_content=LIST_CONTENT)

        self.assertTrue(response.json()["delivery"]["any_native"])

    def test_it_lists_only_this_workspaces_numbers(self):
        other = Tenant.objects.create(name="Other", slug="preview-other-2", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username="preview-other-2-owner", password="x", tenant=other,
        )
        WhatsAppAccount.objects.create(
            tenant=other, user=other_user, name="Theirs",
            connection_method=WhatsAppAccount.ConnectionMethod.CLOUD_API, status=WhatsAppAccount.Status.CONNECTED,
        )

        response = self._preview(body="x")

        names = {row["name"] for row in response.json()["delivery"]["accounts"]}
        self.assertEqual(names, {"Cloud line", "QR line"})

    def test_half_finished_content_still_previews(self):
        """
        Deliberately a rendering endpoint, not a validating one: an author
        half-way through typing a list has an invalid list, and refusing to
        draw it would make the preview useless exactly when it is wanted.
        """
        response = self._preview(body="Hi", list_content={"sections": [{"rows": []}]})

        self.assertEqual(response.status_code, 200)

    def test_it_requires_authentication(self):
        self.assertEqual(
            APIClient().post("/api/v1/templates/templates/draft-preview/", {}, format="json").status_code,
            401,
        )
