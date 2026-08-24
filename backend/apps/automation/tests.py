import itertools
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.contacts.models import Contact, ContactTag
from apps.inbox import services as inbox_services
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.templates_app.models import MessageTemplate
from apps.whatsapp.models import WhatsAppAccount, WhatsAppWebhookEvent
from apps.whatsapp.providers.base import SendResult

from . import presets, services
from .models import AutomationRule, FollowUpRule, Workflow, WorkflowRun
from .services import TriggerEvent, WorkflowError


def _connected_account(tenant, user):
    account = WhatsAppAccount(tenant=tenant, user=user, name="Line", phone_number_id="1", status=WhatsAppAccount.Status.CONNECTED)
    account.set_access_token("token")
    account.set_app_secret("secret")
    account.save()
    return account


def _inbound_event(account, *, from_number="15551234567", body="Hi", message_id="wamid.in.1"):
    return WhatsAppWebhookEvent.objects.create(
        account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE, provider_event_id=message_id,
        payload={"message": {"id": message_id, "from": from_number, "type": "text", "text": {"body": body}}, "profile_name": ""},
        related_message_id=message_id,
    )


def _unique_sends(prefix):
    """side_effect for the provider mock: a distinct provider_message_id per call.

    Message.provider_message_id is globally unique, so a mock returning one
    fixed value blows up with an IntegrityError the moment a test sends twice.
    """
    counter = itertools.count(1)
    return lambda *args, **kwargs: SendResult(
        success=True, provider_message_id=f"{prefix}.{next(counter)}"
    )


class AutomationBaseCase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="Acme", slug="acme-automation", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(username="acme-automation-owner", password="x", tenant=self.tenant, credits=100)
        self.account = _connected_account(self.tenant, self.user)


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class WelcomeAndKeywordTriggerTests(AutomationBaseCase):
    def test_welcome_message_sent_on_first_contact(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.1")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome", trigger_type=AutomationRule.Trigger.WELCOME,
            message_body="Welcome, [Name]!",
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account))
        from apps.whatsapp.signals import webhook_event_stored
        # record_inbound_message alone doesn't fire triggers; simulate what the webhook receiver does.
        services.trigger_welcome_message(message)

        outbound = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertTrue(outbound.is_automated)
        self.assertEqual(outbound.status, Message.Status.SENT)

    def test_keyword_rule_matches_contains(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Pricing", trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword="price", keyword_match_type=AutomationRule.MatchType.CONTAINS, message_body="Our pricing is on the website.",
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account, body="What is the price?"))
        services.trigger_keyword_rules(message)

        self.assertTrue(Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).exists())

    def test_keyword_rule_does_not_match_unrelated_text(self, mock_send):
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Pricing", trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword="price", keyword_match_type=AutomationRule.MatchType.EXACT, message_body="Our pricing is on the website.",
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account, body="hello there"))
        services.trigger_keyword_rules(message)

        mock_send.assert_not_called()
        self.assertFalse(Message.objects.filter(direction=Message.Direction.OUTBOUND).exists())

    def test_highest_priority_keyword_rule_wins(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.3")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Low", trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword="hi", keyword_match_type=AutomationRule.MatchType.CONTAINS, message_body="low priority reply", priority=0,
        )
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="High", trigger_type=AutomationRule.Trigger.KEYWORD,
            keyword="hi", keyword_match_type=AutomationRule.MatchType.CONTAINS, message_body="high priority reply", priority=10,
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account, body="hi there"))
        services.trigger_keyword_rules(message)

        outbound = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "high priority reply")

    def test_match_types(self, mock_send):
        rule = AutomationRule(keyword="hello", keyword_match_type=AutomationRule.MatchType.EXACT, trigger_type=AutomationRule.Trigger.KEYWORD)
        self.assertTrue(rule.matches("Hello"))
        self.assertFalse(rule.matches("Hello there"))

        rule.keyword_match_type = AutomationRule.MatchType.STARTS_WITH
        self.assertTrue(rule.matches("Hello there"))
        self.assertFalse(rule.matches("Say hello"))

        rule.keyword_match_type = AutomationRule.MatchType.ENDS_WITH
        self.assertTrue(rule.matches("Say hello"))
        self.assertFalse(rule.matches("Hello there"))

    def test_automated_message_skipped_without_credits(self, mock_send):
        self.user.credits = 0
        self.user.save(update_fields=["credits"])
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome", trigger_type=AutomationRule.Trigger.WELCOME,
            message_body="Welcome!",
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account))
        services.trigger_welcome_message(message)

        mock_send.assert_not_called()
        self.assertFalse(Message.objects.filter(direction=Message.Direction.OUTBOUND).exists())


class InboundSignalWiringTests(AutomationBaseCase):
    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_webhook_signal_triggers_welcome_end_to_end(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.4")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome", trigger_type=AutomationRule.Trigger.WELCOME,
            message_body="Welcome!",
        )

        from apps.whatsapp.signals import webhook_event_stored

        event = _inbound_event(self.account)
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)

        self.assertTrue(Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).exists())

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_second_message_in_conversation_does_not_retrigger_welcome(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.5")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome", trigger_type=AutomationRule.Trigger.WELCOME,
            message_body="Welcome!",
        )
        from apps.whatsapp.signals import webhook_event_stored

        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_inbound_event(self.account, message_id="wamid.in.1"))
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_inbound_event(self.account, message_id="wamid.in.2"))

        self.assertEqual(Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).count(), 1)


