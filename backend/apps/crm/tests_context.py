"""
Tests for the customer context layer and the business profile that sits
beside it -- the two things every agent on the platform now reads from.

The assertions divide into three kinds, and the third is the one that matters
most: that the context is *correct*, that it is *bounded*, and that it never
says something to a model it should not have said. A prompt is the one place
where a leaked row is not a wrong API response but one customer's private
business read out to another.
"""

import itertools
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contacts.models import Contact, ContactTag
from apps.crm import context as customer_context
from apps.crm import services
from apps.crm.models import Deal, Task
from apps.inbox.models import Conversation
from apps.tenants.models import BusinessProfile, Tenant
from apps.whatsapp.models import WhatsAppAccount

_ids = itertools.count(1)
_phones = itertools.count(920000000)


def _account(tenant, user):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number_id=str(next(_ids)),
        status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class ContextBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(
            name="Acme Cycles", slug=f"acme-ctx-{next(_ids)}", kind=Tenant.Kind.CUSTOMER,
        )
        self.user = get_user_model().objects.create_user(
            username=f"ctx-owner-{next(_ids)}", password="x", tenant=self.tenant,
            first_name="Priya", last_name="Sharma",
        )
        self.account = _account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _contact(self, name="Ravi", **fields):
        return Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name=name, phone=f"+{next(_phones)}", **fields,
        )

    def _deal(self, contact, name="Bulk order", value=50000, **fields):
        return services.create_deal(
            tenant=self.tenant, contact=contact, name=name, value=value, actor=self.user, **fields
        )


