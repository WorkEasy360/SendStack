import itertools
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contacts.models import Contact, ContactTag
from apps.crm import lifecycle, search, segments, services
from apps.crm.models import Activity, ContactNote, Deal, Pipeline, PipelineStage, Task
from apps.crm.scoring import band_for, score_contact, score_from_signals
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

_phones = itertools.count(910000000)
_provider_ids = itertools.count(1)


def _account(tenant, user, name="Line"):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name=name, phone_number_id=str(next(_provider_ids)),
        status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class CrmBase(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = Tenant.objects.create(name="Acme", slug=f"acme-crm-{next(_provider_ids)}", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"crm-owner-{next(_provider_ids)}", password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _contact(self, name="Ravi", **fields):
        return Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name=name,
            phone=f"+{next(_phones)}", **fields,
        )

    def _message(self, contact, *, direction=Message.Direction.OUTBOUND, when=None, **fields):
        when = when or timezone.now()
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=contact,
            to_phone=contact.phone, direction=direction,
            provider_message_id=f"wamid.{next(_provider_ids)}",
            status=Message.Status.SENT, sent_at=when, **fields,
        )
        # queued_at is auto_now_add, so it has to be forced after the insert
        # for any test that cares about a message being in the past.
        Message.objects.filter(pk=message.pk).update(queued_at=when)
        message.refresh_from_db()
        return message


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class LeadScoringTests(CrmBase):
    def test_a_silent_contact_scores_nothing(self):
        self.assertEqual(
            score_from_signals(last_inbound_at=None, inbound_count=0, outbound_count=0, open_deals=0, won_deals=0),
            0,
        )

    def test_replying_today_beats_replying_last_month(self):
        now = timezone.now()
        today = score_from_signals(
            last_inbound_at=now - timedelta(hours=2), inbound_count=1, outbound_count=1,
            open_deals=0, won_deals=0, now=now,
        )
        last_month = score_from_signals(
            last_inbound_at=now - timedelta(days=25), inbound_count=1, outbound_count=1,
            open_deals=0, won_deals=0, now=now,
        )
        self.assertGreater(today, last_month)

    def test_an_unsubscribed_contact_scores_zero_however_engaged(self):
        now = timezone.now()
        score = score_from_signals(
            last_inbound_at=now, inbound_count=30, outbound_count=30, open_deals=2, won_deals=2,
            subscribed=False, now=now,
        )
        self.assertEqual(score, 0)

    def test_one_reply_to_one_message_is_not_a_perfect_reply_rate(self):
        """A single exchange must not score like a long, responsive thread."""
        now = timezone.now()
        thin = score_from_signals(
            last_inbound_at=now, inbound_count=1, outbound_count=1, open_deals=0, won_deals=0, now=now,
        )
        thick = score_from_signals(
            last_inbound_at=now, inbound_count=6, outbound_count=6, open_deals=0, won_deals=0, now=now,
        )
        self.assertGreater(thick, thin)

    def test_the_score_never_leaves_zero_to_one_hundred(self):
        now = timezone.now()
        score = score_from_signals(
            last_inbound_at=now, inbound_count=500, outbound_count=500, open_deals=9, won_deals=9, now=now,
        )
        self.assertLessEqual(score, 100)
        self.assertGreaterEqual(score, 0)

    def test_bands_line_up_with_the_thresholds(self):
        self.assertEqual(band_for(90), "HOT")
        self.assertEqual(band_for(45), "WARM")
        self.assertEqual(band_for(5), "COLD")

    def test_scoring_a_real_contact_reads_their_messages(self):
        contact = self._contact()
        self._message(contact, direction=Message.Direction.OUTBOUND)
        self._message(contact, direction=Message.Direction.INBOUND)
        contact.refresh_from_db()
        score = score_contact(contact)
        self.assertGreater(score, 0)
        contact.refresh_from_db()
        self.assertEqual(contact.lead_score, score)
        self.assertIsNotNone(contact.lead_score_updated_at)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class LifecycleTests(CrmBase):
    def test_a_reply_promotes_a_new_contact_to_engaged(self):
        contact = self._contact()
        self.assertTrue(lifecycle.on_inbound_reply(contact))
        contact.refresh_from_db()
        self.assertEqual(contact.lifecycle_status, Contact.Lifecycle.ENGAGED)

    def test_promotion_never_demotes_a_customer(self):
        contact = self._contact(lifecycle_status=Contact.Lifecycle.CUSTOMER)
        self.assertFalse(lifecycle.on_inbound_reply(contact))
        contact.refresh_from_db()
        self.assertEqual(contact.lifecycle_status, Contact.Lifecycle.CUSTOMER)

    def test_a_status_change_lands_on_the_timeline(self):
        contact = self._contact()
        lifecycle.set_status(contact, Contact.Lifecycle.QUALIFIED, actor=self.user)
        entry = Activity.objects.filter(contact=contact, kind=Activity.Kind.STATUS_CHANGED).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["to"], Contact.Lifecycle.QUALIFIED)

    def test_the_silence_sweep_cools_an_engaged_contact_down(self):
        contact = self._contact(lifecycle_status=Contact.Lifecycle.ENGAGED)
        Contact.objects.filter(pk=contact.pk).update(last_inbound_at=timezone.now() - timedelta(days=120))
        self.assertEqual(lifecycle.mark_inactive_where_silent(self.tenant), 1)
        contact.refresh_from_db()
        self.assertEqual(contact.lifecycle_status, Contact.Lifecycle.INACTIVE)

    def test_the_silence_sweep_leaves_customers_alone(self):
        """Going quiet does not un-buy what somebody already bought."""
        contact = self._contact(lifecycle_status=Contact.Lifecycle.CUSTOMER)
        Contact.objects.filter(pk=contact.pk).update(last_inbound_at=timezone.now() - timedelta(days=400))
        lifecycle.mark_inactive_where_silent(self.tenant)
        contact.refresh_from_db()
        self.assertEqual(contact.lifecycle_status, Contact.Lifecycle.CUSTOMER)