class FollowUpTests(AutomationBaseCase):
    def setUp(self):
        super().setUp()
        self.contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230000")
        self.conversation = Conversation.objects.create(tenant=self.tenant, whatsapp_account=self.account, contact=self.contact)
        self.original_message = Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.OUTBOUND, status=Message.Status.SENT,
            sent_at=timezone.now(), billed_to=self.user,
        )
        self.rule = FollowUpRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Nudge", delay_minutes=60, message_body="Still there?",
        )

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_sends_follow_up_when_no_reply(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.6")

        services.run_follow_up_check(self.original_message.pk, self.rule.pk)

        follow_up = Message.objects.get(is_automated=True)
        self.assertEqual(follow_up.body, "Still there?")

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_skips_follow_up_when_contact_already_replied(self, mock_send):
        Message.objects.create(
            tenant=self.tenant, conversation=self.conversation, whatsapp_account=self.account, contact=self.contact,
            to_phone=self.contact.phone, direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED,
        )

        services.run_follow_up_check(self.original_message.pk, self.rule.pk)

        mock_send.assert_not_called()

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_send_message_task_schedules_follow_up_for_real_outbound_send(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.7")
        message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            billed_to=self.user, status=Message.Status.QUEUED, body="Hello",
        )

        from apps.messaging.tasks import send_message_task

        with patch("apps.automation.services.schedule_follow_up_checks") as mock_schedule:
            send_message_task.delay(message.pk)

        mock_schedule.assert_called_once()

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_automated_sends_never_schedule_further_follow_ups(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.8")
        automated_message = Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            billed_to=self.user, status=Message.Status.QUEUED, body="Automated", is_automated=True,
        )

        from apps.messaging.tasks import send_message_task

        with patch("apps.automation.services.schedule_follow_up_checks") as mock_schedule:
            send_message_task.delay(automated_message.pk)

        mock_schedule.assert_not_called()


class WorkflowValidationTests(TestCase):
    def test_requires_exactly_one_trigger(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes([{"id": "n1", "type": "MESSAGE", "body": "hi"}])

    def test_rejects_dangling_reference(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes([
                {"id": "trigger", "type": "TRIGGER", "next": "missing"},
            ])

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes([
                {"id": "n1", "type": "TRIGGER", "next": "n1"},
                {"id": "n1", "type": "END"},
            ])

    def test_accepts_a_well_formed_graph(self):
        services.validate_nodes([
            {"id": "trigger", "type": "TRIGGER", "next": "msg"},
            {"id": "msg", "type": "MESSAGE", "body": "Hi!", "next": "end"},
            {"id": "end", "type": "END"},
        ])


class WorkflowExecutionTests(AutomationBaseCase):
    def setUp(self):
        super().setUp()
        self.contact = Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230001")

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_runs_message_tag_and_end_nodes(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.9")
        tag = ContactTag.objects.create(tenant=self.tenant, name="Lead")
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="New Lead",
            nodes=[
                {"id": "trigger", "type": "TRIGGER", "next": "msg"},
                {"id": "msg", "type": "MESSAGE", "body": "Welcome [Name]!", "next": "tag"},
                {"id": "tag", "type": "TAG", "tag_id": tag.pk, "next": "end"},
                {"id": "end", "type": "END"},
            ],
        )

        run = services.start_workflow(workflow, self.contact)

        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertTrue(self.contact.tags.filter(pk=tag.pk).exists())
        self.assertEqual(Message.objects.get(is_automated=True).body, "Welcome Priya!")

    def test_wait_node_pauses_and_schedules_resume(self):
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Wait Flow",
            nodes=[
                {"id": "trigger", "type": "TRIGGER", "next": "wait"},
                {"id": "wait", "type": "WAIT", "minutes": 60, "next": "end"},
                {"id": "end", "type": "END"},
            ],
        )

        with patch("apps.automation.tasks.resume_workflow_run.apply_async") as mock_apply_async:
            run = services.start_workflow(workflow, self.contact)

        self.assertEqual(run.status, WorkflowRun.Status.WAITING)
        mock_apply_async.assert_called_once_with(args=[run.pk], countdown=3600)

    def test_condition_node_branches_on_reply(self):
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Branching",
            nodes=[
                {"id": "trigger", "type": "TRIGGER", "next": "cond"},
                {"id": "cond", "type": "CONDITION", "condition": {"field": "has_replied"}, "on_true": "yes", "on_false": "no"},
                {"id": "yes", "type": "END"},
                {"id": "no", "type": "END"},
            ],
        )

        run = services.start_workflow(workflow, self.contact)
        self.assertEqual(run.current_node_id, "no")

        Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            direction=Message.Direction.INBOUND, status=Message.Status.DELIVERED,
        )
        run2 = services.start_workflow(workflow, self.contact)
        self.assertEqual(run2.current_node_id, "yes")

    def test_resume_continues_from_current_node(self):
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Resumable",
            nodes=[
                {"id": "trigger", "type": "TRIGGER", "next": "wait"},
                {"id": "wait", "type": "WAIT", "minutes": 1, "next": "end"},
                {"id": "end", "type": "END"},
            ],
        )
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(workflow, self.contact)
        self.assertEqual(run.status, WorkflowRun.Status.WAITING)

        services.resume_workflow(run)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)


