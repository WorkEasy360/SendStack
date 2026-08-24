from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services
from .models import ClickToChatLink, LeadEvent, LinkVisit

PUBLIC = dict(LINK_TRACKING_BASE_URL="https://msg.example.com")


def _account(tenant, user, phone="+919876500001"):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number=phone, phone_number_id="1",
        status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


@override_settings(**PUBLIC)
class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=f"lg-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"lg-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15556660001",
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )

    def _link(self, **kwargs):
        kwargs.setdefault("name", "Counter poster")
        kwargs.setdefault("prefilled_message", "Hi! I'd like to know more.")
        return ClickToChatLink.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, code=kwargs.pop("code", "ab12cd"), **kwargs,
        )

    def _inbound(self, body):
        message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, body=body,
            status=Message.Status.DELIVERED,
        )
        return message


@override_settings(**PUBLIC)
class LinkBuildingTests(Fixture):
    def test_the_whatsapp_url_uses_the_accounts_number(self):
        url = services.whatsapp_url(self._link())

        self.assertTrue(url.startswith("https://wa.me/919876500001?text="), url)

    def test_the_reference_is_appended_to_the_prefilled_text(self):
        link = self._link()

        self.assertIn("#ab12cd", services.prefilled_text(link))
        self.assertTrue(services.prefilled_text(link).startswith("Hi!"))

    def test_the_reference_can_be_turned_off(self):
        link = self._link(include_reference=False)

        self.assertEqual(services.prefilled_text(link), "Hi! I'd like to know more.")

    def test_a_number_that_is_not_connected_yet_produces_no_url(self):
        """A wa.me link built from a blank number sends the customer to an error page."""
        self.account.phone_number = ""
        self.account.save(update_fields=["phone_number"])

        self.assertEqual(services.whatsapp_url(self._link()), "")

    def test_the_qr_encodes_the_short_link(self):
        svg = services.qr_svg(self._link())

        self.assertIn("<svg", svg)
        self.assertIn("path", svg)


@override_settings(**PUBLIC)
class RedirectTests(Fixture):
    def test_tapping_the_link_counts_it_and_opens_whatsapp(self):
        link = self._link()

        response = self.client.get("/c/ab12cd")

        self.assertEqual(response.status_code, 302)
        self.assertIn("wa.me/919876500001", response["Location"])
        link.refresh_from_db()
        self.assertEqual(link.click_count, 1)
        self.assertEqual(LinkVisit.objects.filter(link=link).count(), 1)

    def test_an_unknown_code_is_a_404(self):
        self.assertEqual(self.client.get("/c/nope99").status_code, 404)

    def test_an_inactive_link_is_a_404(self):
        self._link(is_active=False)

        self.assertEqual(self.client.get("/c/ab12cd").status_code, 404)

    def test_a_link_whose_number_is_not_ready_says_so_rather_than_misdirecting(self):
        self.account.phone_number = ""
        self.account.save(update_fields=["phone_number"])
        self._link()

        response = self.client.get("/c/ab12cd")

        self.assertEqual(response.status_code, 503)

    def test_the_widget_script_is_javascript_and_points_at_the_link(self):
        self._link()

        response = self.client.get("/w/ab12cd.js")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/javascript")
        self.assertIn("https://msg.example.com/c/ab12cd", response.content.decode())


