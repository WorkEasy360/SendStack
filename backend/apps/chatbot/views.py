"""
The chatbot API.

Every queryset in this file starts from ``self.request.user.tenant``. That is
not a convention, it is *the* isolation boundary: the filter is in the query,
so an object belonging to another workspace is not found rather than found and
refused, and there is no code path that fetches first and checks afterwards.

Permissions are declared per view (and per action where they differ) and
enforced by ``HasChatbotPermission``, which refuses anything that declares
nothing -- forgetting the attribute must not publish an endpoint.
"""

import logging

from django.db.models import Count, OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.messaging.models import Message
from apps.messaging.serializers import MessageSerializer

from . import analytics, matching, services, validation
from . import templates as bot_templates
from .models import (
    Chatbot,
    ChatbotEvent,
    ChatbotHandoff,
    ChatbotMediaAsset,
    ChatbotRule,
    ChatbotSession,
    ChatbotVersion,
    ChatbotWorkingHours,
)
from .permissions import HasChatbotPermission, Permission, permissions_for
from .serializers import (
    ChatbotEventSerializer,
    ChatbotHandoffSerializer,
    ChatbotMediaAssetSerializer,
    ChatbotRuleSerializer,
    ChatbotSerializer,
    ChatbotSessionSerializer,
    ChatbotVersionSerializer,
    ChatbotWorkingHoursSerializer,
    FlowSerializer,
    PublishSerializer,
    ReplySerializer,
    RollbackSerializer,
    RuleTestSerializer,
    SimulationSerializer,
    ApplyTemplateSerializer,
)

logger = logging.getLogger("sendstack")


def _first_error(errors) -> str:
    """
    One readable sentence out of a DRF error structure.

    Written out rather than reached for from a utility because the shape is
    genuinely unpredictable -- a dict of lists, a list of dicts, or a bare
    string, depending on which serializer failed and how -- and the only thing
    the caller wants is the first sentence a person can act on.
    """
    if isinstance(errors, dict):
        for value in errors.values():
            found = _first_error(value)
            if found:
                return found
        return ""
    if isinstance(errors, (list, tuple)):
        for value in errors:
            found = _first_error(value)
            if found:
                return found
        return ""
    return str(errors)


class TenantScopedViewSet(viewsets.ModelViewSet):
    """
    A viewset whose every read and write is already inside one workspace.

    ``base_queryset`` is the *only* place a subclass names its model, so there
    is no second, unfiltered path to the same rows for somebody to reach for
    later.
    """

    permission_classes = [IsAuthenticated, HasChatbotPermission]

    def get_queryset(self):
        return self.base_queryset().filter(tenant=self.request.user.tenant)

    def base_queryset(self):
        raise NotImplementedError

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "request": self.request}

    def audit(self, action_name, instance, **metadata):
        log_event(
            request=self.request, action=action_name, tenant=self.request.user.tenant,
            object_type=instance.__class__.__name__.lower(), object_id=instance.pk, metadata=metadata,
        )