class AutomationApiTests(TestCase):
    def setUp(self):
        self.tenant_a = Tenant.objects.create(name="Acme", slug="acme-automation-api", kind=Tenant.Kind.CUSTOMER)
        self.tenant_b = Tenant.objects.create(name="Globex", slug="globex-automation-api", kind=Tenant.Kind.CUSTOMER)
        self.user_a = get_user_model().objects.create_user(username="acme-automation-api-owner", password="x", tenant=self.tenant_a, credits=10)
        self.user_b = get_user_model().objects.create_user(username="globex-automation-api-owner", password="x", tenant=self.tenant_b)
        self.account_a = _connected_account(self.tenant_a, self.user_a)
        self.account_b = _connected_account(self.tenant_b, self.user_b)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_create_rule_for_another_tenants_account(self):
        response = self.client.post(
            "/api/v1/automation/rules/",
            {"name": "Bad", "whatsapp_account": self.account_b.pk, "trigger_type": "WELCOME", "message_body": "Hi"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_keyword_rule_requires_keyword(self):
        response = self.client.post(
            "/api/v1/automation/rules/",
            {"name": "Bad", "whatsapp_account": self.account_a.pk, "trigger_type": "KEYWORD", "message_body": "Hi"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("keyword", response.json())

    def test_workflow_rejects_invalid_nodes(self):
        response = self.client.post(
            "/api/v1/automation/workflows/",
            {"name": "Bad", "whatsapp_account": self.account_a.pk, "nodes": [{"id": "n1", "type": "MESSAGE", "body": "hi"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("nodes", response.json())

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_run_workflow_endpoint(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.10")
        contact = Contact.objects.create(tenant=self.tenant_a, created_by=self.user_a, name="Priya", phone="+15551230099")
        create = self.client.post(
            "/api/v1/automation/workflows/",
            {
                "name": "Simple", "whatsapp_account": self.account_a.pk,
                "nodes": [
                    {"id": "trigger", "type": "TRIGGER", "next": "msg"},
                    {"id": "msg", "type": "MESSAGE", "body": "Hi!", "next": "end"},
                    {"id": "end", "type": "END"},
                ],
            },
            format="json",
        )
        self.assertEqual(create.status_code, 201, create.content)

        response = self.client.post(
            f"/api/v1/automation/workflows/{create.json()['id']}/run/", {"contact_id": contact.pk}, format="json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "COMPLETED")


class AutomationBulkActionTests(AutomationBaseCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _rules(self):
        return [
            AutomationRule.objects.create(
                tenant=self.tenant, whatsapp_account=self.account, name=f"R{n}",
                trigger_type=AutomationRule.Trigger.KEYWORD, keyword=f"k{n}", message_body="Hi",
            )
            for n in range(3)
        ]

    def _bulk(self, action, ids):
        return self.client.post("/api/v1/automation/rules/bulk/", {"action": action, "ids": ids}, format="json")

    def test_bulk_deactivate_pauses_every_selected_rule(self):
        rules = self._rules()

        response = self._bulk("deactivate", [rule.pk for rule in rules])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["detail"], "3 rules deactivated.")
        self.assertEqual(AutomationRule.objects.filter(is_active=True).count(), 0)

    def test_bulk_delete_removes_the_selected_rules(self):
        rules = self._rules()

        response = self._bulk("delete", [rules[0].pk, rules[1].pk])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(AutomationRule.objects.values_list("pk", flat=True)), [rules[2].pk])

    def test_follow_ups_and_workflows_expose_the_same_endpoint(self):
        follow_up = FollowUpRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="F", delay_minutes=60, message_body="Hi",
        )
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="W",
            nodes=[{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}],
        )

        for path, pk in (("follow-ups", follow_up.pk), ("workflows", workflow.pk)):
            response = self.client.post(
                f"/api/v1/automation/{path}/bulk/", {"action": "deactivate", "ids": [pk]}, format="json"
            )
            self.assertEqual(response.status_code, 200, response.content)

        follow_up.refresh_from_db()
        workflow.refresh_from_db()
        self.assertFalse(follow_up.is_active)
        self.assertFalse(workflow.is_active)


def _button_event(account, *, button_id="rsvp_yes", title="Yes", from_number="15551234567", message_id="wamid.btn.1"):
    return WhatsAppWebhookEvent.objects.create(
        account=account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE, provider_event_id=message_id,
        payload={
            "message": {
                "id": message_id, "from": from_number, "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": title}},
            },
            "profile_name": "",
        },
        related_message_id=message_id,
    )


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class ButtonTriggerTests(AutomationBaseCase):
    """Tapping a reply button is its own trigger, matched on the button's id."""

    def test_a_button_rule_answers_the_tap(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.b1")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="RSVP yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="rsvp_yes",
            message_body="Great, see you Friday!",
        )

        message = inbox_services.record_inbound_message(_button_event(self.account))
        self.assertTrue(services.trigger_button_rules(message))

        outbound = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.body, "Great, see you Friday!")
        self.assertTrue(outbound.is_automated)

    def test_a_different_button_does_not_match(self, mock_send):
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="RSVP yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="rsvp_yes", message_body="See you!",
        )

        message = inbox_services.record_inbound_message(_button_event(self.account, button_id="rsvp_no", title="No"))

        self.assertFalse(services.trigger_button_rules(message))
        self.assertFalse(Message.objects.filter(direction=Message.Direction.OUTBOUND).exists())

    def test_a_tap_answered_by_a_button_rule_does_not_also_fire_a_keyword_rule(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.b2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="RSVP yes",
            trigger_type=AutomationRule.Trigger.BUTTON, button_id="rsvp_yes", message_body="Great, see you Friday!",
        )
        # The keyword rule matches the button's label, which is what the
        # contact "said" -- it must not send a second reply on top.
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Yes keyword",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="yes", message_body="You said yes.",
        )

        from apps.whatsapp.signals import webhook_event_stored

        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_button_event(self.account))

        outbound = Message.objects.filter(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.count(), 1)
        self.assertEqual(outbound.first().body, "Great, see you Friday!")

    def test_a_typed_reply_still_reaches_keyword_rules(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.b3")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Yes keyword",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="yes", message_body="You said yes.",
        )

        from apps.whatsapp.signals import webhook_event_stored

        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_inbound_event(self.account, body="yes"))

        self.assertEqual(Message.objects.get(direction=Message.Direction.OUTBOUND).body, "You said yes.")

    def test_an_automated_reply_carries_its_own_buttons(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.out.b4")
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Menu", body="What would you like?",
            buttons=[{"type": "REPLY", "id": "sales", "title": "Sales", "url": ""}],
        )
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome",
            trigger_type=AutomationRule.Trigger.WELCOME, template=template,
        )

        message = inbox_services.record_inbound_message(_inbound_event(self.account, message_id="wamid.in.menu"))
        services.trigger_welcome_message(message)

        outbound = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(outbound.buttons, [{"type": "REPLY", "id": "sales", "title": "Sales", "url": ""}])


class ButtonWorkflowTests(AutomationBaseCase):
    """A tapped button branches a workflow, and wakes one that is still waiting."""

    def setUp(self):
        super().setUp()
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551234567"
        )
        self.nodes = [
            {"id": "trigger", "type": "TRIGGER", "next": "ask"},
            {
                "id": "ask", "type": "MESSAGE", "body": "Coming on Friday?",
                "buttons": [{"type": "REPLY", "id": "rsvp_yes", "title": "Yes"},
                            {"type": "REPLY", "id": "rsvp_no", "title": "No"}],
                "next": "wait",
            },
            {"id": "wait", "type": "WAIT", "minutes": 60, "next": "check"},
            {
                "id": "check", "type": "CONDITION", "condition": {"field": "button_clicked", "value": "rsvp_yes"},
                "on_true": "confirm", "on_false": "end",
            },
            {"id": "confirm", "type": "MESSAGE", "body": "Great, see you then!", "next": "end"},
            {"id": "end", "type": "END"},
        ]

    def _workflow(self):
        return Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="RSVP", nodes=self.nodes
        )

    @patch("apps.messaging.services.ConnectionManager.supports_interactive_buttons", return_value=True)
    @patch("apps.messaging.services.ConnectionManager.send_interactive_message")
    def test_a_message_node_sends_its_buttons(self, mock_interactive, _supported):
        mock_interactive.return_value = SendResult(success=True, provider_message_id="wamid.wf.1")

        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            services.start_workflow(self._workflow(), self.contact)

        sent = Message.objects.get(is_automated=True)
        self.assertEqual([button["id"] for button in sent.buttons], ["rsvp_yes", "rsvp_no"])
        mock_interactive.assert_called_once()

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_the_tapped_button_decides_the_branch(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.wf.2")

        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(self._workflow(), self.contact)
        self.assertEqual(run.status, WorkflowRun.Status.WAITING)

        inbox_services.record_inbound_message(_button_event(self.account, button_id="rsvp_yes"))
        services.resume_workflow(run)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertTrue(Message.objects.filter(body="Great, see you then!").exists())

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_the_other_button_takes_the_false_branch(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.wf.3")

        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(self._workflow(), self.contact)

        inbox_services.record_inbound_message(_button_event(self.account, button_id="rsvp_no", title="No"))
        services.resume_workflow(run)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertFalse(Message.objects.filter(body="Great, see you then!").exists())

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_a_tap_wakes_a_run_that_is_still_waiting_out_its_delay(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.wf.4")

        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(self._workflow(), self.contact)

        from apps.whatsapp.signals import webhook_event_stored

        # No manual resume: the tap itself is what moves the run on, so the
        # contact hears back immediately rather than an hour later.
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_button_event(self.account, button_id="rsvp_yes"))

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertTrue(Message.objects.filter(body="Great, see you then!").exists())

    @patch("apps.messaging.services.ConnectionManager.send_text_message")
    def test_an_unrelated_wait_is_not_cut_short_by_a_tap(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="wamid.wf.5")
        workflow = Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Nudge",
            nodes=[
                {"id": "trigger", "type": "TRIGGER", "next": "wait"},
                {"id": "wait", "type": "WAIT", "minutes": 60, "next": "nudge"},
                {"id": "nudge", "type": "MESSAGE", "body": "Still there?", "next": "end"},
                {"id": "end", "type": "END"},
            ],
        )
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(workflow, self.contact)

        from apps.whatsapp.signals import webhook_event_stored

        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=_button_event(self.account, button_id="rsvp_yes"))

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.WAITING)
        self.assertFalse(Message.objects.filter(body="Still there?").exists())

    def test_a_run_woken_twice_only_advances_once(self):
        """The WAIT's scheduled resume and an early tap can both arrive."""
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(self._workflow(), self.contact)

        with patch("apps.automation.services._advance") as mock_advance:
            services.resume_workflow(run)
            services.resume_workflow(WorkflowRun.objects.get(pk=run.pk))

        self.assertEqual(mock_advance.call_count, 1)

    def test_malformed_buttons_on_a_message_node_are_rejected_on_save(self):
        nodes = [
            {"id": "trigger", "type": "TRIGGER", "next": "msg"},
            {"id": "msg", "type": "MESSAGE", "body": "Hi", "buttons": [{"title": ""}], "next": "end"},
            {"id": "end", "type": "END"},
        ]

        with self.assertRaises(WorkflowError):
            services.validate_nodes(nodes)