class CustomerContextTests(ContextBase):
    def test_a_brand_new_contact_reads_as_a_line_not_a_blank_form(self):
        """
        The commonest customer on any account has no deals, no tasks and no
        history, and is exactly the case where rendering every empty section
        would cost the most attention for the least information.
        """
        contact = self._contact(name="Ravi")

        prompt = customer_context.build(contact).to_prompt()

        self.assertIn("Ravi", prompt)
        self.assertNotIn("Open sales", prompt)
        self.assertNotIn("Outstanding with our team", prompt)
        self.assertNotIn("None", prompt)
        self.assertLessEqual(len(prompt.splitlines()), 3)

    def test_the_things_an_agent_needs_all_reach_the_prompt(self):
        contact = self._contact(name="Ravi", company="Ravi Traders", owner=self.user)
        tag = ContactTag.objects.create(tenant=self.tenant, name="Wholesale")
        contact.tags.add(tag)
        self._deal(contact, name="Fifty cycles", value=250000)
        services.create_task(
            tenant=self.tenant, title="Send the quotation", due_at=timezone.now() - timedelta(days=1),
            contact=contact, actor=self.user,
        )
        # Set last, because opening a deal rescores the contact from its own
        # signals and would overwrite anything written before it.
        Contact.objects.filter(pk=contact.pk).update(
            lead_score=72,
            lifecycle_status=Contact.Lifecycle.QUALIFIED,
            last_inbound_at=timezone.now() - timedelta(days=2),
            last_outbound_at=timezone.now() - timedelta(days=1),
        )
        contact.refresh_from_db()

        built = customer_context.build(contact)
        prompt = built.to_prompt()

        self.assertEqual(built.score_band, "HOT")
        self.assertIn("Ravi Traders", prompt)
        self.assertIn("hot lead", prompt)
        self.assertIn("Priya Sharma", prompt)
        self.assertIn("Wholesale", prompt)
        self.assertIn("Fifty cycles", prompt)
        self.assertIn("Send the quotation", prompt)
        # An overdue task is worth saying so about: it is the difference
        # between "we owe them something" and "we are late".
        self.assertIn("overdue", prompt)
        self.assertIn("2 days ago", prompt)

    def test_a_long_silence_is_described_rather_than_counted(self):
        contact = self._contact()
        contact.last_inbound_at = timezone.now() - timedelta(days=400)
        contact.save()

        prompt = customer_context.build(contact).to_prompt()

        self.assertIn("over 90 days ago", prompt)
        self.assertNotIn("400", prompt)

    def test_lists_are_capped_so_one_reply_cannot_become_a_huge_prompt(self):
        contact = self._contact()
        for index in range(MANY := 12):
            self._deal(contact, name=f"Deal {index}", value=1000 + index)
            services.create_task(
                tenant=self.tenant, title=f"Task {index}", due_at=timezone.now() + timedelta(days=index + 1),
                contact=contact, actor=self.user,
            )

        built = customer_context.build(contact)

        self.assertEqual(Deal.objects.filter(contact=contact).count(), MANY)
        self.assertEqual(len(built.open_deals), customer_context.MAX_DEALS)
        self.assertEqual(len(built.open_tasks), customer_context.MAX_TASKS)

    def test_closed_deals_are_counted_but_not_listed(self):
        contact = self._contact()
        deal = self._deal(contact, name="Old order")
        won = deal.pipeline.stages.filter(kind="WON").first()
        services.move_deal(deal, won, actor=self.user)

        built = customer_context.build(contact)
        prompt = built.to_prompt()

        self.assertEqual(built.open_deals, [])
        self.assertEqual(built.won_deal_count, 1)
        self.assertIn("bought from us before", prompt)
        self.assertNotIn("Old order", prompt)

    def test_another_tenants_records_can_never_reach_the_prompt(self):
        """
        The whole feature in one test. Two customers on two tenants share a
        phone number, which is legal -- the uniqueness constraint is per
        tenant -- and the context for one must contain nothing of the other's.
        """
        other_tenant = Tenant.objects.create(
            name="Rival", slug=f"rival-{next(_ids)}", kind=Tenant.Kind.CUSTOMER,
        )
        other_user = get_user_model().objects.create_user(
            username=f"rival-{next(_ids)}", password="x", tenant=other_tenant,
        )
        shared_phone = f"+{next(_phones)}"
        mine = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Ravi", phone=shared_phone,
        )
        theirs = Contact.objects.create(
            tenant=other_tenant, created_by=other_user, name="Ravi", phone=shared_phone,
        )
        services.create_deal(
            tenant=other_tenant, contact=theirs, name="Rival secret order", value=999999, actor=other_user,
        )
        services.create_task(
            tenant=other_tenant, title="Rival secret task", due_at=timezone.now(),
            contact=theirs, actor=other_user,
        )

        prompt = customer_context.build(mine).to_prompt()

        self.assertNotIn("Rival secret order", prompt)
        self.assertNotIn("Rival secret task", prompt)
        self.assertNotIn("999999", prompt)

    def test_context_for_a_conversation_carries_who_owns_the_thread(self):
        contact = self._contact()
        conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=contact, assigned_agent=self.user,
        )

        built = customer_context.for_conversation(conversation)

        self.assertEqual(built.contact_id, contact.pk)
        self.assertEqual(built.conversation["assigned_to"], "Priya Sharma")
        # Who is handling the chat is for the platform, not for the customer
        # to be told about mid-answer.
        self.assertNotIn("assigned", built.to_prompt().lower())

    def test_no_contact_is_falsy_rather_than_an_error(self):
        built = customer_context.build(None)

        self.assertFalse(built)
        self.assertEqual(built.to_dict()["contact_id"], 0)

    def test_assembling_a_customer_costs_a_bounded_number_of_queries(self):
        """
        This runs on every AI reply, so the cost has to be a constant rather
        than something that grows with how much business the customer has
        done. Ten deals must cost what one deal costs.
        """
        contact = self._contact()
        for index in range(10):
            self._deal(contact, name=f"Deal {index}")

        contact = Contact.objects.select_related("owner").get(pk=contact.pk)
        with self.assertNumQueries(5):
            customer_context.build(contact).to_prompt()