# ---------------------------------------------------------------------------
# Smart segments
# ---------------------------------------------------------------------------

class SmartSegmentTests(CrmBase):
    def test_no_reply_finds_a_contact_we_messaged_and_who_went_quiet(self):
        contact = self._contact()
        Contact.objects.filter(pk=contact.pk).update(last_outbound_at=timezone.now() - timedelta(days=5))
        self.assertIn(contact, segments.resolve(self.tenant, "no_reply_3d"))

    def test_no_reply_excludes_somebody_who_answered_our_last_message(self):
        contact = self._contact()
        now = timezone.now()
        Contact.objects.filter(pk=contact.pk).update(
            last_outbound_at=now - timedelta(days=5), last_inbound_at=now - timedelta(days=4),
        )
        self.assertNotIn(contact, segments.resolve(self.tenant, "no_reply_3d"))

    def test_no_reply_excludes_an_unsubscribed_contact(self):
        contact = self._contact(subscribed=False)
        Contact.objects.filter(pk=contact.pk).update(last_outbound_at=timezone.now() - timedelta(days=5))
        self.assertNotIn(contact, segments.resolve(self.tenant, "no_reply_3d"))

    def test_hot_leads_uses_the_score(self):
        hot = self._contact("Hot", lead_score=85)
        cold = self._contact("Cold", lead_score=5)
        found = list(segments.resolve(self.tenant, "hot_leads"))
        self.assertIn(hot, found)
        self.assertNotIn(cold, found)

    def test_a_segment_never_reaches_another_tenant(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-seg", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-seg", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}", lead_score=99,
        )
        self.assertNotIn(stranger, segments.resolve(self.tenant, "hot_leads"))

    def test_follow_up_due_finds_a_contact_with_an_overdue_task(self):
        contact = self._contact()
        Task.objects.create(
            tenant=self.tenant, contact=contact, title="Call back",
            due_at=timezone.now() - timedelta(hours=1),
        )
        self.assertIn(contact, segments.resolve(self.tenant, "follow_up_due"))

    def test_unknown_segment_keys_resolve_to_nothing_rather_than_erroring(self):
        self.assertIsNone(segments.resolve(self.tenant, "not_a_segment"))

    def test_the_catalog_counts_every_segment(self):
        self._contact("Hot", lead_score=85)
        entries = segments.catalog(self.tenant)
        self.assertEqual(len(entries), len(segments.SMART_SEGMENTS))
        hot = next(entry for entry in entries if entry["key"] == "hot_leads")
        self.assertEqual(hot["count"], 1)