# ===========================================================================
# Hardening: everything below covers a failure the engine used to have.
# Each class names the specific way it went wrong.
# ===========================================================================


class _HardeningBase(TestCase):
    """Automation base + a cleared cooldown cache, which is cache-backed."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        self.tenant = Tenant.objects.create(name="Acme", slug=f"acme-{self._testMethodName[:30]}", kind=Tenant.Kind.CUSTOMER)
        self.user = get_user_model().objects.create_user(
            username=f"u-{self._testMethodName[:30]}", password="x", tenant=self.tenant, credits=10_000,
        )
        self.account = _connected_account(self.tenant, self.user)
        self.contact = Contact.objects.create(
            tenant=self.tenant, created_by=self.user, name="Priya", phone="+15551230001",
        )

    def _workflow(self, nodes, **kwargs):
        return Workflow.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name=kwargs.pop("name", "WF"), nodes=nodes, **kwargs
        )

    _delivery = 0

    def _deliver(self, body="hi", *, from_number="15551230001", button_id=None):
        """
        Put an inbound message through the real path.

        record_inbound_message() on its own only writes the Conversation and
        Message rows; the automation ladder hangs off inbound_message_recorded,
        which the inbox receiver sends when a webhook event is stored. Tests
        that skip that step are testing nothing about the wiring.
        """
        from apps.whatsapp.signals import webhook_event_stored

        type(self)._delivery += 1
        message_id = f"wamid.{self._testMethodName[:20]}.{type(self)._delivery}"
        payload = {"message": {"id": message_id, "from": from_number, "type": "text", "text": {"body": body}},
                   "profile_name": ""}
        if button_id is not None:
            payload["message"] = {
                "id": message_id, "from": from_number, "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": body}},
            }
        event = WhatsAppWebhookEvent.objects.create(
            account=self.account, event_type=WhatsAppWebhookEvent.EventType.INBOUND_MESSAGE,
            provider_event_id=message_id, payload=payload, related_message_id=message_id,
        )
        webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=event)
        return Message.objects.filter(direction=Message.Direction.INBOUND).order_by("-pk").first()


class LoopProtectionTests(_HardeningBase):
    """A graph that loops with no WAIT in the cycle used to spin forever."""

    def test_tight_cycle_is_rejected_on_save(self):
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "hi", "next": "c"},
            {"id": "c", "type": "CONDITION", "condition": {"field": "has_replied"}, "on_true": "e", "on_false": "m"},
            {"id": "e", "type": "END"},
        ]
        with self.assertRaises(WorkflowError) as ctx:
            services.validate_nodes(nodes)
        self.assertIn("loop forever", str(ctx.exception))

    def test_cycle_through_a_wait_is_allowed(self):
        """A drip sequence that nudges until someone answers is legitimate."""
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "still there?", "next": "w"},
            {"id": "w", "type": "WAIT", "minutes": 60, "next": "c"},
            {"id": "c", "type": "CONDITION", "condition": {"field": "has_replied"}, "on_true": "e", "on_false": "m"},
            {"id": "e", "type": "END"},
        ]
        services.validate_nodes(nodes)  # must not raise

    def test_self_loop_is_rejected(self):
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "hi", "next": "m"},
        ]
        with self.assertRaises(WorkflowError):
            services.validate_nodes(nodes)

    def test_diamond_is_not_mistaken_for_a_cycle(self):
        """Two paths converging is not a loop; the DFS must not say it is."""
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "c"},
            {"id": "c", "type": "CONDITION", "condition": {"field": "has_replied"}, "on_true": "a", "on_false": "b"},
            {"id": "a", "type": "MESSAGE", "body": "yes", "next": "join"},
            {"id": "b", "type": "MESSAGE", "body": "no", "next": "join"},
            {"id": "join", "type": "MESSAGE", "body": "thanks", "next": "e"},
            {"id": "e", "type": "END"},
        ]
        services.validate_nodes(nodes)  # must not raise

    def test_runtime_budget_stops_a_legacy_loop(self):
        """Rows saved before validation existed still have to be survivable."""
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "hi", "next": "c"},
            {"id": "c", "type": "CONDITION", "condition": {"field": "has_replied"}, "on_true": "e", "on_false": "m"},
            {"id": "e", "type": "END"},
        ]
        workflow = Workflow(tenant=self.tenant, whatsapp_account=self.account, name="Legacy", nodes=nodes)
        workflow.save()  # straight to the DB, bypassing the serializer

        sends = []
        with patch.object(services, "send_automated_message", side_effect=lambda *a, **kw: sends.append(1)):
            with patch.object(services, "validate_nodes"):  # simulate the row predating validation
                run = services.start_workflow(workflow, self.contact)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.FAILED)
        self.assertIn("loops", run.error)
        self.assertLessEqual(len(sends), services.MAX_STEPS_PER_ADVANCE)

    def test_lifetime_budget_stops_a_wait_cycle(self):
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "w"},
            {"id": "w", "type": "WAIT", "minutes": 1, "next": "w2"},
            {"id": "w2", "type": "WAIT", "minutes": 1, "next": "w"},
        ]
        workflow = self._workflow(nodes)
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            run = services.start_workflow(workflow, self.contact)
            for _ in range(services.MAX_TOTAL_STEPS + 5):
                run.refresh_from_db()
                if run.status != WorkflowRun.Status.WAITING:
                    break
                services.resume_workflow(run)

        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.FAILED)
        self.assertIn("total steps", run.error)


class CrashContainmentTests(_HardeningBase):
    """A run whose step raised used to be stranded in RUNNING forever."""

    def test_exception_marks_the_run_failed_with_a_reason(self):
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "hi", "next": "e"},
            {"id": "e", "type": "END"},
        ]
        workflow = self._workflow(nodes)
        with patch.object(services, "send_automated_message", side_effect=RuntimeError("provider down")):
            services.start_workflow(workflow, self.contact)

        run = WorkflowRun.objects.get(workflow=workflow)
        self.assertEqual(run.status, WorkflowRun.Status.FAILED)
        self.assertIn("provider down", run.error)
        self.assertIsNotNone(run.completed_at)

    def test_sweeper_reclaims_a_run_abandoned_mid_step(self):
        nodes = [{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}]
        workflow = self._workflow(nodes)
        run = WorkflowRun.objects.create(
            workflow=workflow, contact=self.contact, status=WorkflowRun.Status.RUNNING, current_node_id="e",
        )
        WorkflowRun.objects.filter(pk=run.pk).update(
            updated_at=timezone.now() - timedelta(minutes=services.STUCK_RUNNING_MINUTES + 1)
        )

        result = services.sweep_stuck_runs()

        run.refresh_from_db()
        self.assertEqual(result["reclaimed"], 1)
        self.assertEqual(run.status, WorkflowRun.Status.FAILED)
        self.assertIn("Abandoned", run.error)

    def test_sweeper_leaves_a_fresh_run_alone(self):
        nodes = [{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}]
        workflow = self._workflow(nodes)
        run = WorkflowRun.objects.create(
            workflow=workflow, contact=self.contact, status=WorkflowRun.Status.RUNNING, current_node_id="e",
        )
        self.assertEqual(services.sweep_stuck_runs()["reclaimed"], 0)
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.RUNNING)


class ConversationLinkingTests(_HardeningBase):
    """ASSIGN_AGENT and AI_REPLY were silent no-ops: no run ever had a conversation."""

    def test_run_adopts_the_contacts_existing_conversation(self):
        conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )
        nodes = [{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}]
        run = services.start_workflow(self._workflow(nodes), self.contact)
        self.assertEqual(run.conversation_id, conversation.pk)

    def test_assign_agent_actually_assigns(self):
        conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "a"},
            {"id": "a", "type": "ASSIGN_AGENT", "agent_id": self.user.pk, "next": "e"},
            {"id": "e", "type": "END"},
        ]
        services.start_workflow(self._workflow(nodes), self.contact)

        conversation.refresh_from_db()
        self.assertEqual(conversation.assigned_agent_id, self.user.pk)

    def test_assign_agent_refuses_a_user_from_another_tenant(self):
        other_tenant = Tenant.objects.create(name="Globex", slug="globex-assign", kind=Tenant.Kind.CUSTOMER)
        outsider = get_user_model().objects.create_user(username="outsider", password="x", tenant=other_tenant)
        conversation = Conversation.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact,
        )
        nodes = [
            {"id": "t", "type": "TRIGGER", "next": "a"},
            {"id": "a", "type": "ASSIGN_AGENT", "agent_id": outsider.pk, "next": "e"},
            {"id": "e", "type": "END"},
        ]
        services.start_workflow(self._workflow(nodes), self.contact)

        conversation.refresh_from_db()
        self.assertIsNone(conversation.assigned_agent_id)


class DuplicateRunTests(_HardeningBase):
    def _waiting_workflow(self):
        return self._workflow([
            {"id": "t", "type": "TRIGGER", "next": "w"},
            {"id": "w", "type": "WAIT", "minutes": 60, "next": "e"},
            {"id": "e", "type": "END"},
        ])

    def test_second_run_for_the_same_contact_is_refused(self):
        workflow = self._waiting_workflow()
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            services.start_workflow(workflow, self.contact)
            with self.assertRaises(WorkflowError) as ctx:
                services.start_workflow(workflow, self.contact)
        self.assertIn("already in run", str(ctx.exception))
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_force_overrides_the_guard(self):
        workflow = self._waiting_workflow()
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            services.start_workflow(workflow, self.contact)
            services.start_workflow(workflow, self.contact, force=True)
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 2)

    def test_a_finished_run_does_not_block_a_new_one(self):
        workflow = self._workflow([{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}])
        services.start_workflow(workflow, self.contact)
        services.start_workflow(workflow, self.contact)  # first one COMPLETED, so this is fine
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 2)


class ValidationHardeningTests(_HardeningBase):
    def _nodes(self, *extra):
        return [{"id": "t", "type": "TRIGGER", "next": "x"}, *extra]

    def test_wait_rejects_zero_and_negative(self):
        for minutes in (0, -5):
            with self.subTest(minutes=minutes):
                with self.assertRaises(WorkflowError):
                    services.validate_nodes(self._nodes({"id": "x", "type": "WAIT", "minutes": minutes}))

    def test_wait_rejects_absurdly_long(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes(
                self._nodes({"id": "x", "type": "WAIT", "minutes": services.MAX_WAIT_MINUTES + 1})
            )

    def test_wait_rejects_a_boolean(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes(self._nodes({"id": "x", "type": "WAIT", "minutes": True}))

    def test_message_needs_content(self):
        with self.assertRaises(WorkflowError) as ctx:
            services.validate_nodes(self._nodes({"id": "x", "type": "MESSAGE", "body": "   "}))
        self.assertIn("needs a template, a body, or media", str(ctx.exception))

    def test_condition_rejects_an_unknown_field(self):
        with self.assertRaises(WorkflowError) as ctx:
            services.validate_nodes(self._nodes(
                {"id": "x", "type": "CONDITION", "condition": {"field": "hs_replied"}, "on_true": "t"}
            ))
        self.assertIn("unknown condition field", str(ctx.exception))

    def test_condition_needs_a_branch(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes(self._nodes(
                {"id": "x", "type": "CONDITION", "condition": {"field": "has_replied"}}
            ))

    def test_tag_and_assign_need_their_target(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes(self._nodes({"id": "x", "type": "TAG"}))
        with self.assertRaises(WorkflowError):
            services.validate_nodes(self._nodes({"id": "x", "type": "ASSIGN_AGENT"}))

    def test_node_count_is_capped(self):
        nodes = [{"id": "t", "type": "TRIGGER"}]
        nodes += [{"id": f"e{i}", "type": "END"} for i in range(services.MAX_NODES)]
        with self.assertRaises(WorkflowError) as ctx:
            services.validate_nodes(nodes)
        self.assertIn("at most", str(ctx.exception))

    def test_trigger_keyword_needs_a_keyword(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes([
                {"id": "t", "type": "TRIGGER", "trigger": {"event": "KEYWORD"}, "next": "e"},
                {"id": "e", "type": "END"},
            ])

    def test_trigger_rejects_an_unknown_event(self):
        with self.assertRaises(WorkflowError):
            services.validate_nodes([
                {"id": "t", "type": "TRIGGER", "trigger": {"event": "TELEPATHY"}, "next": "e"},
                {"id": "e", "type": "END"},
            ])

    def test_a_workflow_with_no_trigger_config_still_validates(self):
        """Every workflow saved before triggers existed must keep working."""
        services.validate_nodes([{"id": "t", "type": "TRIGGER", "next": "e"}, {"id": "e", "type": "END"}])
        self.assertEqual(
            services.trigger_config([{"id": "t", "type": "TRIGGER", "next": "e"}])["event"],
            TriggerEvent.MANUAL,
        )


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class WorkflowAutoTriggerTests(_HardeningBase):
    """Workflows used to be manual-only: the TRIGGER node had no semantics."""

    def _keyword_workflow(self, keyword="demo", **trigger):
        config = {"event": TriggerEvent.KEYWORD, "keyword": keyword, **trigger}
        return self._workflow([
            {"id": "t", "type": "TRIGGER", "trigger": config, "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "Booking you in.", "next": "e"},
            {"id": "e", "type": "END"},
        ])

    def test_keyword_starts_a_workflow(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="w.1")
        workflow = self._keyword_workflow()

        self._deliver("I want a demo")

        run = WorkflowRun.objects.get(workflow=workflow)
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertTrue(Message.objects.filter(body="Booking you in.", is_automated=True).exists())

    def test_non_matching_keyword_starts_nothing(self, mock_send):
        self._keyword_workflow()
        self._deliver("hello")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_manual_workflow_is_never_auto_started(self, mock_send):
        self._workflow([
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "nope", "next": "e"},
            {"id": "e", "type": "END"},
        ])
        self._deliver("anything")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_inactive_workflow_is_never_auto_started(self, mock_send):
        workflow = self._keyword_workflow()
        workflow.is_active = False
        workflow.save(update_fields=["is_active"])
        self._deliver("demo please")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_a_keyword_rule_wins_over_a_keyword_workflow(self, mock_send):
        """One inbound message must never produce two answers."""
        mock_send.return_value = SendResult(success=True, provider_message_id="w.2")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Demo rule",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="demo", message_body="Rule answered.",
        )
        self._keyword_workflow()

        self._deliver("demo")

        self.assertFalse(WorkflowRun.objects.exists())
        outbound = Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True)
        self.assertEqual(outbound.count(), 1)
        self.assertEqual(outbound.first().body, "Rule answered.")

    def test_opt_out_stands_every_workflow_down(self, mock_send):
        mock_send.side_effect = _unique_sends("optout")
        self._keyword_workflow(keyword="stop")
        self._deliver("STOP")
        self.assertFalse(WorkflowRun.objects.exists())

    def test_an_active_run_is_not_restarted_by_the_same_keyword(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="w.3")
        workflow = self._workflow([
            {"id": "t", "type": "TRIGGER", "trigger": {"event": TriggerEvent.KEYWORD, "keyword": "demo"}, "next": "w"},
            {"id": "w", "type": "WAIT", "minutes": 60, "next": "e"},
            {"id": "e", "type": "END"},
        ])
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            for _ in range(3):
                self._deliver("demo")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_new_conversation_trigger_fires_once(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="w.4")
        workflow = self._workflow([
            {"id": "t", "type": "TRIGGER", "trigger": {"event": TriggerEvent.NEW_CONVERSATION}, "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "Nice to meet you.", "next": "e"},
            {"id": "e", "type": "END"},
        ])
        for _ in range(2):
            self._deliver("hi")
        self.assertEqual(WorkflowRun.objects.filter(workflow=workflow).count(), 1)

    def test_a_broken_workflow_does_not_stop_a_healthy_one(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="w.5")
        broken = Workflow(
            tenant=self.tenant, whatsapp_account=self.account, name="Broken",
            nodes=[{"id": "t", "type": "TRIGGER", "trigger": {"event": TriggerEvent.KEYWORD, "keyword": "demo"},
                    "next": "ghost"}],
        )
        broken.save()  # dangling reference, straight past the serializer
        healthy = self._keyword_workflow()

        self._deliver("demo")

        self.assertFalse(WorkflowRun.objects.filter(workflow=broken).exists())
        self.assertTrue(WorkflowRun.objects.filter(workflow=healthy).exists())


@patch("apps.messaging.services.ConnectionManager.send_text_message")
class AutoReplyThrottleTests(_HardeningBase):
    def test_repeated_keyword_is_answered_once_inside_the_cooldown(self, mock_send):
        mock_send.return_value = SendResult(success=True, provider_message_id="c.1")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Price",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Our price list...", cooldown_seconds=600,
        )

        for _ in range(10):
            self._deliver("price?")

        self.assertEqual(
            Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).count(), 1
        )

    def test_different_contacts_are_not_throttled_by_each_other(self, mock_send):
        mock_send.side_effect = _unique_sends("throttle")
        Contact.objects.create(tenant=self.tenant, created_by=self.user, name="Sam", phone="+15551230002")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Price",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Our price list...", cooldown_seconds=600,
        )

        for number in ("15551230001", "15551230002"):
            self._deliver("price?", from_number=number)

        self.assertEqual(
            Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).count(), 2
        )

    def test_the_flood_guard_applies_even_with_no_cooldown_set(self, mock_send):
        """cooldown_seconds=0 keeps the old behaviour but still can't burst."""
        mock_send.return_value = SendResult(success=True, provider_message_id="c.3")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Price",
            trigger_type=AutomationRule.Trigger.KEYWORD, keyword="price",
            message_body="Our price list...", cooldown_seconds=0,
        )

        for _ in range(5):
            self._deliver("price?")

        self.assertEqual(
            Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).count(), 1
        )