class ChatbotViewSet(TenantScopedViewSet):
    """Create, configure, publish, pause and roll back a chatbot."""

    serializer_class = ChatbotSerializer
    required_permission = Permission.CHATBOT_VIEW
    action_permissions = {
        "create": [Permission.CHATBOT_CREATE],
        "update": [Permission.CHATBOT_EDIT],
        "partial_update": [Permission.CHATBOT_EDIT],
        "destroy": [Permission.CHATBOT_DELETE],
        "publish": [Permission.CHATBOT_PUBLISH],
        "pause": [Permission.CHATBOT_PUBLISH],
        "resume": [Permission.CHATBOT_PUBLISH],
        "archive": [Permission.CHATBOT_PUBLISH],
        "rollback": [Permission.CHATBOT_PUBLISH],
        "flow": [Permission.CHATBOT_EDIT],
        "working_hours": [Permission.CHATBOT_EDIT],
        "analytics": [Permission.CHATBOT_ANALYTICS],
        "validate": [Permission.CHATBOT_EDIT],
        "duplicate": [Permission.CHATBOT_CREATE],
        "export": [Permission.CHATBOT_EDIT],
        "import_flow": [Permission.CHATBOT_EDIT],
        "simulate": [Permission.CHATBOT_EDIT],
        "apply_template": [Permission.CHATBOT_EDIT],
    }
    filterset_fields = {"status": ["exact"], "bot_type": ["exact"], "whatsapp_account": ["exact"]}
    search_fields = ["name", "description"]

    def base_queryset(self):
        return (
            Chatbot.objects
            .select_related("whatsapp_account", "knowledge_base", "published_version")
            .prefetch_related("working_hours")
            .annotate(rule_total=Count("rules", distinct=True))
        )

    def perform_create(self, serializer):
        chatbot = serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)
        self.audit("chatbot.created", chatbot, name=chatbot.name, account=chatbot.whatsapp_account_id)

    def perform_update(self, serializer):
        chatbot = serializer.save()
        self.audit("chatbot.updated", chatbot, name=chatbot.name)

    def perform_destroy(self, instance):
        tenant, pk, name = instance.tenant, instance.pk, instance.name
        instance.delete()
        log_event(
            request=self.request, action="chatbot.deleted", tenant=tenant,
            object_type="chatbot", object_id=pk, metadata={"name": name},
        )

    # -- lifecycle ----------------------------------------------------------

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        """
        Freeze the current configuration and put it in front of customers.

        Refused when another bot is already published on the same number: two
        bots on one line would both answer every message. The database
        constraint would refuse it anyway, but a sentence naming the other bot
        is a better answer than an integrity error.
        """
        chatbot = self.get_object()
        serializer = PublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        clash = (
            Chatbot.objects
            .filter(whatsapp_account_id=chatbot.whatsapp_account_id, status=Chatbot.Status.PUBLISHED)
            .exclude(pk=chatbot.pk)
            .values_list("name", flat=True)
            .first()
        )
        if clash:
            return Response(
                {"detail": f"'{clash}' is already published on this number. Pause it first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        report = validation.report(chatbot)
        if not report["ok"]:
            # The list travels with the 400 so the Publish dialog can show every
            # problem at once. `detail` stays a single sentence because that is
            # what every other error in this API returns and what the generic
            # toast renders when nothing knows to look for `problems`.
            first = report["problems"][0]["message"]
            more = len(report["problems"]) - 1
            return Response(
                {
                    "detail": first if not more else f"{first} (and {more} more problem{'s' if more > 1 else ''})",
                    **report,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        version = services.publish(chatbot, user=request.user, notes=serializer.validated_data.get("notes", ""))
        self.audit("chatbot.published", chatbot, version=version.version)
        return Response(ChatbotSerializer(chatbot, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["get", "post"])
    def validate(self, request, pk=None):
        """
        Everything standing between this bot and a publish, without publishing.

        POST may carry a ``flow`` the browser has not saved yet, so the builder
        can light up problems as somebody draws rather than only when they give
        up and press Publish. That payload is checked, never stored, and the
        publish endpoint reads the *saved* draft regardless -- a check on one
        graph must not be able to authorise a publish of another.
        """
        chatbot = self.get_object()

        draft_flow = None
        if request.method == "POST" and "flow" in request.data:
            serializer = FlowSerializer(
                data=request.data, context={**self.get_serializer_context(), "chatbot": chatbot},
            )
            if not serializer.is_valid():
                # A graph too broken to normalise is itself the finding, phrased
                # the way the rest of the report is rather than as a DRF field
                # error the dialog would have to special-case.
                return Response({
                    "ok": False,
                    "problems": [{
                        "severity": "problem",
                        "message": _first_error(serializer.errors),
                        "node_id": "", "node_label": "",
                    }],
                    "warnings": [],
                })
            draft_flow = serializer.validated_data["flow"]

        return Response(validation.report(chatbot, flow=draft_flow))

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        """
        Stop this bot answering, without losing what it is.

        The published version is deliberately kept: pausing is temporary, and a
        pause that discarded the live version would make resuming a republish.
        """
        chatbot = self.get_object()
        chatbot.status = Chatbot.Status.PAUSED
        chatbot.save(update_fields=["status", "updated_at"])
        self.audit("chatbot.paused", chatbot)
        return Response(ChatbotSerializer(chatbot, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        chatbot = self.get_object()
        if chatbot.published_version_id is None:
            return Response(
                {"detail": "This bot has never been published, so there is nothing to resume. Publish it."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        clash = (
            Chatbot.objects
            .filter(whatsapp_account_id=chatbot.whatsapp_account_id, status=Chatbot.Status.PUBLISHED)
            .exclude(pk=chatbot.pk)
            .values_list("name", flat=True)
            .first()
        )
        if clash:
            return Response(
                {"detail": f"'{clash}' is already published on this number. Pause it first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        chatbot.status = Chatbot.Status.PUBLISHED
        chatbot.save(update_fields=["status", "updated_at"])
        self.audit("chatbot.resumed", chatbot)
        return Response(ChatbotSerializer(chatbot, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        chatbot = self.get_object()
        chatbot.status = Chatbot.Status.ARCHIVED
        chatbot.save(update_fields=["status", "updated_at"])
        self.audit("chatbot.archived", chatbot)
        return Response(ChatbotSerializer(chatbot, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def rollback(self, request, pk=None):
        chatbot = self.get_object()
        serializer = RollbackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        version = ChatbotVersion.objects.filter(
            pk=serializer.validated_data["version_id"], chatbot=chatbot,
        ).first()
        if version is None:
            return Response({"detail": "That version does not exist."}, status=status.HTTP_404_NOT_FOUND)

        try:
            services.rollback(chatbot, version, user=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        self.audit("chatbot.rolled_back", chatbot, version=version.version)
        return Response(ChatbotSerializer(chatbot, context=self.get_serializer_context()).data)

    # -- the draft ----------------------------------------------------------

    @action(detail=True, methods=["get", "put"])
    def flow(self, request, pk=None):
        """Read or replace the draft graph. Publishing is what makes it live."""
        chatbot = self.get_object()
        draft = services.draft_version(chatbot)

        if request.method == "GET":
            return Response({
                "version": draft.version,
                "flow": draft.flow or {"nodes": []},
                "published_version": getattr(chatbot.published_version, "version", None),
                "published_flow": (chatbot.published_version.flow if chatbot.published_version else None),
            })

        serializer = FlowSerializer(
            data=request.data, context={**self.get_serializer_context(), "chatbot": chatbot},
        )
        serializer.is_valid(raise_exception=True)
        draft.flow = serializer.validated_data["flow"]
        draft.save(update_fields=["flow"])
        self.audit("chatbot.flow_saved", chatbot, nodes=len(draft.flow.get("nodes", [])))
        return Response({"version": draft.version, "flow": draft.flow})

    # -- copying, sharing and previewing -------------------------------------

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """
        A second bot with the same configuration and none of the history.

        What is copied is everything that describes *behaviour*: the settings,
        the rules, the working hours and the draft graph. What is not copied is
        everything that describes *what happened* -- conversations, sessions,
        handoffs, the trail, and the version history. Those belong to the bot
        that lived them, and a copy carrying them would report another bot's
        numbers on its own analytics screen.

        The copy is always a DRAFT, whatever the original was. Publishing is a
        decision about a number, and one number may only have one published bot
        -- so a duplicate that arrived already published would either be refused
        or would silently take a live conversation over.
        """
        chatbot = self.get_object()
        copy = services.duplicate(chatbot, user=request.user)
        self.audit("chatbot.duplicated", copy, source=chatbot.pk, name=copy.name)
        return Response(
            ChatbotSerializer(copy, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"])
    def export(self, request, pk=None):
        """
        The draft graph as portable JSON.

        Deliberately not a database dump. Ids of other rows -- a knowledge base,
        a campaign, an agent, a WhatsApp number -- are meaningless in the
        workspace this file is imported into and dangerous if they happen to
        match something, so ``services.export_flow`` strips them along with
        anything that looks like a credential. What survives is the shape of the
        conversation, which is the part worth carrying.
        """
        chatbot = self.get_object()
        return Response(services.export_flow(chatbot))

    @action(detail=True, methods=["post"], url_path="import")
    def import_flow(self, request, pk=None):
        """
        Replace the draft graph with an exported one.

        The payload is treated as hostile: it is stripped of ids and secrets,
        then put through ``flow.validate`` with *this* request's tenant, which
        is where every remaining reference is confirmed to belong here. An
        import can therefore never reach another workspace's rows, whatever the
        file claims.
        """
        chatbot = self.get_object()
        try:
            flow = services.import_flow(chatbot, request.data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        self.audit("chatbot.flow_imported", chatbot, nodes=len(flow.get("nodes", [])))
        return Response({"flow": flow, "nodes": len(flow.get("nodes", []))})

    @action(detail=True, methods=["post"])
    def simulate(self, request, pk=None):
        """
        Talk to the draft, without a phone and without leaving a trace.

        The browser sends the whole transcript so far and gets the whole
        conversation back, because the preview keeps nothing between requests --
        see ``services.simulate`` for why replaying is the honest design here.
        """
        chatbot = self.get_object()
        serializer = SimulationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = services.simulate(chatbot, serializer.validated_data["messages"])
        # Not audited as a send, because nothing was sent. Audited at all
        # because "who has been running this bot against the draft" is a
        # reasonable question for a workspace owner to ask.
        self.audit("chatbot.simulated", chatbot, turns=len(serializer.validated_data["messages"]))
        return Response(result)

    @action(detail=False, methods=["get"])
    def templates(self, request):
        """The starter flows, for the Create-from-template picker."""
        return Response(bot_templates.listing())

    @action(detail=True, methods=["post"], url_path="apply-template")
    def apply_template(self, request, pk=None):
        """
        Drop a starter flow into this bot's draft.

        Refused when the draft already holds a graph unless ``replace`` is
        explicitly true: a template is a starting point, and quietly discarding
        an afternoon's work because somebody clicked the wrong card is not a
        recoverable mistake -- the draft is the one version with no history
        behind it.
        """
        chatbot = self.get_object()
        serializer = ApplyTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        draft = services.draft_version(chatbot)
        if (draft.flow or {}).get("nodes") and not serializer.validated_data["replace"]:
            return Response(
                {"detail": "This bot already has a flow. Confirm that you want to replace it."},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            flow = bot_templates.build_flow(
                serializer.validated_data["template"], tenant_id=chatbot.tenant_id,
            )
        except KeyError:
            return Response({"detail": "That template does not exist."}, status=status.HTTP_404_NOT_FOUND)

        draft.flow = flow
        draft.save(update_fields=["flow"])
        self.audit(
            "chatbot.template_applied", chatbot,
            template=serializer.validated_data["template"], nodes=len(flow.get("nodes", [])),
        )
        return Response({"version": draft.version, "flow": flow})

    @action(detail=True, methods=["get"])
    def versions(self, request, pk=None):
        chatbot = self.get_object()
        queryset = chatbot.versions.select_related("published_by").order_by("-version")
        return Response(ChatbotVersionSerializer(queryset, many=True).data)

    @action(detail=True, methods=["get", "put"], url_path="working-hours")
    def working_hours(self, request, pk=None):
        chatbot = self.get_object()

        if request.method == "GET":
            return Response(ChatbotWorkingHoursSerializer(chatbot.working_hours.all(), many=True).data)

        serializer = ChatbotWorkingHoursSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        rows = serializer.validated_data
        if len({row["weekday"] for row in rows}) != len(rows):
            return Response({"detail": "Each weekday may appear once."}, status=status.HTTP_400_BAD_REQUEST)

        # Replaced wholesale rather than merged: the form sends all seven days,
        # and a merge would leave a day the user removed silently in place.
        chatbot.working_hours.all().delete()
        ChatbotWorkingHours.objects.bulk_create(
            [ChatbotWorkingHours(chatbot=chatbot, **row) for row in rows]
        )
        self.audit("chatbot.hours_saved", chatbot, days=len(rows))
        return Response(ChatbotWorkingHoursSerializer(chatbot.working_hours.all(), many=True).data)

    # -- trying it out ------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="test-rules")
    def test_rules(self, request, pk=None):
        """
        Which rule would answer this message, and what it would do.

        Sends nothing. Runs the engine's own matcher over the engine's own rule
        source, so the answer is what would actually happen rather than a
        second implementation's opinion of it.
        """
        chatbot = self.get_object()
        serializer = RuleTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data["use_published"] and chatbot.published_version is not None:
            rules = chatbot.published_version.rules
            source = f"published v{chatbot.published_version.version}"
        else:
            rules = [matching.as_dict(rule) for rule in chatbot.rules.order_by("priority", "id")]
            source = "current draft"

        checked = []
        winner = None
        for rule in rules:
            if not rule.get("is_active", True):
                checked.append({**_rule_summary(rule), "matched": False, "skipped": "inactive"})
                continue
            matched = matching.rule_matches(
                rule, text=data["message"], button_id=data.get("button_id", ""),
            )
            checked.append({**_rule_summary(rule), "matched": matched})
            if matched and winner is None:
                winner = rule

        return Response({
            "source": source,
            "message": data["message"],
            "matched_rule": _rule_summary(winner) if winner else None,
            "actions": winner.get("actions") if winner else [],
            "would_reach_ai": winner is None and chatbot.ai_enabled
            and chatbot.bot_type in (Chatbot.BotType.AI_BASED, Chatbot.BotType.HYBRID),
            "rules_checked": checked,
        })

    @action(detail=True, methods=["get"])
    def analytics(self, request, pk=None):
        chatbot = self.get_object()
        days = request.query_params.get("days")
        return Response({
            **analytics.overview(request.user.tenant_id, days=days, chatbot_id=chatbot.pk),
            "rules": analytics.rule_performance(chatbot),
            "handoffs": analytics.handoff_breakdown(request.user.tenant_id, days=days or 30, chatbot_id=chatbot.pk),
            "flow_usage": analytics.flow_usage(request.user.tenant_id, days=days or 30, chatbot_id=chatbot.pk),
            "top_questions": analytics.top_questions(request.user.tenant_id, days=days or 30, chatbot_id=chatbot.pk),
            "top_intents": analytics.top_intents(request.user.tenant_id, days=days or 30, chatbot_id=chatbot.pk),
        })


def _rule_summary(rule) -> dict:
    return {
        "id": rule.get("id"), "name": rule.get("name"), "priority": rule.get("priority"),
        "match_type": rule.get("match_type"), "pattern": rule.get("pattern"),
    }


class ChatbotRuleViewSet(TenantScopedViewSet):
    serializer_class = ChatbotRuleSerializer
    required_permission = Permission.CHATBOT_VIEW
    action_permissions = {
        "create": [Permission.CHATBOT_EDIT],
        "update": [Permission.CHATBOT_EDIT],
        "partial_update": [Permission.CHATBOT_EDIT],
        "destroy": [Permission.CHATBOT_EDIT],
        "reorder": [Permission.CHATBOT_EDIT],
    }
    filterset_fields = {"chatbot": ["exact"], "is_active": ["exact"], "match_type": ["exact"]}
    search_fields = ["name", "pattern"]

    def get_queryset(self):
        # Rules have no tenant column of their own -- they belong to a bot,
        # which belongs to a workspace. Filtering through the relation is what
        # makes that one authoritative statement rather than a denormalised
        # copy somebody has to remember to keep in step.
        #
        # `base_queryset` is deliberately NOT implemented on this class: the
        # base one filters on a `tenant` field this model does not have, and a
        # helper here returning `ChatbotRule.objects.all()` would sit in the
        # file looking harmless until somebody deleted this override and got an
        # unscoped list. Inheriting the NotImplementedError makes that mistake
        # loud instead of silent.
        return ChatbotRule.objects.filter(chatbot__tenant=self.request.user.tenant).select_related("chatbot")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        chatbot_id = self.request.data.get("chatbot") if hasattr(self.request, "data") else None
        if chatbot_id:
            context["chatbot"] = Chatbot.objects.filter(
                pk=chatbot_id, tenant=self.request.user.tenant,
            ).first()
        elif self.kwargs.get("pk"):
            rule = ChatbotRule.objects.filter(
                pk=self.kwargs["pk"], chatbot__tenant=self.request.user.tenant,
            ).select_related("chatbot").first()
            if rule:
                context["chatbot"] = rule.chatbot
        return context

    def perform_create(self, serializer):
        rule = serializer.save()
        self.audit("chatbot.rule_created", rule.chatbot, rule=rule.name, priority=rule.priority)

    def perform_update(self, serializer):
        rule = serializer.save()
        self.audit("chatbot.rule_updated", rule.chatbot, rule=rule.name)

    def perform_destroy(self, instance):
        chatbot, name = instance.chatbot, instance.name
        instance.delete()
        log_event(
            request=self.request, action="chatbot.rule_deleted", tenant=self.request.user.tenant,
            object_type="chatbot", object_id=chatbot.pk, metadata={"rule": name},
        )

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        """Set several rules' priorities in one call, so a drag-and-drop list saves once."""
        order = request.data.get("order") or []
        if not isinstance(order, list):
            return Response({"detail": "Send an 'order' list."}, status=status.HTTP_400_BAD_REQUEST)

        rules = {rule.pk: rule for rule in self.get_queryset().filter(pk__in=[row.get("id") for row in order])}
        updated = []
        for row in order:
            rule = rules.get(row.get("id"))
            if rule is None:
                continue
            rule.priority = int(row.get("priority", rule.priority))
            updated.append(rule)
        if updated:
            ChatbotRule.objects.bulk_update(updated, ["priority"])
        return Response({"updated": len(updated)})


class ChatbotMediaAssetViewSet(TenantScopedViewSet):
    serializer_class = ChatbotMediaAssetSerializer
    required_permission = Permission.CHATBOT_VIEW
    action_permissions = {
        "create": [Permission.CHATBOT_EDIT],
        "update": [Permission.CHATBOT_EDIT],
        "partial_update": [Permission.CHATBOT_EDIT],
        "destroy": [Permission.CHATBOT_EDIT],
    }
    filterset_fields = {"chatbot": ["exact"]}

    def base_queryset(self):
        return ChatbotMediaAsset.objects.select_related("chatbot")

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class ChatbotConversationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The chatbot inbox: conversations, their bot state, and the controls on them.

    Read-only as a viewset because a conversation is never *created* through
    this API -- it exists because a customer wrote in. Everything that changes
    one is an explicit action below, which is what keeps "take over" auditable
    rather than a PATCH somebody could send by accident.
    """

    permission_classes = [IsAuthenticated, HasChatbotPermission]
    serializer_class = ChatbotSessionSerializer
    required_permission = Permission.CONVERSATION_VIEW
    action_permissions = {
        "reply": [Permission.CONVERSATION_REPLY],
        "takeover": [Permission.CONVERSATION_TAKEOVER],
        "resume_bot": [Permission.CONVERSATION_TAKEOVER],
        "assign": [Permission.CONVERSATION_ASSIGN],
        "close": [Permission.CONVERSATION_REPLY],
        "reopen": [Permission.CONVERSATION_REPLY],
        "resolve": [Permission.CONVERSATION_REPLY],
        "mark_unread": [Permission.CONVERSATION_REPLY],
        "trace": [Permission.CHATBOT_ANALYTICS],
    }
    filterset_fields = {
        "state": ["exact", "in"],
        "chatbot": ["exact"],
        "resolution": ["exact"],
    }
    search_fields = ["conversation__contact__name", "conversation__contact__phone"]

    def get_queryset(self):
        latest = Message.objects.filter(
            conversation=OuterRef("conversation_id"),
        ).order_by("-queued_at", "-id")

        queryset = (
            ChatbotSession.objects
            .filter(tenant=self.request.user.tenant)
            .select_related(
                "conversation", "conversation__contact", "conversation__assigned_agent",
                "conversation__whatsapp_account", "chatbot",
            )
            .annotate(
                last_message_preview=Subquery(latest.values("body")[:1]),
            )
            .order_by("-updated_at")
        )
        return self._apply_filters(queryset)

    def _apply_filters(self, queryset):
        params = self.request.query_params

        if params.get("unread") in ("1", "true", "True"):
            queryset = queryset.filter(conversation__unread_count__gt=0)
        if params.get("account"):
            queryset = queryset.filter(conversation__whatsapp_account_id=params["account"])
        if params.get("agent"):
            queryset = queryset.filter(conversation__assigned_agent_id=params["agent"])

        bucket = params.get("bucket")
        if bucket == "bot":
            queryset = queryset.filter(state__in=[
                ChatbotSession.State.NEW, ChatbotSession.State.BOT_ACTIVE,
                ChatbotSession.State.WAITING_FOR_CUSTOMER,
            ])
        elif bucket == "human":
            queryset = queryset.filter(state__in=[
                ChatbotSession.State.HUMAN_ACTIVE, ChatbotSession.State.WAITING_FOR_AGENT,
            ])
        elif bucket == "resolved":
            queryset = queryset.filter(state__in=[
                ChatbotSession.State.RESOLVED, ChatbotSession.State.CLOSED,
            ])
        elif bucket == "active":
            queryset = queryset.exclude(state__in=[
                ChatbotSession.State.RESOLVED, ChatbotSession.State.CLOSED,
            ])

        search = params.get("search")
        if search:
            queryset = queryset.filter(
                Q(conversation__contact__name__icontains=search)
                | Q(conversation__contact__phone__icontains=search)
            )
        return queryset

    def _conversation(self, session):
        return session.conversation

    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        """Newest first, so page 1 is the live end of the thread."""
        session = self.get_object()
        queryset = session.conversation.messages.order_by("-queued_at", "-id")
        page = self.paginate_queryset(queryset)
        serializer = MessageSerializer(page if page is not None else queryset, many=True)
        return self.get_paginated_response(serializer.data) if page is not None else Response(serializer.data)

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        """
        An agent types into a chatbot conversation.

        Routed through ``inbox.send_reply``, which is the same call the ordinary
        inbox makes -- so the credit reservation, the compliance gate, the link
        rewriting and the response-time clock all behave identically. Replying
        also takes the conversation off the bot: a person who has just answered
        does not want the bot answering over the top of them.
        """
        from apps.inbox.services import InboxError, send_reply

        session = self.get_object()
        serializer = ReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        services.take_over(session.conversation, request.user)
        try:
            message = send_reply(session.conversation, request.user, **serializer.validated_data)
        except InboxError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def takeover(self, request, pk=None):
        session = services.take_over(self.get_object().conversation, request.user)
        log_event(
            request=request, action="chatbot.human_takeover", tenant=request.user.tenant,
            object_type="conversation", object_id=session.conversation_id,
            metadata={"chatbot": session.chatbot_id},
        )
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"], url_path="resume-bot")
    def resume_bot(self, request, pk=None):
        session = services.resume_bot(self.get_object().conversation, request.user)
        log_event(
            request=request, action="chatbot.bot_resumed", tenant=request.user.tenant,
            object_type="conversation", object_id=session.conversation_id,
            metadata={"chatbot": session.chatbot_id},
        )
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        from apps.inbox.services import InboxError, assign_agent

        session = self.get_object()
        agent = None
        agent_id = request.data.get("agent_id")
        if agent_id:
            from django.contrib.auth import get_user_model

            agent = get_user_model().objects.filter(pk=agent_id, tenant=request.user.tenant).first()
            if agent is None:
                return Response({"detail": "Agent not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            assign_agent(session.conversation, agent)
        except InboxError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        log_event(
            request=request, action="chatbot.conversation_assigned", tenant=request.user.tenant,
            object_type="conversation", object_id=session.conversation_id, metadata={"agent": agent_id},
        )
        session.refresh_from_db()
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        from apps.inbox.services import close

        session = self.get_object()
        close(session.conversation)
        session.refresh_from_db()
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        from apps.inbox.services import reopen

        session = self.get_object()
        reopen(session.conversation)
        ChatbotSession.objects.filter(pk=session.pk).update(
            state=ChatbotSession.State.BOT_ACTIVE, ended_at=None,
            resolution=ChatbotSession.Resolution.UNRESOLVED,
        )
        session.refresh_from_db()
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        session = services.resolve_session(self.get_object(), ChatbotSession.Resolution.HUMAN)
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        from apps.inbox.services import mark_read

        session = self.get_object()
        mark_read(session.conversation)
        session.refresh_from_db()
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["post"], url_path="mark-unread")
    def mark_unread(self, request, pk=None):
        from apps.inbox.models import Conversation

        session = self.get_object()
        Conversation.objects.filter(pk=session.conversation_id, unread_count=0).update(unread_count=1)
        session.refresh_from_db()
        return Response(ChatbotSessionSerializer(session).data)

    @action(detail=True, methods=["get"])
    def trace(self, request, pk=None):
        """
        The debug panel: every step this conversation's messages went through.

        Grouped by correlation id so the screen shows one journey per inbound
        message rather than a flat river of rows. Never carries a credential --
        see ``trace._safe``, which strips anything key-shaped on the way in.
        """
        session = self.get_object()
        limit = min(int(request.query_params.get("limit", 200)), 500)
        events = (
            ChatbotEvent.objects
            .filter(tenant=request.user.tenant, conversation_id=session.conversation_id)
            .order_by("-created_at", "-id")[:limit]
        )

        journeys = {}
        for event in reversed(list(events)):
            journey = journeys.setdefault(event.correlation_id, {
                "correlation_id": event.correlation_id,
                "started_at": event.created_at,
                "total_ms": 0,
                "has_error": False,
                "steps": [],
            })
            journey["steps"].append(ChatbotEventSerializer(event).data)
            journey["total_ms"] += event.duration_ms
            journey["has_error"] = journey["has_error"] or event.outcome == ChatbotEvent.Outcome.ERROR

        return Response({
            "session": ChatbotSessionSerializer(session).data,
            "journeys": sorted(journeys.values(), key=lambda row: row["started_at"], reverse=True),
        })

    @action(detail=True, methods=["get"])
    def handoffs(self, request, pk=None):
        session = self.get_object()
        rows = ChatbotHandoff.objects.filter(
            tenant=request.user.tenant, conversation_id=session.conversation_id,
        ).select_related("assigned_to")
        return Response(ChatbotHandoffSerializer(rows, many=True).data)


class ChatbotOverviewView(APIView):
    """The dashboard's headline figures, for every bot in the workspace."""

    permission_classes = [IsAuthenticated, HasChatbotPermission]
    required_permission = Permission.CHATBOT_ANALYTICS

    def get(self, request):
        tenant_id = request.user.tenant_id
        days = request.query_params.get("days")
        return Response({
            **analytics.overview(tenant_id, days=days),
            "bots": analytics.bot_summaries(tenant_id, days=days or 30),
            "handoffs": analytics.handoff_breakdown(tenant_id, days=days or 30),
            "top_questions": analytics.top_questions(tenant_id, days=days or 30),
            "top_intents": analytics.top_intents(tenant_id, days=days or 30),
        })


class ChatbotCapabilitiesView(APIView):
    """
    What each of this workspace's numbers can actually deliver.

    Read by the composer so a flow made of buttons is labelled before it is
    built on a number that cannot send them, rather than after a thousand
    messages nobody could tap.
    """

    permission_classes = [IsAuthenticated, HasChatbotPermission]
    required_permission = Permission.CHATBOT_VIEW

    def get(self, request):
        from apps.whatsapp.models import WhatsAppAccount
        from apps.whatsapp.providers.registry import provider_capabilities

        accounts = WhatsAppAccount.objects.filter(tenant=request.user.tenant).values(
            "id", "name", "phone_number", "status", "provider",
        )
        by_provider = provider_capabilities()
        return Response({
            "accounts": [
                {**account, "capabilities": by_provider.get(account["provider"], {})}
                for account in accounts
            ],
            "providers": by_provider,
            "permissions": sorted(permissions_for(request.user)),
        })