@override_settings(**PUBLIC)
class AttributionTests(Fixture):
    def test_a_message_carrying_the_reference_creates_a_lead(self):
        link = self._link()
        message = self._inbound("Hi! I'd like to know more.\n\n#ab12cd")

        event = services.attribute_inbound(message)

        self.assertEqual(event.kind, LeadEvent.Kind.CLICK_TO_CHAT)
        self.assertEqual(event.link_id, link.pk)
        link.refresh_from_db()
        self.assertEqual(link.lead_count, 1)

    def test_the_contact_records_where_they_came_from(self):
        self._link(utm_source="instagram")
        services.attribute_inbound(self._inbound("Hello #ab12cd"))

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.source, LeadEvent.Kind.CLICK_TO_CHAT)
        self.assertEqual(self.contact.source_detail["utm_source"], "instagram")

    def test_the_same_person_tapping_twice_is_one_lead(self):
        link = self._link()
        services.attribute_inbound(self._inbound("Hi #ab12cd"))
        services.attribute_inbound(self._inbound("Hi again #ab12cd"))

        link.refresh_from_db()
        self.assertEqual(link.lead_count, 1)
        self.assertEqual(LeadEvent.objects.filter(link=link).count(), 2)

    def test_first_touch_is_never_overwritten(self):
        self._link()
        services.attribute_inbound(self._inbound("Hi #ab12cd"))

        message = self._inbound("Saw your ad")
        message.referral_payload = {"source_type": "ad", "headline": "Diwali sale"}
        services.attribute_inbound(message)

        self.contact.refresh_from_db()
        self.assertEqual(self.contact.source, LeadEvent.Kind.CLICK_TO_CHAT)
        self.assertEqual(LeadEvent.objects.filter(contact=self.contact).count(), 2)

    def test_an_ad_referral_is_captured_whole(self):
        message = self._inbound("Is this available?")
        message.referral_payload = {
            "source_type": "ad", "source_id": "1201", "headline": "Diwali sale", "ctwa_clid": "abc",
        }

        event = services.attribute_inbound(message)

        self.assertEqual(event.kind, LeadEvent.Kind.AD_REFERRAL)
        self.assertEqual(event.payload["ctwa_clid"], "abc")
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.source, LeadEvent.Kind.AD_REFERRAL)

    def test_an_ordinary_message_attributes_nothing(self):
        self._link()

        self.assertIsNone(services.attribute_inbound(self._inbound("what time do you close?")))

    def test_a_reference_belonging_to_another_tenant_is_ignored(self):
        other = Tenant.objects.create(name="Other", slug="lg-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="lg-other-user", password="x", tenant=other)
        ClickToChatLink.objects.create(
            tenant=other, whatsapp_account=_account(other, other_user, phone="+919876500002"),
            name="Theirs", code="zz99zz",
        )

        self.assertIsNone(services.attribute_inbound(self._inbound("hello #zz99zz")))

    def test_the_receiver_attributes_an_inbound_message(self):
        self._link()
        message = self._inbound("Hi #ab12cd")

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=True)

        self.assertTrue(LeadEvent.objects.filter(contact=self.contact).exists())


@override_settings(**PUBLIC)
class LeadgenApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_creating_a_link_mints_a_code(self):
        response = self.client.post(
            "/api/v1/leadgen/links/",
            {"name": "Shop counter", "whatsapp_account": self.account.pk, "prefilled_message": "Hi!"},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["code"])
        self.assertIn("wa.me", response.data["whatsapp_url"])

    def test_the_preview_shows_exactly_what_the_customer_will_send(self):
        response = self.client.post(
            "/api/v1/leadgen/links/",
            {"name": "Bio link", "whatsapp_account": self.account.pk, "prefilled_message": "Hi there"},
            format="json",
        )

        self.assertIn(f"#{response.data['code']}", response.data["prefilled_preview"])

    def test_a_number_from_another_tenant_is_refused(self):
        other = Tenant.objects.create(name="Other", slug="lg-api-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="lg-api-other-u", password="x", tenant=other)
        foreign = _account(other, other_user, phone="+919876500003")

        response = self.client.post(
            "/api/v1/leadgen/links/", {"name": "x", "whatsapp_account": foreign.pk}, format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_the_qr_endpoint_returns_svg(self):
        link = self._link()

        response = self.client.get(f"/api/v1/leadgen/links/{link.pk}/qr/")

        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertIn(b"<svg", response.content)

    def test_the_snippet_endpoint_hands_over_everything_needed_to_publish(self):
        link = self._link()

        response = self.client.get(f"/api/v1/leadgen/links/{link.pk}/snippet/")

        self.assertEqual(response.data["short_url"], "https://msg.example.com/c/ab12cd")
        self.assertIn("<script", response.data["embed_snippet"])
        self.assertIn("/w/ab12cd.js", response.data["embed_snippet"])

    def test_the_summary_counts_people_not_taps(self):
        link = self._link()
        LeadEvent.objects.create(
            tenant=self.tenant, contact=self.contact, link=link, kind=LeadEvent.Kind.CLICK_TO_CHAT,
        )
        LeadEvent.objects.create(
            tenant=self.tenant, contact=self.contact, link=link, kind=LeadEvent.Kind.CLICK_TO_CHAT,
        )

        response = self.client.get("/api/v1/leadgen/leads/summary/")

        self.assertEqual(response.data["by_kind"][0]["contacts"], 1)
        self.assertEqual(response.data["by_kind"][0]["events"], 2)

    def test_another_tenants_links_are_invisible(self):
        other = Tenant.objects.create(name="Other", slug="lg-vis-other", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="lg-vis-other-u", password="x", tenant=other)
        ClickToChatLink.objects.create(
            tenant=other, whatsapp_account=_account(other, other_user, phone="+919876500004"),
            name="Theirs", code="hidden",
        )
        self._link()

        response = self.client.get("/api/v1/leadgen/links/")

        self.assertEqual([row["code"] for row in response.data["results"]], ["ab12cd"])