class FollowUpHardeningTests(_HardeningBase):
    def _rule(self, **kwargs):
        kwargs.setdefault("name", "Nudge")
        kwargs.setdefault("delay_minutes", 60)
        kwargs.setdefault("message_body", "Just checking in!")
        return FollowUpRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, **kwargs
        )

    def _outbound(self, *, minutes_ago=0):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            direction=Message.Direction.OUTBOUND, status=Message.Status.SENT,
            sent_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_a_superseded_message_does_not_nudge(self):
        rule = self._rule()
        first = self._outbound(minutes_ago=120)
        self._outbound(minutes_ago=5)  # we have said something more recent since

        with patch.object(services, "send_automated_message") as mock_send:
            services.run_follow_up_check(first.pk, rule.pk)
        mock_send.assert_not_called()

    def test_an_unsubscribed_contact_is_not_nudged(self):
        rule = self._rule()
        message = self._outbound()
        self.contact.subscribed = False
        self.contact.save(update_fields=["subscribed"])

        with patch.object(services, "send_automated_message") as mock_send:
            services.run_follow_up_check(message.pk, rule.pk)
        mock_send.assert_not_called()

    def test_the_latest_message_still_nudges(self):
        rule = self._rule()
        message = self._outbound()
        with patch.object(services, "send_automated_message") as mock_send:
            services.run_follow_up_check(message.pk, rule.pk)
        mock_send.assert_called_once()

    def test_follow_up_rules_armed_per_message_are_capped(self):
        for i in range(services.MAX_FOLLOW_UP_RULES_PER_MESSAGE + 5):
            self._rule(name=f"Nudge {i}")
        message = self._outbound()

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)

        self.assertEqual(mock_apply.call_count, services.MAX_FOLLOW_UP_RULES_PER_MESSAGE)