# ---------------------------------------------------------------------------
# Natural-language search
# ---------------------------------------------------------------------------

class SearchParsingTests(CrmBase):
    def test_it_understands_a_whole_sentence(self):
        parsed = search.parse("show me hot leads who haven't replied in 3 days")
        self.assertIn("Hot leads", parsed["applied"])
        self.assertIn("No reply in 3 days", parsed["applied"])
        self.assertEqual(parsed["text"], "")

    def test_weeks_and_months_become_days(self):
        self.assertIn("No reply in 14 days", search.parse("no reply for 2 weeks")["applied"])
        self.assertIn("No reply in 30 days", search.parse("not replied in a month")["applied"])

    def test_plain_text_survives_as_a_name_search(self):
        parsed = search.parse("customers named sharma")
        self.assertIn("Customers", parsed["applied"])
        self.assertIn("sharma", parsed["text"])

    def test_mine_resolves_against_the_requesting_user(self):
        parsed = search.parse("my leads", user=self.user)
        self.assertIn("Owned by me", parsed["applied"])

    def test_an_unparseable_query_still_searches(self):
        parsed = search.parse("zzzqqq")
        self.assertEqual(parsed["applied"], [])
        self.assertEqual(parsed["text"], "zzzqqq")

    def test_applying_it_actually_filters(self):
        hot = self._contact("Hot", lead_score=90)
        self._contact("Cold", lead_score=1)
        queryset, applied = search.apply(Contact.objects.filter(tenant=self.tenant), "hot leads")
        self.assertEqual(list(queryset), [hot])
        self.assertEqual(applied, ["Hot leads"])

    def test_a_name_search_matches_the_company_column(self):
        match = self._contact("Ravi", company="Sharma Textiles")
        self._contact("Other", company="Nothing")
        queryset, _ = search.apply(Contact.objects.filter(tenant=self.tenant), "sharma")
        self.assertEqual(list(queryset), [match])


# ---------------------------------------------------------------------------
# Deals and the pipeline
# ---------------------------------------------------------------------------

class DealTests(CrmBase):
    def setUp(self):
        super().setUp()
        self.pipeline = services.default_pipeline(self.tenant)
        self.contact = self._contact()

    def test_the_default_pipeline_is_built_once_with_the_standard_stages(self):
        again = services.default_pipeline(self.tenant)
        self.assertEqual(again.pk, self.pipeline.pk)
        self.assertEqual(Pipeline.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(self.pipeline.stages.count(), len(services.DEFAULT_STAGES))

    def test_a_new_deal_starts_open_and_qualifies_the_contact(self):
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Bulk order", actor=self.user)
        self.assertEqual(deal.status, Deal.Status.OPEN)
        self.assertIsNone(deal.closed_at)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.lifecycle_status, Contact.Lifecycle.QUALIFIED)

    def test_moving_a_deal_to_a_won_stage_wins_it_everywhere(self):
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Bulk order", actor=self.user)
        won = self.pipeline.stages.get(kind=PipelineStage.Kind.WON)
        services.move_deal(deal, won, actor=self.user)
        deal.refresh_from_db()
        self.assertEqual(deal.status, Deal.Status.WON)
        self.assertIsNotNone(deal.closed_at)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.lifecycle_status, Contact.Lifecycle.CUSTOMER)

    def test_reopening_a_closed_deal_clears_its_close_date(self):
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Bulk order")
        won = self.pipeline.stages.get(kind=PipelineStage.Kind.WON)
        services.move_deal(deal, won)
        first_open = self.pipeline.stages.filter(kind=PipelineStage.Kind.OPEN).order_by("order").first()
        services.move_deal(deal, first_open)
        deal.refresh_from_db()
        self.assertEqual(deal.status, Deal.Status.OPEN)
        self.assertIsNone(deal.closed_at)

    def test_a_stage_from_another_pipeline_is_refused(self):
        other = Pipeline.objects.create(tenant=self.tenant, name="Wholesale")
        foreign = PipelineStage.objects.create(pipeline=other, name="Start")
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Bulk order")
        with self.assertRaises(ValueError):
            services.move_deal(deal, foreign)

    def test_moving_a_deal_writes_a_timeline_entry(self):
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Bulk order")
        target = self.pipeline.stages.filter(kind=PipelineStage.Kind.OPEN).order_by("order")[2]
        services.move_deal(deal, target, actor=self.user)
        self.assertTrue(
            Activity.objects.filter(contact=self.contact, kind=Activity.Kind.DEAL_STAGE_CHANGED).exists()
        )

    def test_the_board_groups_deals_and_totals_them(self):
        stage = self.pipeline.stages.order_by("order").first()
        services.create_deal(tenant=self.tenant, contact=self.contact, name="A", stage=stage, value=100)
        services.create_deal(tenant=self.tenant, contact=self.contact, name="B", stage=stage, value=250)
        columns = services.pipeline_board(self.pipeline)
        first = next(column for column in columns if column["stage"].pk == stage.pk)
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["value"], 350)


