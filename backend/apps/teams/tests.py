from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.compliance.models import CompliancePolicy
from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.inbox.signals import inbound_message_recorded
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services
from .models import AssignmentPolicy, CannedReply, TeamMember, WorkingHours


def _account(tenant, user):
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


class Fixture(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug=f"acme-{self._testMethodName}"[:50], kind=Tenant.Kind.CUSTOMER)
        self.owner = get_user_model().objects.create_user(
            username=f"owner-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        self.account = _account(self.tenant, self.owner)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.owner, name="Priya", phone="+15559990001",
        )

    def _agent(self, name, **kwargs):
        user = get_user_model().objects.create_user(
            username=f"{name}-{self._testMethodName}"[:140], password="x", tenant=self.tenant, credits=100,
        )
        return TeamMember.objects.create(tenant=self.tenant, user=user, **kwargs)

    def _conversation(self, **kwargs):
        return Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=kwargs.pop("contact", self.contact), **kwargs,
        )


class AssignmentTests(Fixture):
    def test_off_by_default_leaves_conversations_unassigned(self):
        self._agent("a")
        conversation = self._conversation()

        self.assertFalse(services.auto_assign(conversation))
        self.assertIsNone(conversation.assigned_agent_id)

    def test_round_robin_walks_through_the_agents(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        first, second = self._agent("a"), self._agent("b")

        picked = []
        for index in range(4):
            contact = Contact.objects.create(
                tenant=self.tenant, created_by=self.owner, name=f"C{index}", phone=f"+1555999100{index}",
            )
            conversation = self._conversation(contact=contact)
            services.auto_assign(conversation)
            conversation.refresh_from_db()
            picked.append(conversation.assigned_agent_id)

        self.assertEqual(picked, [first.user_id, second.user_id, first.user_id, second.user_id])

    def test_least_busy_prefers_the_agent_with_fewer_open_chats(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.LEAST_BUSY)
        busy, idle = self._agent("busy"), self._agent("idle")
        loaded = Contact.objects.create(tenant=self.tenant, created_by=self.owner, name="X", phone="+15559991100")
        self._conversation(contact=loaded, assigned_agent=busy.user)

        conversation = self._conversation()
        services.auto_assign(conversation)
        conversation.refresh_from_db()

        self.assertEqual(conversation.assigned_agent_id, idle.user_id)

    def test_unavailable_agents_are_skipped(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        self._agent("away", is_available=False)
        working = self._agent("working")

        conversation = self._conversation()
        services.auto_assign(conversation)
        conversation.refresh_from_db()

        self.assertEqual(conversation.assigned_agent_id, working.user_id)

    def test_an_agent_at_capacity_is_skipped(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        full = self._agent("full", max_open_conversations=1)
        spare = self._agent("spare")
        taken = Contact.objects.create(tenant=self.tenant, created_by=self.owner, name="X", phone="+15559991200")
        self._conversation(contact=taken, assigned_agent=full.user)

        conversation = self._conversation()
        services.auto_assign(conversation)
        conversation.refresh_from_db()

        self.assertEqual(conversation.assigned_agent_id, spare.user_id)

    def test_an_owned_conversation_is_never_taken_away(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        holder, other = self._agent("holder"), self._agent("other")
        conversation = self._conversation(assigned_agent=holder.user)

        self.assertFalse(services.auto_assign(conversation))
        conversation.refresh_from_db()
        self.assertEqual(conversation.assigned_agent_id, holder.user_id)
        self.assertNotEqual(conversation.assigned_agent_id, other.user_id)

    def test_nobody_available_leaves_it_in_the_queue(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        self._agent("away", is_available=False)
        conversation = self._conversation()

        self.assertFalse(services.auto_assign(conversation))
        self.assertIsNone(conversation.assigned_agent_id)


class ResponseClockTests(Fixture):
    def test_inbound_starts_the_clock_once(self):
        conversation = self._conversation()
        services.on_inbound(conversation)
        started = conversation.awaiting_reply_since

        services.on_inbound(conversation)

        self.assertEqual(conversation.awaiting_reply_since, started)

    def test_human_reply_stops_the_clock_and_records_the_time(self):
        conversation = self._conversation(awaiting_reply_since=timezone.now() - timedelta(minutes=5))

        services.on_human_reply(conversation)

        self.assertIsNone(conversation.awaiting_reply_since)
        self.assertGreaterEqual(conversation.first_response_seconds, 299)

    def test_first_response_time_is_never_revised(self):
        conversation = self._conversation(
            awaiting_reply_since=timezone.now() - timedelta(minutes=1), first_response_seconds=12,
        )

        services.on_human_reply(conversation)

        self.assertEqual(conversation.first_response_seconds, 12)

    def test_overdue_once_the_target_passes(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, first_response_target_minutes=15)
        conversation = self._conversation(awaiting_reply_since=timezone.now() - timedelta(minutes=20))

        due = services.response_due_at(conversation)

        self.assertIsNotNone(due)
        self.assertLess(due, timezone.now())

    def test_no_target_means_nothing_is_ever_late(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, first_response_target_minutes=0)
        conversation = self._conversation(awaiting_reply_since=timezone.now() - timedelta(days=3))

        self.assertIsNone(services.response_due_at(conversation))

    def test_an_answered_conversation_owes_nothing(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, first_response_target_minutes=15)
        conversation = self._conversation()

        self.assertIsNone(services.response_due_at(conversation))

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_an_agent_reply_stops_the_clock_through_the_inbox(self, mock_send):
        from apps.whatsapp.providers.base import SendResult

        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.reply")
        from apps.inbox.services import send_reply

        conversation = self._conversation(awaiting_reply_since=timezone.now() - timedelta(minutes=2))
        send_reply(conversation, self.owner, body="On it!")

        conversation.refresh_from_db()
        self.assertIsNone(conversation.awaiting_reply_since)
        self.assertIsNotNone(conversation.first_response_seconds)


class WorkingHoursTests(Fixture):
    def setUp(self):
        super().setUp()
        CompliancePolicy.objects.create(tenant=self.tenant, timezone="UTC")

    def test_no_configuration_means_always_open(self):
        self.assertTrue(services.is_open_now(self.tenant.pk))

    def test_closed_outside_the_window(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, working_hours_enabled=True)
        for weekday in range(7):
            WorkingHours.objects.create(
                tenant=self.tenant, weekday=weekday, is_open=True, opens_at=time(9, 0), closes_at=time(18, 0),
            )
        at_night = timezone.now().replace(hour=22, minute=0)

        self.assertFalse(services.is_open_now(self.tenant.pk, now=at_night))

    def test_open_inside_the_window(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, working_hours_enabled=True)
        for weekday in range(7):
            WorkingHours.objects.create(
                tenant=self.tenant, weekday=weekday, is_open=True, opens_at=time(0, 1), closes_at=time(23, 59),
            )
        midday = timezone.now().replace(hour=12, minute=0)

        self.assertTrue(services.is_open_now(self.tenant.pk, now=midday))

    def test_a_day_marked_closed_is_closed_all_day(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, working_hours_enabled=True)
        now = timezone.now().replace(hour=12, minute=0)
        WorkingHours.objects.create(
            tenant=self.tenant, weekday=now.weekday(), is_open=False, opens_at=time(9, 0), closes_at=time(18, 0),
        )

        self.assertFalse(services.is_open_now(self.tenant.pk, now=now))

    def test_a_night_shift_wraps_past_midnight(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, working_hours_enabled=True)
        now = timezone.now().replace(hour=2, minute=0)
        for weekday in range(7):
            WorkingHours.objects.create(
                tenant=self.tenant, weekday=weekday, is_open=True, opens_at=time(22, 0), closes_at=time(6, 0),
            )

        self.assertTrue(services.is_open_now(self.tenant.pk, now=now))


class AwayMessageTests(Fixture):
    def setUp(self):
        super().setUp()
        CompliancePolicy.objects.create(tenant=self.tenant, timezone="UTC")
        AssignmentPolicy.objects.create(
            tenant=self.tenant, working_hours_enabled=True, send_away_message=True, away_message="We're closed.",
        )
        WorkingHours.objects.create(
            tenant=self.tenant, weekday=timezone.now().weekday(), is_open=False,
        )

    def _inbound(self, body="Hello?"):
        conversation = self._conversation()
        message = Message.objects.create(
            tenant=self.tenant, conversation=conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, body=body,
            status=Message.Status.DELIVERED,
        )
        return conversation, message

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_sends_once_while_the_team_stays_silent(self, mock_send):
        from apps.whatsapp.providers.base import SendResult

        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.away")
        conversation, message = self._inbound()
        services.on_inbound(conversation)

        self.assertTrue(services.maybe_send_away_message(conversation, message))
        self.assertFalse(services.maybe_send_away_message(conversation, message))
        self.assertEqual(
            Message.objects.filter(conversation=conversation, body="We're closed.").count(), 1,
        )

    def test_never_answers_an_opt_out_with_an_away_message(self):
        """Somebody typing STOP is answered by compliance and by nothing else."""
        conversation, message = self._inbound(body="STOP")
        services.on_inbound(conversation)

        self.assertFalse(services.maybe_send_away_message(conversation, message))

    def test_silent_when_the_team_is_open(self):
        WorkingHours.objects.filter(tenant=self.tenant).update(
            is_open=True, opens_at=time(0, 1), closes_at=time(23, 59),
        )
        conversation, message = self._inbound()

        self.assertFalse(services.maybe_send_away_message(conversation, message))


class MentionTests(Fixture):
    def test_finds_usernames_in_this_tenant_only(self):
        member = self._agent("priya")
        other_tenant = Tenant.objects.create(name="Other", slug="other-mentions", kind=Tenant.Kind.CUSTOMER)
        get_user_model().objects.create_user(username="outsider", password="x", tenant=other_tenant)

        found = services.mentioned_users(self.tenant.pk, f"@{member.user.username} and @outsider please look")

        self.assertEqual([user.pk for user in found], [member.user_id])

    def test_plain_text_mentions_nobody(self):
        self.assertEqual(services.mentioned_users(self.tenant.pk, "no mentions here"), [])

    def test_a_note_mention_creates_a_notification(self):
        from apps.notifications.models import Notification

        member = self._agent("nina")
        conversation = self._conversation()
        client = APIClient()
        client.force_authenticate(user=self.owner)

        response = client.post(
            f"/api/v1/inbox/conversations/{conversation.pk}/notes/",
            {"body": f"@{member.user.username} can you take this?"}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        notification = Notification.objects.get(user=member.user)
        self.assertEqual(notification.kind, Notification.Kind.MENTION)
        self.assertEqual(notification.actor_id, self.owner.pk)

    def test_mentioning_yourself_notifies_nobody(self):
        from apps.notifications.models import Notification

        services.ensure_member(self.owner)
        conversation = self._conversation()
        client = APIClient()
        client.force_authenticate(user=self.owner)

        client.post(
            f"/api/v1/inbox/conversations/{conversation.pk}/notes/",
            {"body": f"@{self.owner.username} note to self"}, format="json",
        )

        self.assertFalse(Notification.objects.filter(user=self.owner).exists())


class TeamApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def test_listing_members_materialises_a_profile_for_every_user(self):
        get_user_model().objects.create_user(username="colleague-x", password="x", tenant=self.tenant)

        response = self.client.get("/api/v1/teams/members/")

        self.assertEqual(response.status_code, 200)
        usernames = {row["username"] for row in response.data}
        self.assertIn("colleague-x", usernames)
        self.assertIn(self.owner.username, usernames)

    def test_an_agent_can_go_off_shift(self):
        response = self.client.patch("/api/v1/teams/members/me/", {"is_available": False}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_available"])
        self.assertFalse(TeamMember.objects.get(user=self.owner).is_available)

    def test_away_message_needs_working_hours_to_ever_fire(self):
        response = self.client.patch(
            "/api/v1/teams/assignment-policy/",
            {"send_away_message": True, "away_message": "Closed", "working_hours_enabled": False}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("send_away_message", response.data)

    def test_working_hours_are_written_as_a_whole_week(self):
        days = [
            {"weekday": weekday, "is_open": weekday < 5, "opens_at": "09:00", "closes_at": "18:00"}
            for weekday in range(7)
        ]

        response = self.client.put("/api/v1/teams/working-hours/", {"days": days}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WorkingHours.objects.filter(tenant=self.tenant).count(), 7)
        self.assertEqual(len(response.data["days"]), 7)

    def test_a_repeated_weekday_is_rejected_rather_than_half_saved(self):
        days = [{"weekday": 0, "is_open": True}, {"weekday": 0, "is_open": False}]

        response = self.client.put("/api/v1/teams/working-hours/", {"days": days}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(WorkingHours.objects.filter(tenant=self.tenant).exists())

    def test_canned_reply_shortcut_is_normalised(self):
        response = self.client.post(
            "/api/v1/teams/canned-replies/", {"shortcut": "/Hours", "title": "Hours", "body": "9-6"}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["shortcut"], "hours")

    def test_a_shortcut_with_a_space_could_never_be_typed(self):
        response = self.client.post(
            "/api/v1/teams/canned-replies/", {"shortcut": "open hours", "title": "x", "body": "y"}, format="json",
        )

        self.assertEqual(response.status_code, 400)

    def test_using_a_canned_reply_counts_it(self):
        reply = CannedReply.objects.create(tenant=self.tenant, shortcut="hi", title="Hi", body="Hello!")

        response = self.client.post(f"/api/v1/teams/canned-replies/{reply.pk}/use/")

        self.assertEqual(response.data["usage_count"], 1)

    def test_performance_reports_response_times(self):
        agent = self._agent("perf")
        Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
            assigned_agent=agent.user, first_response_seconds=120,
        )

        response = self.client.get("/api/v1/teams/performance/")

        row = next(item for item in response.data["agents"] if item["user_id"] == agent.user_id)
        self.assertEqual(row["assigned"], 1)
        self.assertEqual(row["avg_first_response_seconds"], 120)

    def test_another_tenants_canned_replies_are_invisible(self):
        other = Tenant.objects.create(name="Other", slug="other-canned", kind=Tenant.Kind.CUSTOMER)
        CannedReply.objects.create(tenant=other, shortcut="secret", title="x", body="y")
        CannedReply.objects.create(tenant=self.tenant, shortcut="mine", title="x", body="y")

        response = self.client.get("/api/v1/teams/canned-replies/")

        shortcuts = [row["shortcut"] for row in response.data["results"]]
        self.assertEqual(shortcuts, ["mine"])


class InboundRoutingTests(Fixture):
    def test_an_inbound_message_starts_the_clock_and_assigns(self):
        AssignmentPolicy.objects.create(tenant=self.tenant, mode=AssignmentPolicy.Mode.ROUND_ROBIN)
        agent = self._agent("router")
        conversation = self._conversation()
        message = Message.objects.create(
            tenant=self.tenant, conversation=conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, body="Hi",
            status=Message.Status.DELIVERED,
        )

        inbound_message_recorded.send(sender=Message, message=message, is_new_conversation=True)

        conversation.refresh_from_db()
        self.assertEqual(conversation.assigned_agent_id, agent.user_id)
        self.assertIsNotNone(conversation.awaiting_reply_since)
