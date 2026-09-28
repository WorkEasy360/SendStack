from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.tenants.models import Tenant
from core.media import resolve_media_items, spec_from_object

from .models import MessageTemplate, extract_variables
from .services import render_template


class VariableExtractionTests(TestCase):
    def test_extracts_unique_bracketed_names_in_order(self):
        self.assertEqual(
            extract_variables("Hi [Name], your order [OrderID] is ready. Thanks [Name]!"),
            ["Name", "OrderID"],
        )


class RenderTemplateTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-tpl", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-tpl-owner", password="x", tenant=self.tenant)

    def test_render_uses_contact_fields_and_custom_fields(self):
        contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551234567",
            custom_fields={"OrderID": "A100"},
        )
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Order Ready",
            body="Hi [Name], your order [OrderID] is ready. Amount: [Amount]",
            variables={"Amount": "N/A"},
        )

        rendered = render_template(template, contact)

        self.assertEqual(rendered["body"], "Hi Priya, your order A100 is ready. Amount: N/A")

    def test_render_falls_back_when_field_missing(self):
        contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="", phone="+15551234568")
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Greeting",
            body="Hi [Name]!", variables={"Name": "Customer"},
        )

        rendered = render_template(template, contact)

        self.assertEqual(rendered["body"], "Hi Customer!")


class MessageTemplateApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-tpl-api", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-tpl-api-owner", password="x", tenant=self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_create_text_template(self):
        response = self.client.post(
            "/api/v1/templates/templates/",
            {"name": "Welcome", "message_type": "TEXT", "body": "Hi [Name]!"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["detected_variables"], ["Name"])

    def test_media_template_requires_media_url(self):
        response = self.client.post(
            "/api/v1/templates/templates/",
            {"name": "Flyer", "message_type": "IMAGE", "body": "Check this out"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("media_url", response.json())

    def test_preview_with_sample_data(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Greeting", body="Hi [Name]!",
        )

        response = self.client.post(
            f"/api/v1/templates/templates/{template.pk}/preview/",
            {"sample_data": {"Name": "Priya"}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["body"], "Hi Priya!")


class TemplateMediaItemsApiTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-tpl-media", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-tpl-media-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_several_attachments_are_saved_and_the_first_mirrors_the_legacy_columns(self):
        response = self.client.post(
            "/api/v1/templates/templates/",
            {
                "name": "Brochure",
                "message_type": "TEXT",
                "body": "Have a look",
                "media_items": [
                    {"url": "https://cdn.example.com/a.png", "type": "IMAGE"},
                    {"url": "https://cdn.example.com/b.mp4", "type": "VIDEO"},
                    {"url": "https://cdn.example.com/c.pdf", "type": "DOCUMENT", "filename": "c.pdf"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        template = MessageTemplate.objects.get(pk=response.json()["id"])
        self.assertEqual(len(template.media_items), 3)
        # Everything that predates multi-attachment support reads these three.
        self.assertEqual(template.message_type, "IMAGE")
        self.assertEqual(template.media_url, "https://cdn.example.com/a.png")

    def test_a_document_attachment_carries_its_filename_over(self):
        response = self.client.post(
            "/api/v1/templates/templates/",
            {
                "name": "Invoice",
                "body": "Attached",
                "media_items": [{"url": "https://cdn.example.com/i.pdf", "type": "DOCUMENT", "filename": "invoice.pdf"}],
            },
            format="json",
        )

        template = MessageTemplate.objects.get(pk=response.json()["id"])
        self.assertEqual((template.message_type, template.filename), ("DOCUMENT", "invoice.pdf"))

    def test_a_document_attachment_without_a_filename_is_rejected(self):
        response = self.client.post(
            "/api/v1/templates/templates/",
            {"name": "Bad", "body": "x", "media_items": [{"url": "https://cdn.example.com/i.pdf", "type": "DOCUMENT"}]},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("filename", str(response.json()))

    def test_clearing_the_attachments_clears_the_legacy_columns_too(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Flyer", body="Hi",
            message_type=MessageTemplate.MessageType.IMAGE, media_url="https://cdn.example.com/old.png",
            media_items=[{"url": "https://cdn.example.com/old.png", "type": "IMAGE", "filename": "", "caption": ""}],
        )

        response = self.client.patch(
            f"/api/v1/templates/templates/{template.pk}/",
            {"media_items": [], "message_type": "TEXT"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        template.refresh_from_db()
        self.assertEqual((template.media_items, template.media_url), ([], ""))

    def test_posting_only_a_media_url_still_works(self):
        """The pre-existing single-attachment API shape must not change."""
        response = self.client.post(
            "/api/v1/templates/templates/",
            {"name": "Legacy", "message_type": "IMAGE", "body": "Hi", "media_url": "https://cdn.example.com/x.png"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        template = MessageTemplate.objects.get(pk=response.json()["id"])
        self.assertEqual((template.media_url, template.media_items), ("https://cdn.example.com/x.png", []))
        # ...and the send path still resolves one attachment out of it.
        self.assertEqual(len(resolve_media_items(spec_from_object(template))), 1)


class TemplateBulkActionTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-tpl-bulk", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-tpl-bulk-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.templates = [
            MessageTemplate.objects.create(tenant=self.tenant, created_by=self.user, name=f"T{n}", body="Hi")
            for n in range(3)
        ]

    def _bulk(self, action, ids):
        return self.client.post(
            "/api/v1/templates/templates/bulk/", {"action": action, "ids": ids}, format="json"
        )

    def test_bulk_delete_removes_only_the_selected_templates(self):
        response = self._bulk("delete", [self.templates[0].pk, self.templates[2].pk])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual(list(MessageTemplate.objects.values_list("pk", flat=True)), [self.templates[1].pk])

    def test_bulk_deactivate_then_activate(self):
        ids = [template.pk for template in self.templates]

        self._bulk("deactivate", ids)
        self.assertEqual(MessageTemplate.objects.filter(is_active=True).count(), 0)

        response = self._bulk("activate", ids)
        self.assertEqual(response.json()["detail"], "3 templates activated.")
        self.assertEqual(MessageTemplate.objects.filter(is_active=True).count(), 3)

    def test_another_tenants_template_is_reported_not_touched(self):
        other = Tenant.objects.create(name="Other", slug="other-tpl-bulk", kind=Tenant.Kind.CUSTOMER)
        theirs = MessageTemplate.objects.create(tenant=other, name="Theirs", body="Hi")

        response = self._bulk("delete", [self.templates[0].pk, theirs.pk])

        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.json()["errors"], [{"id": theirs.pk, "detail": "Not found."}])
        self.assertTrue(MessageTemplate.objects.filter(pk=theirs.pk).exists())

    def test_an_unknown_action_is_refused(self):
        response = self._bulk("explode", [self.templates[0].pk])

        self.assertEqual(response.status_code, 400)
        self.assertIn("activate", response.json()["detail"])

    def test_an_empty_selection_is_refused(self):
        response = self._bulk("delete", [])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MessageTemplate.objects.count(), 3)


class TemplateButtonsApiTests(TestCase):
    """
    Buttons are validated on save, not at send time: WhatsApp's limits (three
    reply buttons, one link, one call button, 20-character labels) are what
    make an interactive message deliverable at all.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-tpl-buttons", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username="acme-tpl-buttons-owner", password="x", tenant=self.tenant
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, **overrides):
        payload = {"name": "Confirm", "message_type": "TEXT", "body": "Coming on Friday?"}
        payload.update(overrides)
        return self.client.post("/api/v1/templates/templates/", payload, format="json")

    def test_reply_buttons_are_saved_with_ids_derived_from_their_labels(self):
        response = self._post(buttons=[{"title": "Yes, I'll be there"}, {"title": "No"}])

        self.assertEqual(response.status_code, 201, response.content)
        buttons = response.json()["buttons"]
        self.assertEqual([button["id"] for button in buttons], ["yes_i_ll_be_there", "no"])
        self.assertEqual([button["type"] for button in buttons], ["REPLY", "REPLY"])

    def test_a_supplied_id_is_kept_so_automation_keeps_matching_after_an_edit(self):
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Confirm", body="Coming?",
            buttons=[{"type": "REPLY", "id": "rsvp_yes", "title": "Yes", "url": ""}],
        )

        response = self.client.patch(
            f"/api/v1/templates/templates/{template.pk}/",
            {"buttons": [{"type": "REPLY", "id": "rsvp_yes", "title": "Yes please", "url": ""}]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["buttons"][0]["id"], "rsvp_yes")

    def test_more_than_three_reply_buttons_is_rejected(self):
        response = self._post(buttons=[{"title": f"Option {n}"} for n in range(4)])

        self.assertEqual(response.status_code, 400)
        self.assertIn("3 reply buttons", str(response.json()))

    def test_reply_and_link_buttons_save_together(self):
        """
        The kinds combine on the way in even though only one of them can be
        tappable on the way out -- see core.buttons. Refusing the save would
        only push the author into pasting the link into the body by hand.
        """
        response = self._post(
            buttons=[{"title": "Yes"}, {"type": "URL", "title": "Shop", "url": "https://example.com"}]
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([button["type"] for button in response.json()["buttons"]], ["REPLY", "URL"])

    def test_a_second_link_button_is_still_refused(self):
        response = self._post(buttons=[
            {"type": "URL", "title": "Shop", "url": "https://example.com"},
            {"type": "URL", "title": "Blog", "url": "https://example.com/blog"},
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn("one link button", str(response.json()))

    def test_a_label_longer_than_whatsapp_allows_is_rejected(self):
        response = self._post(buttons=[{"title": "This label is far too long for WhatsApp"}])

        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 20", str(response.json()).lower())

    def test_a_link_button_needs_an_http_url(self):
        response = self._post(buttons=[{"type": "URL", "title": "Shop", "url": "javascript:alert(1)"}])

        self.assertEqual(response.status_code, 400)
        self.assertIn("http", str(response.json()))

    def test_buttons_without_body_text_are_rejected(self):
        response = self._post(body="", buttons=[{"title": "Yes"}])

        self.assertEqual(response.status_code, 400)
        self.assertIn("body", str(response.json()).lower())

    def test_an_image_template_can_carry_buttons(self):
        response = self._post(
            message_type="IMAGE",
            body="Like this one?",
            media_items=[{"url": "https://cdn.example.com/a.png", "type": "IMAGE"}],
            buttons=[{"title": "Yes"}, {"title": "Show me more"}],
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()["buttons"]), 2)