class FollowUpTemplateScopingTests(_HardeningBase):
    """
    A follow-up rule scoped to specific templates must only be armed by an
    outbound message that actually used one of them -- see
    services.schedule_follow_up_checks and Message.template.
    """

    def _template(self, name="Promo"):
        return MessageTemplate.objects.create(tenant=self.tenant, name=name, body="Hi [Name]!")

    def _rule(self, **kwargs):
        kwargs.setdefault("name", "Nudge")
        kwargs.setdefault("delay_minutes", 60)
        kwargs.setdefault("message_body", "Just checking in!")
        templates = kwargs.pop("trigger_templates", None)
        rule = FollowUpRule.objects.create(tenant=self.tenant, whatsapp_account=self.account, **kwargs)
        if templates:
            rule.trigger_templates.set(templates)
        return rule

    def _outbound(self, *, template=None):
        return Message.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, contact=self.contact, to_phone=self.contact.phone,
            direction=Message.Direction.OUTBOUND, status=Message.Status.SENT, sent_at=timezone.now(),
            template=template,
        )

    def test_unscoped_rule_still_arms_for_every_message(self):
        self._rule()  # trigger_templates left empty -- the pre-scoping default
        message = self._outbound()

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)
        self.assertEqual(mock_apply.call_count, 1)

    def test_scoped_rule_skips_a_message_from_a_different_template(self):
        promo = self._template("Promo")
        other = self._template("Reminder")
        self._rule(trigger_templates=[promo])
        message = self._outbound(template=other)

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)
        mock_apply.assert_not_called()

    def test_scoped_rule_skips_a_plain_message_with_no_template(self):
        promo = self._template("Promo")
        self._rule(trigger_templates=[promo])
        message = self._outbound(template=None)

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)
        mock_apply.assert_not_called()

    def test_scoped_rule_arms_for_its_own_template(self):
        promo = self._template("Promo")
        self._rule(trigger_templates=[promo])
        message = self._outbound(template=promo)

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)
        self.assertEqual(mock_apply.call_count, 1)

    def test_one_message_can_arm_both_a_scoped_and_an_unscoped_rule(self):
        promo = self._template("Promo")
        self._rule(name="Scoped", trigger_templates=[promo])
        self._rule(name="Unscoped")
        message = self._outbound(template=promo)

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(message)
        self.assertEqual(mock_apply.call_count, 2)

    def test_a_rule_scoped_to_several_templates_arms_for_any_of_them(self):
        promo = self._template("Promo")
        reminder = self._template("Reminder")
        self._rule(trigger_templates=[promo, reminder])

        with patch("apps.automation.tasks.check_and_send_follow_up.apply_async") as mock_apply:
            services.schedule_follow_up_checks(self._outbound(template=reminder))
        self.assertEqual(mock_apply.call_count, 1)

    def test_automated_sends_carry_the_template_they_used(self):
        """send_automated_message must stamp Message.template from the rule's
        own content, so a workflow/rule send is itself correctly attributed
        (even though is_automated sends never re-arm follow-ups)."""
        promo = self._template("Promo")
        AutomationRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Welcome",
            trigger_type=AutomationRule.Trigger.WELCOME, template=promo,
        )
        with patch("apps.messaging.services.ConnectionManager.send_text_message") as mock_send:
            mock_send.side_effect = _unique_sends("welcome")
            message = self._deliver(body="hi")
        self.assertIsNotNone(message)
        sent = Message.objects.filter(direction=Message.Direction.OUTBOUND, is_automated=True).order_by("-pk").first()
        self.assertIsNotNone(sent)
        self.assertEqual(sent.template_id, promo.pk)

    def test_serializer_rejects_a_template_from_another_tenant(self):
        from rest_framework.test import APIClient

        other_tenant = Tenant.objects.create(name="Someone Else")
        foreign_template = MessageTemplate.objects.create(tenant=other_tenant, name="Not yours", body="Hi")

        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(
            "/api/v1/automation/follow-ups/",
            {
                "name": "Nudge", "whatsapp_account": self.account.pk, "delay_minutes": 60,
                "message_body": "Hi", "trigger_templates": [foreign_template.pk],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("trigger_templates", response.data)


class HardenedApiTests(_HardeningBase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_saving_a_looping_workflow_is_a_400(self):
        response = self.client.post("/api/v1/automation/workflows/", {
            "name": "Loop", "whatsapp_account": self.account.pk,
            "nodes": [
                {"id": "t", "type": "TRIGGER", "next": "m"},
                {"id": "m", "type": "MESSAGE", "body": "hi", "next": "m"},
            ],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("loop forever", str(response.data))

    def test_running_a_workflow_twice_is_a_409(self):
        workflow = self._workflow([
            {"id": "t", "type": "TRIGGER", "next": "w"},
            {"id": "w", "type": "WAIT", "minutes": 60, "next": "e"},
            {"id": "e", "type": "END"},
        ])
        url = f"/api/v1/automation/workflows/{workflow.pk}/run/"
        with patch("apps.automation.tasks.resume_workflow_run.apply_async"):
            first = self.client.post(url, {"contact_id": self.contact.pk}, format="json")
            second = self.client.post(url, {"contact_id": self.contact.pk}, format="json")
            forced = self.client.post(url, {"contact_id": self.contact.pk, "force": True}, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(forced.status_code, 201)

    def test_workflow_list_reports_its_trigger(self):
        self._workflow([
            {"id": "t", "type": "TRIGGER", "trigger": {"event": TriggerEvent.KEYWORD, "keyword": "demo"}, "next": "e"},
            {"id": "e", "type": "END"},
        ])
        response = self.client.get("/api/v1/automation/workflows/")
        self.assertEqual(response.status_code, 200)
        trigger = response.data["results"][0]["trigger"]
        self.assertEqual(trigger["event"], TriggerEvent.KEYWORD)
        self.assertEqual(trigger["keyword"], "demo")

    def test_run_detail_exposes_the_failure_reason(self):
        workflow = self._workflow([
            {"id": "t", "type": "TRIGGER", "next": "m"},
            {"id": "m", "type": "MESSAGE", "body": "hi", "next": "e"},
            {"id": "e", "type": "END"},
        ])
        with patch.object(services, "send_automated_message", side_effect=RuntimeError("provider down")):
            services.start_workflow(workflow, self.contact)
        run = WorkflowRun.objects.get(workflow=workflow)

        response = self.client.get(f"/api/v1/automation/workflow-runs/{run.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "FAILED")
        self.assertIn("provider down", response.data["error"])


class AutomationPresetTests(_HardeningBase):
    """
    The simple-mode presets, which have to end up as ordinary rules -- the
    whole point is that they reuse the proven engine rather than being a
    second one. See presets.py.
    """

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_catalog_lists_every_preset_with_its_questions(self):
        response = self.client.get("/api/v1/automation/presets/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), len(presets.PRESETS))
        for preset in response.data["results"]:
            self.assertTrue(preset["name"])
            self.assertTrue(preset["goal"])
            self.assertTrue(preset["fields"])

    def test_activating_the_welcome_preset_creates_a_real_welcome_rule(self):
        response = self.client.post("/api/v1/automation/presets/welcome_new_lead/activate/", {
            "whatsapp_account": self.account.pk,
            "message_body": "Hi there, thanks for reaching out!",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        rule = AutomationRule.objects.get(pk=response.data["rule"]["id"])
        self.assertEqual(rule.trigger_type, AutomationRule.Trigger.WELCOME)
        self.assertTrue(rule.is_active)
        self.assertEqual(rule.tenant_id, self.tenant.pk)

    def test_activating_the_follow_up_preset_creates_a_real_follow_up_rule(self):
        response = self.client.post("/api/v1/automation/presets/no_reply_follow_up/activate/", {
            "whatsapp_account": self.account.pk,
            "delay_minutes": 2880,
            "message_body": "Just checking in!",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        rule = FollowUpRule.objects.get(pk=response.data["rule"]["id"])
        self.assertEqual(rule.delay_minutes, 2880)
        self.assertEqual(rule.trigger_templates.count(), 0)

    def test_a_scoped_preset_keeps_its_template_scoping(self):
        """The template-scoped follow-up behaviour has to survive simple mode."""
        template = MessageTemplate.objects.create(
            tenant=self.tenant, created_by=self.user, name="Quotation", body="Your quote",
        )
        response = self.client.post("/api/v1/automation/presets/quote_follow_up/activate/", {
            "whatsapp_account": self.account.pk,
            "delay_minutes": 4320,
            "message_body": "Did you see the quote?",
            "trigger_templates": [template.pk],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        rule = FollowUpRule.objects.get(pk=response.data["rule"]["id"])
        self.assertEqual(list(rule.trigger_templates.all()), [template])

    def test_a_keyword_preset_gets_a_cooldown_so_it_cannot_spam(self):
        response = self.client.post("/api/v1/automation/presets/price_question_reply/activate/", {
            "whatsapp_account": self.account.pk,
            "keyword": "price",
            "message_body": "Happy to share pricing!",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        rule = AutomationRule.objects.get(pk=response.data["rule"]["id"])
        self.assertGreater(rule.cooldown_seconds, 0)

    def test_an_empty_message_is_refused(self):
        response = self.client.post("/api/v1/automation/presets/no_reply_follow_up/activate/", {
            "whatsapp_account": self.account.pk, "message_body": "   ",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_preset_is_refused(self):
        response = self.client.post("/api/v1/automation/presets/not_a_preset/activate/", {
            "whatsapp_account": self.account.pk, "message_body": "hi",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_another_tenants_account_cannot_be_used(self):
        other = Tenant.objects.create(name="Other", slug="other-preset", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-preset", password="x", tenant=other)
        foreign = _connected_account(other, other_user)
        response = self.client.post("/api/v1/automation/presets/welcome_new_lead/activate/", {
            "whatsapp_account": foreign.pk, "message_body": "hi",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AutomationRule.objects.filter(tenant=other).count(), 0)

    def test_another_tenants_template_cannot_be_scoped_to(self):
        other = Tenant.objects.create(name="Other", slug="other-preset-tpl", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-preset-tpl", password="x", tenant=other)
        foreign = MessageTemplate.objects.create(tenant=other, created_by=other_user, name="Theirs", body="x")
        response = self.client.post("/api/v1/automation/presets/quote_follow_up/activate/", {
            "whatsapp_account": self.account.pk,
            "message_body": "Did you see the quote?",
            "trigger_templates": [foreign.pk],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        # The half-built rule must not survive a rejected scope.
        self.assertEqual(FollowUpRule.objects.filter(tenant=self.tenant).count(), 0)

    def test_presets_need_authentication(self):
        self.assertEqual(APIClient().get("/api/v1/automation/presets/").status_code, 401)


class AutomationRecommendationTests(_HardeningBase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_a_quiet_account_is_offered_nothing(self):
        response = self.client.get("/api/v1/automation/recommendations/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def _quiet_contacts(self, prefix, count, *, tenant=None, creator=None, days=5):
        tenant = tenant or self.tenant
        creator = creator or self.user
        for index in range(count):
            contact = Contact.objects.create(
                tenant=tenant, created_by=creator, name=f"{prefix}{index}",
                phone=f"+9199{prefix}{index:05d}"[:16],
            )
            Contact.objects.filter(pk=contact.pk).update(last_outbound_at=timezone.now() - timedelta(days=days))

    def test_quiet_leads_produce_a_follow_up_suggestion(self):
        self._quiet_contacts("100", 6)
        response = self.client.get("/api/v1/automation/recommendations/")
        keys = [item["key"] for item in response.data["results"]]
        self.assertIn("no_reply_follow_up", keys)
        suggestion = next(item for item in response.data["results"] if item["key"] == "no_reply_follow_up")
        self.assertEqual(suggestion["action"], "preset")
        self.assertEqual(suggestion["preset"], "no_reply_follow_up")

    def test_the_suggestion_disappears_once_a_follow_up_rule_exists(self):
        self._quiet_contacts("200", 6)
        FollowUpRule.objects.create(
            tenant=self.tenant, whatsapp_account=self.account, name="Nudge", delay_minutes=60,
            message_body="hi", is_active=True,
        )
        response = self.client.get("/api/v1/automation/recommendations/")
        self.assertNotIn("no_reply_follow_up", [item["key"] for item in response.data["results"]])

    def test_recommendations_never_reach_another_tenants_data(self):
        other = Tenant.objects.create(name="Other", slug="other-reco", kind=Tenant.Kind.CUSTOMER)
        other_user = get_user_model().objects.create_user(username="other-reco", password="x", tenant=other)
        self._quiet_contacts("300", 9, tenant=other, creator=other_user, days=9)
        response = self.client.get("/api/v1/automation/recommendations/")
        self.assertEqual(response.data["results"], [])

    def test_recommendations_never_send_anything(self):
        """A suggestion is a link to a form, never an action."""
        self._quiet_contacts("400", 6)
        before = Message.objects.count()
        self.client.get("/api/v1/automation/recommendations/")
        self.assertEqual(Message.objects.count(), before)