# ---------------------------------------------------------------------------
# Tasks and notes
# ---------------------------------------------------------------------------

class TaskAndNoteTests(CrmBase):
    def test_completing_a_task_stamps_it_and_records_it(self):
        contact = self._contact()
        task = services.create_task(
            tenant=self.tenant, title="Call Ravi", due_at=timezone.now() + timedelta(hours=1),
            contact=contact, actor=self.user,
        )
        services.complete_task(task, actor=self.user)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertIsNotNone(task.completed_at)
        self.assertTrue(Activity.objects.filter(contact=contact, kind=Activity.Kind.TASK_COMPLETED).exists())

    def test_completing_an_already_done_task_is_a_no_op(self):
        task = services.create_task(tenant=self.tenant, title="X", due_at=timezone.now())
        services.complete_task(task)
        first_completion = task.completed_at
        services.complete_task(task)
        task.refresh_from_db()
        self.assertEqual(task.completed_at, first_completion)

    def test_an_overdue_task_says_so(self):
        task = services.create_task(tenant=self.tenant, title="X", due_at=timezone.now() - timedelta(hours=2))
        self.assertTrue(task.is_overdue)

    def test_a_note_appears_on_the_timeline(self):
        contact = self._contact()
        services.add_note(contact=contact, body="Prefers a call after 6pm.", actor=self.user)
        self.assertEqual(ContactNote.objects.filter(contact=contact).count(), 1)
        self.assertTrue(Activity.objects.filter(contact=contact, kind=Activity.Kind.NOTE_ADDED).exists())


# ---------------------------------------------------------------------------
# Timeline and Today
# ---------------------------------------------------------------------------

class TimelineTests(CrmBase):
    def test_it_merges_messages_and_crm_events_in_time_order(self):
        contact = self._contact()
        now = timezone.now()
        self._message(contact, direction=Message.Direction.OUTBOUND, when=now - timedelta(hours=3), body="Hello")
        services.add_note(contact=contact, body="Called them", actor=self.user)
        self._message(contact, direction=Message.Direction.INBOUND, when=now - timedelta(hours=1), body="Hi back")

        entries = services.contact_timeline(contact)
        kinds = [entry["kind"] for entry in entries]
        self.assertIn("MESSAGE_SENT", kinds)
        self.assertIn("MESSAGE_RECEIVED", kinds)
        self.assertIn(Activity.Kind.NOTE_ADDED, kinds)
        stamps = [entry["at"] for entry in entries]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_the_limit_is_respected_across_both_sources(self):
        contact = self._contact()
        for index in range(6):
            self._message(contact, when=timezone.now() - timedelta(minutes=index))
            services.add_note(contact=contact, body=f"note {index}")
        self.assertEqual(len(services.contact_timeline(contact, limit=5)), 5)

    def test_a_timeline_only_shows_one_contact(self):
        one, two = self._contact("One"), self._contact("Two")
        self._message(one, body="for one")
        self._message(two, body="for two")
        summaries = [entry["summary"] for entry in services.contact_timeline(one)]
        self.assertIn("for one", summaries)
        self.assertNotIn("for two", summaries)


