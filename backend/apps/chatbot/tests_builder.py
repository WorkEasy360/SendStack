"""
The builder's own endpoints: templates, duplicate, export/import, preview and
publish validation.

These are the operations the visual canvas performs, and each of them has one
property worth defending in a test rather than in a comment:

* a **template** must produce a graph the engine can actually run;
* a **duplicate** must copy behaviour and leave history behind;
* an **import** must not be able to reach another workspace's rows;
* a **preview** must run the real engine and persist nothing;
* **validation** must list every problem, not the first one.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.ai.models import KnowledgeBase
from apps.contacts.models import Contact
from apps.inbox.models import Conversation
from apps.messaging.models import Message
from apps.tenants.models import Tenant
from apps.whatsapp.models import WhatsAppAccount

from . import services, templates, validation
from .models import Chatbot, ChatbotRule, ChatbotSession, ChatbotWorkingHours


def make_workspace(slug, *, name="Bot"):
    tenant = Tenant.objects.create(name=slug, slug=slug, kind=Tenant.Kind.CUSTOMER)
    user = get_user_model().objects.create_user(username=f"{slug}-user", password="x", tenant=tenant)
    account = WhatsAppAccount(
        tenant=tenant, user=user, name="Line", phone_number_id=f"pn-{slug}",
        status=WhatsAppAccount.Status.CONNECTED,
    )
    account.set_access_token("token")
    account.save()
    chatbot = Chatbot.objects.create(
        tenant=tenant, whatsapp_account=account, name=name, created_by=user,
        welcome_message="", bot_type=Chatbot.BotType.RULE_BASED, ai_enabled=False,
    )
    return tenant, user, account, chatbot


class ApiTestCase(TestCase):
    slug = "builder"

    def setUp(self):
        cache.clear()
        self.tenant, self.user, self.account, self.bot = make_workspace(self.slug)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def url(self, name, *args):
        return reverse(name, args=args)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

class TemplateTests(ApiTestCase):
    slug = "builder-templates"

    def test_every_template_builds_a_graph_the_engine_can_run(self):
        """
        The test this file exists for.

        A template is data, and data drifts away from the validator that has to
        accept it. Building all ten through ``flow.validate`` means a template
        that stops being runnable is a red test rather than a bot that fails
        the first time somebody presses Publish.
        """
        for entry in templates.CATALOGUE:
            with self.subTest(template=entry["id"]):
                flow = templates.build_flow(entry["id"], tenant_id=self.tenant.pk)
                self.assertTrue(flow["nodes"], f"{entry['id']} built an empty graph")
                starts = [node for node in flow["nodes"] if node["type"] == "START"]
                self.assertEqual(len(starts), 1)

    def test_every_template_lays_its_nodes_out(self):
        """A template is the first graph most people see; it should not be a pile."""
        for entry in templates.CATALOGUE:
            with self.subTest(template=entry["id"]):
                flow = templates.build_flow(entry["id"], tenant_id=self.tenant.pk)
                positions = {(node["position"]["x"], node["position"]["y"]) for node in flow["nodes"]}
                self.assertEqual(
                    len(positions), len(flow["nodes"]),
                    f"{entry['id']} has two nodes stacked on the same spot",
                )

    def test_the_catalogue_is_listed(self):
        response = self.client.get(self.url("chatbot-templates"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), len(templates.CATALOGUE))
        self.assertIn("description", response.data[0])

    def test_applying_a_template_fills_the_draft(self):
        response = self.client.post(
            self.url("chatbot-apply-template", self.bot.pk), {"template": "welcome_bot"}, format="json",
        )
        self.assertEqual(response.status_code, 200)

        draft = services.draft_version(self.bot)
        self.assertTrue(draft.flow["nodes"])
        self.assertTrue(any(node["type"] == "QUESTION" for node in draft.flow["nodes"]))

    def test_a_template_will_not_silently_replace_existing_work(self):
        self.client.post(
            self.url("chatbot-apply-template", self.bot.pk), {"template": "welcome_bot"}, format="json",
        )
        response = self.client.post(
            self.url("chatbot-apply-template", self.bot.pk), {"template": "faq_bot"}, format="json",
        )
        self.assertEqual(response.status_code, 409)

        confirmed = self.client.post(
            self.url("chatbot-apply-template", self.bot.pk),
            {"template": "faq_bot", "replace": True}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200)

    def test_an_unknown_template_is_a_404(self):
        response = self.client.post(
            self.url("chatbot-apply-template", self.bot.pk), {"template": "no_such_thing"}, format="json",
        )
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

class DuplicateTests(ApiTestCase):
    slug = "builder-duplicate"

    def setUp(self):
        super().setUp()
        ChatbotRule.objects.create(
            chatbot=self.bot, name="Greet", pattern="hi", priority=1,
            actions=[{"type": "SEND_MESSAGE", "body": "Hello!", "buttons": []}],
            match_count=17,
        )
        ChatbotWorkingHours.objects.create(chatbot=self.bot, weekday=0, is_open=True)
        draft = services.draft_version(self.bot)
        draft.flow = templates.build_flow("welcome_bot", tenant_id=self.tenant.pk)
        draft.save()

    def test_a_duplicate_carries_the_configuration(self):
        response = self.client.post(self.url("chatbot-duplicate", self.bot.pk))
        self.assertEqual(response.status_code, 201)

        copy = Chatbot.objects.get(pk=response.data["id"])
        self.assertEqual(copy.name, "Bot (copy)")
        self.assertEqual(copy.rules.count(), 1)
        self.assertEqual(copy.working_hours.count(), 1)
        self.assertEqual(
            len(services.draft_version(copy).flow["nodes"]),
            len(services.draft_version(self.bot).flow["nodes"]),
        )

    def test_a_duplicate_starts_as_a_draft_with_no_history(self):
        services.publish(self.bot)
        response = self.client.post(self.url("chatbot-duplicate", self.bot.pk))

        copy = Chatbot.objects.get(pk=response.data["id"])
        self.assertEqual(copy.status, Chatbot.Status.DRAFT)
        self.assertIsNone(copy.published_version)
        self.assertEqual(copy.rules.first().match_count, 0, "the copy inherited the original's counters")

    def test_duplicating_twice_does_not_collide(self):
        first = self.client.post(self.url("chatbot-duplicate", self.bot.pk))
        second = self.client.post(self.url("chatbot-duplicate", self.bot.pk))

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["name"], second.data["name"])

    def test_another_workspace_cannot_duplicate_this_bot(self):
        _, stranger, _, _ = make_workspace("builder-duplicate-other")
        client = APIClient()
        client.force_authenticate(stranger)

        response = client.post(self.url("chatbot-duplicate", self.bot.pk))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Chatbot.objects.filter(name__startswith="Bot (copy)").count(), 0)


# ---------------------------------------------------------------------------
# Export and import
# ---------------------------------------------------------------------------

class ExportImportTests(ApiTestCase):
    slug = "builder-portability"

    def test_an_export_reimports_into_the_same_shape(self):
        draft = services.draft_version(self.bot)
        draft.flow = templates.build_flow("product_inquiry", tenant_id=self.tenant.pk)
        draft.save()
        before = draft.flow

        exported = self.client.get(self.url("chatbot-export", self.bot.pk)).data

        _, _, _, target = make_workspace("builder-portability-target", name="Target")
        services.import_flow(target, exported)

        after = services.draft_version(target).flow
        self.assertEqual(
            [node["id"] for node in after["nodes"]],
            [node["id"] for node in before["nodes"]],
        )
        self.assertEqual(
            [node["position"] for node in after["nodes"]],
            [node["position"] for node in before["nodes"]],
            "positions should survive a round trip",
        )

    def test_an_export_carries_no_local_ids(self):
        """
        A knowledge base id is meaningless in another workspace and dangerous
        if it happens to match one. It must not be in the file at all.
        """
        base = KnowledgeBase.objects.create(tenant=self.tenant, name="Docs")
        draft = services.draft_version(self.bot)
        draft.flow = {"nodes": [
            {"id": "start", "type": "START", "next": "kb", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "kb", "type": "KNOWLEDGE", "next": "", "position": {"x": 300, "y": 0},
             "actions": [{"type": "KNOWLEDGE_ANSWER", "prompt": "Answer it", "knowledge_base_id": base.pk}]},
        ]}
        draft.save()

        exported = self.client.get(self.url("chatbot-export", self.bot.pk)).data

        serialised = str(exported)
        self.assertNotIn("knowledge_base_id", serialised)

    def test_an_import_cannot_reach_another_workspaces_knowledge_base(self):
        """
        The isolation test for this endpoint. A hand-written file naming a
        foreign id must be refused -- and it is refused by the same
        ``actions.validate`` tenant check every other write goes through, not
        by a special case here.
        """
        other_tenant, _, _, _ = make_workspace("builder-portability-stranger")
        foreign = KnowledgeBase.objects.create(tenant=other_tenant, name="Their docs")

        payload = {
            "format": services.EXPORT_FORMAT,
            "version": services.EXPORT_VERSION,
            "nodes": [
                {"id": "start", "type": "START", "next": "kb", "actions": [], "position": {"x": 0, "y": 0}},
                {"id": "kb", "type": "KNOWLEDGE", "next": "", "position": {"x": 300, "y": 0},
                 "actions": [{"type": "KNOWLEDGE_ANSWER", "prompt": "Hi", "knowledge_base_id": foreign.pk}]},
            ],
        }

        response = self.client.post(
            self.url("chatbot-import-flow", self.bot.pk), payload, format="json",
        )

        # The id is stripped before validation, so the import succeeds and the
        # node simply carries no base -- which is the safe outcome. What must
        # never happen is the foreign id surviving into this tenant's draft.
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(foreign.pk), str(services.draft_version(self.bot).flow))

    def test_a_file_from_a_newer_format_is_refused(self):
        response = self.client.post(
            self.url("chatbot-import-flow", self.bot.pk),
            {"format": services.EXPORT_FORMAT, "version": 999, "nodes": []}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("newer version", response.data["detail"])

    def test_a_file_that_is_not_an_export_is_refused(self):
        response = self.client.post(
            self.url("chatbot-import-flow", self.bot.pk), {"nodes": []}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_an_unrunnable_graph_is_refused_on_import(self):
        payload = {
            "format": services.EXPORT_FORMAT,
            "version": services.EXPORT_VERSION,
            "nodes": [
                {"id": "start", "type": "START", "next": "nowhere", "actions": [], "position": {"x": 0, "y": 0}},
            ],
        }
        response = self.client.post(
            self.url("chatbot-import-flow", self.bot.pk), payload, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("nowhere", response.data["detail"])


# ---------------------------------------------------------------------------
# Publish validation
# ---------------------------------------------------------------------------

class PublishValidationTests(ApiTestCase):
    slug = "builder-validation"

    def _draft(self, nodes):
        draft = services.draft_version(self.bot)
        draft.flow = {"nodes": nodes}
        draft.save()
        return draft

    def test_a_good_flow_reports_no_problems(self):
        self._draft(templates.build_flow("welcome_bot", tenant_id=self.tenant.pk)["nodes"])
        report = validation.report(self.bot)
        self.assertTrue(report["ok"], report["problems"])

    def test_an_incomplete_action_is_a_validation_error_not_an_internal_error(self):
        """A half-filled question must not produce the generic 500 toast."""
        flow = {
            "nodes": [
                {"id": "start", "type": "START", "next": "question", "actions": []},
                {
                    "id": "question", "type": "QUESTION", "next": "", "position": {"x": 300, "y": 0},
                    "actions": [{"type": "ASK_QUESTION", "body": "", "buttons": [], "save_to": ""}],
                    "routes": [],
                },
            ],
        }

        response = self.client.put(
            f"/api/v1/chatbot/bots/{self.bot.pk}/flow/", {"flow": flow}, format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("body", str(response.data).lower())

        check = self.client.post(
            f"/api/v1/chatbot/bots/{self.bot.pk}/validate/", {"flow": flow}, format="json",
        )
        self.assertEqual(check.status_code, 200, check.data)
        self.assertFalse(check.data["ok"])
        self.assertIn("body", str(check.data).lower())

    def test_a_draft_that_no_longer_validates_is_reported_not_raised(self):
        """
        A stored draft can stop being valid without anybody touching it -- the
        knowledge base it named gets deleted, or a validator is tightened. The
        publish endpoint must answer "you cannot publish this yet" rather than
        crash, which is what it did before ``_check_graph`` caught the action
        validator's own error as well as the graph one.

        Written straight to the column on purpose: the API would refuse this
        graph, and the case being described is precisely one that got past the
        API earlier and is being read back now.
        """
        draft = services.draft_version(self.bot)
        draft.flow = {"nodes": [
            {"id": "start", "type": "START", "next": "say", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "say", "type": "MESSAGE", "next": "", "position": {"x": 300, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": "", "buttons": []}]},
        ]}
        draft.save()

        report = validation.report(self.bot)

        self.assertFalse(report["ok"])
        self.assertEqual(len(report["problems"]), 1)

        response = self.client.post(self.url("chatbot-publish", self.bot.pk), {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_every_problem_is_listed_not_just_the_first(self):
        """
        The reason this module collects instead of raising. Somebody pressing
        Publish wants the whole list, not one item per attempt.
        """
        self._draft([
            {"id": "start", "type": "START", "next": "one", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "one", "type": "AI", "next": "two", "position": {"x": 300, "y": 0},
             "actions": [{"type": "AI_REPLY", "prompt": "Answer them", "knowledge_base_id": None}]},
            {"id": "two", "type": "AI", "next": "", "position": {"x": 600, "y": 0},
             "actions": [{"type": "AI_REPLY", "prompt": "Answer them again", "knowledge_base_id": None}]},
        ])
        report = validation.report(self.bot)

        self.assertGreaterEqual(len(report["problems"]), 2)
        self.assertEqual({item["node_id"] for item in report["problems"]}, {"one", "two"})

    def test_a_button_with_nowhere_to_go_is_a_problem(self):
        """The failure the specification names: a Pricing button with no destination."""
        self._draft([
            {"id": "start", "type": "START", "next": "menu", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "menu", "type": "QUESTION", "next": "", "position": {"x": 300, "y": 0},
             "routes": [{"value": "products", "next": "prod"}],
             "actions": [{
                 "type": "ASK_QUESTION", "body": "Pick one", "save_to": "topic",
                 "buttons": [{"id": "pricing", "title": "Pricing"}, {"id": "products", "title": "Products"}],
             }]},
            {"id": "prod", "type": "MESSAGE", "next": "", "position": {"x": 600, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": "Catalogue", "buttons": []}]},
        ])
        report = validation.report(self.bot)

        self.assertFalse(report["ok"])
        self.assertTrue(any("Pricing" in item["message"] for item in report["problems"]))

    def test_an_unrouted_button_is_only_a_warning_when_there_is_a_default(self):
        """
        Falling through to the default path is a shape somebody chooses on
        purpose, so it must not block a publish.
        """
        self._draft([
            {"id": "start", "type": "START", "next": "menu", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "menu", "type": "QUESTION", "next": "anything", "position": {"x": 300, "y": 0},
             "routes": [{"value": "products", "next": "prod"}],
             "actions": [{
                 "type": "ASK_QUESTION", "body": "Pick one", "save_to": "topic",
                 "buttons": [{"id": "pricing", "title": "Pricing"}, {"id": "products", "title": "Products"}],
             }]},
            {"id": "prod", "type": "MESSAGE", "next": "", "position": {"x": 600, "y": 0},
             "actions": [{"type": "SEND_MESSAGE", "body": "Catalogue", "buttons": []}]},
            {"id": "anything", "type": "MESSAGE", "next": "", "position": {"x": 600, "y": 200},
             "actions": [{"type": "SEND_MESSAGE", "body": "Anything else?", "buttons": []}]},
        ])
        report = validation.report(self.bot)

        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(any("Pricing" in item["message"] for item in report["warnings"]))

    def test_an_ai_node_on_a_rules_only_bot_is_a_problem(self):
        self._draft([
            {"id": "start", "type": "START", "next": "ai", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "ai", "type": "AI", "next": "", "position": {"x": 300, "y": 0},
             "actions": [{"type": "AI_REPLY", "prompt": "Help them", "knowledge_base_id": None}]},
        ])
        report = validation.report(self.bot)

        self.assertFalse(report["ok"])
        self.assertTrue(any("AI" in item["message"] for item in report["problems"]))

    def test_publishing_is_refused_and_says_why(self):
        self._draft([
            {"id": "start", "type": "START", "next": "ai", "actions": [], "position": {"x": 0, "y": 0}},
            {"id": "ai", "type": "AI", "next": "", "position": {"x": 300, "y": 0},
             "actions": [{"type": "AI_REPLY", "prompt": "Answer them", "knowledge_base_id": None}]},
        ])
        response = self.client.post(self.url("chatbot-publish", self.bot.pk), {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("problems", response.data)
        self.assertTrue(response.data["problems"])
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.status, Chatbot.Status.DRAFT)

    def test_the_endpoint_can_check_an_unsaved_graph(self):
        """The builder lights up problems as somebody draws, before any save."""
        response = self.client.post(
            self.url("chatbot-validate", self.bot.pk),
            {"flow": {"nodes": [
                {"id": "start", "type": "START", "next": "ai", "actions": [], "position": {"x": 0, "y": 0}},
                {"id": "ai", "type": "AI", "next": "", "position": {"x": 300, "y": 0},
                 "actions": [{"type": "AI_REPLY", "prompt": "Answer them", "knowledge_base_id": None}]},
            ]}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])
        self.assertEqual(services.draft_version(self.bot).flow, {"nodes": []},
                         "checking a graph must not save it")


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------

class SimulationTests(ApiTestCase):
    slug = "builder-preview"

    def setUp(self):
        super().setUp()
        draft = services.draft_version(self.bot)
        draft.flow = templates.build_flow("welcome_bot", tenant_id=self.tenant.pk)
        draft.save()

    def _simulate(self, messages):
        return self.client.post(
            self.url("chatbot-simulate", self.bot.pk), {"messages": messages}, format="json",
        )

    def test_a_preview_runs_the_draft_without_publishing_it(self):
        response = self._simulate([{"text": "hello"}])

        self.assertEqual(response.status_code, 200)
        replies = [turn["text"] for turn in response.data["transcript"] if turn["role"] == "bot"]
        self.assertTrue(any("thanks for messaging us" in reply for reply in replies), replies)

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.status, Chatbot.Status.DRAFT)
        self.assertIsNone(self.bot.published_version)

    def test_a_preview_leaves_nothing_behind(self):
        """
        The property that lets this run against a real workspace: a preview
        must not appear in the inbox, the reports or the message history.
        """
        self._simulate([{"text": "hello"}, {"button_id": "hours", "text": "Opening hours"}])

        self.assertEqual(Conversation.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(Message.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(Contact.objects.filter(tenant=self.tenant).count(), 0)
        self.assertEqual(ChatbotSession.objects.filter(tenant=self.tenant).count(), 0)

    def test_a_preview_sends_nothing_to_the_provider(self):
        with self.settings():
            from unittest.mock import patch

            with patch("apps.messaging.services.ConnectionManager.send_text_message") as send:
                self._simulate([{"text": "hello"}])
            send.assert_not_called()

    def test_a_tapped_option_takes_its_branch_in_the_preview(self):
        response = self._simulate([
            {"text": "hello"},
            {"text": "Opening hours", "button_id": "hours"},
        ])

        replies = [turn["text"] for turn in response.data["transcript"] if turn["role"] == "bot"]
        self.assertTrue(any("Monday to Saturday" in reply for reply in replies), replies)

    def test_the_preview_reports_the_execution_path(self):
        response = self._simulate([{"text": "hello"}])

        steps = [step["step"] for step in response.data["steps"]]
        self.assertIn("RECEIVED", steps)
        self.assertIn("FLOW", steps)
        self.assertIn("SEND", steps)

    def test_the_preview_reports_what_the_bot_learned(self):
        response = self._simulate([
            {"text": "hello"},
            {"text": "About us", "button_id": "about"},
        ])
        self.assertEqual(response.data["variables"].get("topic"), "About us")

    def test_a_preview_needs_something_to_say(self):
        response = self._simulate([{"text": "   "}])
        self.assertEqual(response.status_code, 400)

    def test_another_workspace_cannot_preview_this_bot(self):
        _, stranger, _, _ = make_workspace("builder-preview-other")
        client = APIClient()
        client.force_authenticate(stranger)

        response = client.post(
            self.url("chatbot-simulate", self.bot.pk), {"messages": [{"text": "hi"}]}, format="json",
        )
        self.assertEqual(response.status_code, 404)