class BusinessProfileTests(ContextBase):
    def test_an_unconfigured_profile_renders_nothing(self):
        profile = BusinessProfile.for_tenant(self.tenant.pk)

        self.assertFalse(profile.is_configured)
        self.assertEqual(profile.to_prompt(), "")
        # Reading must never write: the agents call this on the inbound path.
        self.assertFalse(BusinessProfile.objects.filter(tenant=self.tenant).exists())

    def test_a_filled_profile_reads_as_a_description_of_the_business(self):
        BusinessProfile.objects.create(
            tenant=self.tenant,
            description="We sell and service bicycles.",
            products="Road bikes, city bikes, helmets",
            locations="Pune and Mumbai",
            policies="Never quote a discount above 10%.",
            languages="English, Hindi",
        )

        prompt = BusinessProfile.for_tenant(self.tenant.pk).to_prompt()

        self.assertIn("Acme Cycles", prompt)
        self.assertIn("We sell and service bicycles.", prompt)
        self.assertIn("Road bikes", prompt)
        self.assertIn("Never quote a discount above 10%.", prompt)
        self.assertIn("English, Hindi", prompt)
        # Nothing empty gets a heading of its own.
        self.assertNotIn("Services we offer", prompt)

    def test_a_trading_name_overrides_the_account_name(self):
        profile = BusinessProfile.objects.create(
            tenant=self.tenant, trading_name="Acme Bikes", description="Bicycles.",
        )

        self.assertEqual(profile.display_name(), "Acme Bikes")
        self.assertIn("Acme Bikes", profile.to_prompt())

    def test_opening_hours_come_from_the_team_screen_not_a_second_copy(self):
        from apps.teams.models import WorkingHours

        WorkingHours.objects.create(
            tenant=self.tenant, weekday=WorkingHours.Weekday.MONDAY, is_open=True,
            opens_at="09:00", closes_at="18:00",
        )
        profile = BusinessProfile.objects.create(tenant=self.tenant, description="Bicycles.")

        self.assertIn("Monday 09:00-18:00", profile.to_prompt())

    def test_the_profile_endpoint_round_trips_and_previews_the_prompt(self):
        response = self.client.patch(
            "/api/v1/tenants/business-profile/",
            {"description": "We sell and service bicycles.", "tone": "PROFESSIONAL"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["is_configured"])
        # The preview is what makes this honest: the user sees the text the
        # model will be given, not a paraphrase of it.
        self.assertIn("We sell and service bicycles.", response.data["preview"])
        self.assertIn("professional", response.data["preview"])

    def test_the_profile_endpoint_is_tenant_scoped(self):
        other = Tenant.objects.create(name="Rival", slug=f"rival-{next(_ids)}", kind=Tenant.Kind.CUSTOMER)
        BusinessProfile.objects.create(tenant=other, description="Rival secrets.")

        response = self.client.get("/api/v1/tenants/business-profile/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Rival secrets", response.data["preview"])


class ContactContextEndpointTests(ContextBase):
    def test_it_returns_both_the_record_and_the_text_the_model_gets(self):
        contact = self._contact(name="Ravi")
        self._deal(contact, name="Fifty cycles")

        response = self.client.get(f"/api/v1/crm/contacts/{contact.pk}/context/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["context"]["name"], "Ravi")
        self.assertEqual(len(response.data["context"]["open_deals"]), 1)
        self.assertIn("Fifty cycles", response.data["prompt"])

    def test_another_tenants_contact_is_not_found_rather_than_forbidden(self):
        other = Tenant.objects.create(name="Rival", slug=f"rival-{next(_ids)}", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(
            username=f"rival-{next(_ids)}", password="x", tenant=other,
        )
        theirs = Contact.objects.create(
            tenant=other, created_by=other_user, name="Secret", phone=f"+{next(_phones)}",
        )

        response = self.client.get(f"/api/v1/crm/contacts/{theirs.pk}/context/")

        self.assertEqual(response.status_code, 404)