class TodayDigestTests(CrmBase):
    def test_it_counts_due_tasks_and_flags_the_overdue_ones(self):
        contact = self._contact()
        services.create_task(
            tenant=self.tenant, title="Overdue", due_at=timezone.now() - timedelta(days=1),
            contact=contact, assigned_to=self.user,
        )
        digest = services.today_digest(self.tenant, user=self.user)
        tasks_item = next(item for item in digest["items"] if item["key"] == "tasks_due")
        self.assertEqual(tasks_item["count"], 1)
        self.assertEqual(tasks_item["urgent"], 1)

    def test_it_counts_chats_that_have_been_waiting(self):
        contact = self._contact()
        Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=contact,
            awaiting_reply_since=timezone.now() - timedelta(hours=8),
        )
        digest = services.today_digest(self.tenant, user=self.user)
        item = next(item for item in digest["items"] if item["key"] == "needs_reply")
        self.assertEqual(item["count"], 1)

    def test_every_bucket_carries_a_link(self):
        digest = services.today_digest(self.tenant, user=self.user)
        self.assertTrue(all(item["link"] for item in digest["items"]))

    def test_another_tenants_work_never_appears(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-today", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-today", password="x", tenant=other)
        Task.objects.create(
            tenant=other, title="Not mine", due_at=timezone.now() - timedelta(hours=1), assigned_to=other_user,
        )
        digest = services.today_digest(self.tenant, user=self.user)
        tasks_item = next(item for item in digest["items"] if item["key"] == "tasks_due")
        self.assertEqual(tasks_item["count"], 0)


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

class MergeTests(CrmBase):
    def test_everything_moves_to_the_survivor(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("")
        duplicate.email = "ravi@example.com"
        duplicate.company = "Sharma Textiles"
        duplicate.save()

        message = self._message(duplicate, body="from the duplicate")
        deal = services.create_deal(tenant=self.tenant, contact=duplicate, name="Order")
        task = services.create_task(tenant=self.tenant, title="Call", due_at=timezone.now(), contact=duplicate)
        note = services.add_note(contact=duplicate, body="Note on the duplicate")

        services.merge_contacts(primary, duplicate, actor=self.user)

        message.refresh_from_db(); deal.refresh_from_db(); task.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(message.contact_id, primary.pk)
        self.assertEqual(deal.contact_id, primary.pk)
        self.assertEqual(task.contact_id, primary.pk)
        self.assertEqual(note.contact_id, primary.pk)
        self.assertFalse(Contact.objects.filter(pk=duplicate.pk).exists())

    def test_empty_fields_are_filled_but_populated_ones_are_kept(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("Ravi Kumar", email="ravi@example.com", company="Sharma Textiles")
        services.merge_contacts(primary, duplicate)
        primary.refresh_from_db()
        self.assertEqual(primary.name, "Ravi")  # the survivor's own value wins
        self.assertEqual(primary.email, "ravi@example.com")  # the gap is filled
        self.assertEqual(primary.company, "Sharma Textiles")

    def test_an_opt_out_on_either_record_binds_the_survivor(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("Ravi K", subscribed=False)
        services.merge_contacts(primary, duplicate)
        primary.refresh_from_db()
        self.assertFalse(primary.subscribed)

    def test_tags_are_combined(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("Ravi K")
        tag = ContactTag.objects.create(tenant=self.tenant, name="VIP")
        duplicate.tags.add(tag)
        services.merge_contacts(primary, duplicate)
        self.assertIn(tag, primary.tags.all())

    def test_contacts_from_different_tenants_cannot_be_merged(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-merge", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-merge", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}",
        )
        with self.assertRaises(ValueError):
            services.merge_contacts(self._contact(), stranger)

    def test_refreshing_stamps_recomputes_them_from_the_message_table(self):
        """
        The same recomputation migration 0006 runs over the whole table: it has
        to produce the right answer from messages alone, since every contact
        that predates the CRM has these columns empty.
        """
        contact = self._contact()
        now = timezone.now()
        self._message(contact, direction=Message.Direction.OUTBOUND, when=now - timedelta(days=3))
        self._message(contact, direction=Message.Direction.INBOUND, when=now - timedelta(days=1))
        Contact.objects.filter(pk=contact.pk).update(
            last_inbound_at=None, last_outbound_at=None, last_message_at=None,
        )

        services.refresh_message_stamps(contact)

        contact.refresh_from_db()
        self.assertIsNotNone(contact.last_inbound_at)
        self.assertIsNotNone(contact.last_outbound_at)
        self.assertGreater(contact.last_inbound_at, contact.last_outbound_at)
        self.assertEqual(contact.last_message_at, contact.last_inbound_at)

    def test_a_duplicate_conversation_on_the_same_account_is_dropped_not_orphaned(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("Ravi K")
        Conversation.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=primary)
        Conversation.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=duplicate)
        services.merge_contacts(primary, duplicate)
        self.assertEqual(Conversation.objects.filter(contact=primary).count(), 1)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class CrmApiTests(CrmBase):
    def setUp(self):
        super().setUp()
        self.pipeline = services.default_pipeline(self.tenant)
        self.contact = self._contact()

    def test_listing_pipelines_creates_the_first_one(self):
        Pipeline.objects.all().delete()
        response = self.client.get("/api/v1/crm/pipelines/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertTrue(response.data["results"][0]["stages"])

    def test_creating_a_deal_through_the_api_records_the_timeline_entry(self):
        response = self.client.post("/api/v1/crm/deals/", {
            "contact": self.contact.pk, "name": "Bulk order", "value": "5000",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(Activity.objects.filter(contact=self.contact, kind=Activity.Kind.DEAL_CREATED).exists())

    def test_a_deal_cannot_be_opened_on_another_tenants_contact(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-api", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-api", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}",
        )
        response = self.client.post("/api/v1/crm/deals/", {"contact": stranger.pk, "name": "Nope"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_deal_list_never_shows_another_tenants_deals(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-list", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-list", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}",
        )
        services.create_deal(tenant=other, contact=stranger, name="Theirs")
        services.create_deal(tenant=self.tenant, contact=self.contact, name="Mine")
        response = self.client.get("/api/v1/crm/deals/")
        self.assertEqual([deal["name"] for deal in response.data["results"]], ["Mine"])

    def test_moving_a_deal_to_another_tenants_stage_is_refused(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-stage", kind=Tenant.Kind.CUSTOMER)
        foreign_pipeline = Pipeline.objects.create(tenant=other, name="Theirs")
        foreign_stage = PipelineStage.objects.create(pipeline=foreign_pipeline, name="Start")
        deal = services.create_deal(tenant=self.tenant, contact=self.contact, name="Mine")
        response = self.client.post(f"/api/v1/crm/deals/{deal.pk}/move/", {"stage": foreign_stage.pk}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_the_today_endpoint_answers(self):
        response = self.client.get("/api/v1/crm/today/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["items"])

    def test_the_segments_endpoint_lists_them_with_counts(self):
        response = self.client.get("/api/v1/crm/segments/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["segments"]), len(segments.SMART_SEGMENTS))

    def test_the_timeline_endpoint_is_tenant_scoped(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-tl", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-tl", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}",
        )
        response = self.client.get(f"/api/v1/crm/contacts/{stranger.pk}/timeline/")
        self.assertEqual(response.status_code, 404)

    def test_completing_a_task_through_the_api(self):
        task = services.create_task(
            tenant=self.tenant, title="Call", due_at=timezone.now(), contact=self.contact, assigned_to=self.user,
        )
        response = self.client.post(f"/api/v1/crm/tasks/{task.pk}/complete/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], Task.Status.DONE)

    def test_a_task_cannot_be_assigned_outside_the_tenant(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-assign", kind=Tenant.Kind.CUSTOMER)
        stranger = get_user_model().objects.create_user(username="other-assign", password="x", tenant=other)
        response = self.client.post("/api/v1/crm/tasks/", {
            "title": "Call", "due_at": timezone.now().isoformat(), "assigned_to": stranger.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_bulk_completing_tasks(self):
        tasks = [
            services.create_task(tenant=self.tenant, title=f"T{index}", due_at=timezone.now(), contact=self.contact)
            for index in range(3)
        ]
        response = self.client.post("/api/v1/crm/tasks/bulk/", {
            "action": "complete", "ids": [task.pk for task in tasks],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["succeeded"], 3)
        self.assertEqual(Task.objects.filter(tenant=self.tenant, status=Task.Status.DONE).count(), 3)

    def test_a_stage_holding_deals_cannot_be_deleted(self):
        stage = self.pipeline.stages.order_by("order").first()
        services.create_deal(tenant=self.tenant, contact=self.contact, name="Order", stage=stage)
        response = self.client.delete(f"/api/v1/crm/stages/{stage.pk}/")
        self.assertEqual(response.status_code, 400)

    def test_everything_needs_authentication(self):
        anonymous = APIClient()
        for url in ("/api/v1/crm/deals/", "/api/v1/crm/tasks/", "/api/v1/crm/today/", "/api/v1/crm/segments/"):
            self.assertEqual(anonymous.get(url).status_code, 401, url)


class ContactCrmApiTests(CrmBase):
    def test_the_profile_endpoint_returns_the_whole_customer(self):
        contact = self._contact()
        services.create_deal(tenant=self.tenant, contact=contact, name="Order", value=1200)
        services.create_task(tenant=self.tenant, title="Call", due_at=timezone.now(), contact=contact)
        services.add_note(contact=contact, body="Prefers evenings")

        response = self.client.get(f"/api/v1/contacts/contacts/{contact.pk}/profile/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["deals"]), 1)
        self.assertEqual(len(response.data["tasks"]), 1)
        self.assertEqual(len(response.data["notes"]), 1)
        self.assertEqual(response.data["open_deal_value"], 1200)

    def test_the_contact_list_filters_by_smart_segment(self):
        hot = self._contact("Hot", lead_score=90)
        self._contact("Cold", lead_score=1)
        response = self.client.get("/api/v1/contacts/contacts/?smart=hot_leads")
        self.assertEqual([row["id"] for row in response.data["results"]], [hot.pk])

    def test_the_contact_list_answers_a_natural_language_query(self):
        hot = self._contact("Hot", lead_score=90)
        self._contact("Cold", lead_score=1)
        response = self.client.get("/api/v1/contacts/contacts/?q=hot leads")
        self.assertEqual([row["id"] for row in response.data["results"]], [hot.pk])
        self.assertEqual(response.data["understood"], ["Hot leads"])

    def test_the_band_filter_works(self):
        hot = self._contact("Hot", lead_score=90)
        self._contact("Cold", lead_score=1)
        response = self.client.get("/api/v1/contacts/contacts/?band=HOT")
        self.assertEqual([row["id"] for row in response.data["results"]], [hot.pk])

    def test_duplicates_finds_the_same_number_written_differently(self):
        primary = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Ravi", phone="+919876543210",
        )
        near = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Ravi K", phone="+9876543210",
        )
        response = self.client.get(f"/api/v1/contacts/contacts/{primary.pk}/duplicates/")
        self.assertEqual([row["id"] for row in response.data["results"]], [near.pk])

    def test_merging_through_the_api(self):
        primary = self._contact("Ravi")
        duplicate = self._contact("Ravi K", email="ravi@example.com")
        response = self.client.post(
            f"/api/v1/contacts/contacts/{primary.pk}/merge/", {"duplicate_id": duplicate.pk}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], "ravi@example.com")
        self.assertFalse(Contact.objects.filter(pk=duplicate.pk).exists())

    def test_a_contact_cannot_be_merged_into_itself(self):
        contact = self._contact()
        response = self.client.post(
            f"/api/v1/contacts/contacts/{contact.pk}/merge/", {"duplicate_id": contact.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_another_tenants_contact_cannot_be_merged_in(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-merge-api", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-merge-api", password="x", tenant=other)
        stranger = Contact.objects.create(
            tenant=other, created_by=other_user, name="Stranger", phone=f"+{next(_phones)}",
        )
        response = self.client.post(
            f"/api/v1/contacts/contacts/{self._contact().pk}/merge/", {"duplicate_id": stranger.pk}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_bulk_assigning_an_owner(self):
        contacts = [self._contact(f"C{index}") for index in range(3)]
        response = self.client.post("/api/v1/contacts/contacts/bulk/", {
            "action": "assign_owner", "ids": [contact.pk for contact in contacts], "owner_id": self.user.pk,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant, owner=self.user).count(), 3)

    def test_bulk_assigning_to_somebody_outside_the_tenant_is_refused(self):
        other = Tenant.objects.create(name="Other", slug="other-crm-bulk", kind=Tenant.Kind.CUSTOMER)
        stranger = get_user_model().objects.create_user(username="other-bulk", password="x", tenant=other)
        response = self.client.post("/api/v1/contacts/contacts/bulk/", {
            "action": "assign_owner", "ids": [self._contact().pk], "owner_id": stranger.pk,
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_bulk_setting_a_status(self):
        contacts = [self._contact(f"C{index}") for index in range(2)]
        response = self.client.post("/api/v1/contacts/contacts/bulk/", {
            "action": "set_status", "ids": [contact.pk for contact in contacts],
            "lifecycle_status": Contact.Lifecycle.QUALIFIED,
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Contact.objects.filter(tenant=self.tenant, lifecycle_status=Contact.Lifecycle.QUALIFIED).count(), 2,
        )

    def test_an_unknown_status_is_refused(self):
        response = self.client.post("/api/v1/contacts/contacts/bulk/", {
            "action": "set_status", "ids": [self._contact().pk], "lifecycle_status": "BANANA",
        }, format="json")
        self.assertEqual(response.status_code, 400)


class CrmSignalTests(CrmBase):
    def test_creating_a_contact_starts_its_timeline(self):
        contact = self._contact()
        self.assertTrue(Activity.objects.filter(contact=contact, kind=Activity.Kind.CONTACT_CREATED).exists())

    def test_tagging_a_contact_is_recorded(self):
        contact = self._contact()
        tag = ContactTag.objects.create(tenant=self.tenant, name="VIP")
        contact.tags.add(tag)
        entry = Activity.objects.filter(contact=contact, kind=Activity.Kind.TAG_ADDED).first()
        self.assertIsNotNone(entry)
        self.assertIn("VIP", entry.summary)

    def test_bulk_tagging_from_the_tag_side_does_not_flood_the_timeline(self):
        """One manager tagging 3 contacts is one action, not 3 timeline rows each."""
        contacts = [self._contact(f"C{index}") for index in range(3)]
        tag = ContactTag.objects.create(tenant=self.tenant, name="Wholesale")
        tag.contacts.add(*contacts)
        self.assertEqual(Activity.objects.filter(kind=Activity.Kind.TAG_ADDED).count(), 0)

    def test_an_opt_out_lands_on_the_timeline_and_zeroes_the_score(self):
        from apps.compliance.models import ConsentEvent
        from apps.compliance.services import record_consent

        contact = self._contact(lead_score=80)
        record_consent(contact, ConsentEvent.Event.OPT_OUT, source=ConsentEvent.Source.AGENT, actor=self.user)
        contact.refresh_from_db()
        self.assertTrue(Activity.objects.filter(contact=contact, kind=Activity.Kind.CONSENT_CHANGED).exists())
        self.assertEqual(contact.lead_score, 0)
