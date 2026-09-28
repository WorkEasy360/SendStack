from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.contacts.models import Contact
from apps.subscriptions.services import require_feature
from apps.whatsapp.models import WhatsAppAccount
from core.bulk import BulkActionMixin

from . import presets, recommendations, services
from .models import AutomationRule, FollowUpRule, Workflow, WorkflowRun
from .serializers import (
    AutomationRuleSerializer,
    FollowUpRuleSerializer,
    StartWorkflowSerializer,
    WorkflowRunSerializer,
    WorkflowSerializer,
)
from .services import WorkflowError


class _ActivationBulkMixin(BulkActionMixin):
    """Delete / activate / deactivate, shared by all three automation lists."""

    bulk_actions = {"delete": "_bulk_delete", "activate": "_bulk_activate", "deactivate": "_bulk_deactivate"}

    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)

    def _set_active(self, instance, is_active):
        if instance.is_active != is_active:
            instance.is_active = is_active
            instance.save(update_fields=["is_active", "updated_at"])

    def _bulk_activate(self, request, instance):
        self._set_active(instance, True)

    def _bulk_deactivate(self, request, instance):
        self._set_active(instance, False)


class AutomationRuleViewSet(_ActivationBulkMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = AutomationRuleSerializer
    bulk_object_label = "rule"
    filterset_fields = ["whatsapp_account", "trigger_type", "is_active"]
    search_fields = ["name", "keyword"]

    def get_queryset(self):
        return AutomationRule.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class FollowUpRuleViewSet(_ActivationBulkMixin, viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = FollowUpRuleSerializer
    bulk_object_label = "follow-up"
    filterset_fields = ["whatsapp_account", "is_active"]
    search_fields = ["name"]

    def get_queryset(self):
        return FollowUpRule.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class WorkflowViewSet(_ActivationBulkMixin, viewsets.ModelViewSet):
    """
    The drag-and-drop sequences, behind the `workflows` plan switch.

    Listing, reading and deleting stay open on every plan: a customer whose
    plan no longer includes workflows still owns the ones they built, still
    needs to see why they stopped, and must be able to tidy them up. Creating,
    editing and activating are what the switch closes -- together with
    `services.start_workflow` and `services.resume_workflow`, which close the
    same door from the execution side so a sequence cannot outlive the plan
    that allowed it.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowSerializer
    bulk_object_label = "workflow"
    filterset_fields = ["whatsapp_account", "is_active"]
    search_fields = ["name"]

    def get_queryset(self):
        return Workflow.objects.filter(tenant=self.request.user.tenant)

    def _require_workflows(self):
        require_feature(self.request.user.tenant_id, services.WORKFLOWS_FEATURE)

    def perform_create(self, serializer):
        self._require_workflows()
        serializer.save(tenant=self.request.user.tenant)

    def perform_update(self, serializer):
        # Covers editing the graph and flipping `is_active` through PATCH,
        # which is the same door by a quieter route.
        self._require_workflows()
        serializer.save()

    def _bulk_activate(self, request, instance):
        self._require_workflows()
        super()._bulk_activate(request, instance)

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        workflow = self.get_object()
        serializer = StartWorkflowSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        contact = Contact.objects.filter(pk=serializer.validated_data["contact_id"], tenant=request.user.tenant).first()
        if not contact:
            return Response({"detail": "Contact not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            run = services.start_workflow(workflow, contact, force=serializer.validated_data["force"])
        except WorkflowError as exc:
            # A contact already mid-run is a conflict, not a bad request: the
            # caller sent nothing wrong and retrying with force=true is the
            # documented way through it.
            already_running = services.active_run_for(workflow, contact) is not None
            code = status.HTTP_409_CONFLICT if already_running else status.HTTP_400_BAD_REQUEST
            return Response({"detail": str(exc)}, status=code)

        return Response(WorkflowRunSerializer(run).data, status=status.HTTP_201_CREATED)


class WorkflowRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WorkflowRunSerializer
    filterset_fields = ["workflow", "status"]

    def get_queryset(self):
        return WorkflowRun.objects.filter(workflow__tenant=self.request.user.tenant)


class AutomationPresetView(APIView):
    """
    The simple side of automation: a list of sentences, and a way to say yes.

    See presets.py for why this exists alongside the workflow builder.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"results": presets.catalog()})


class AutomationPresetActivateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, key):
        preset = presets.get(key)
        # The knowledge preset configures the tenant's agent rather than one
        # number's rule, so it is the one preset that needs no account.
        needs_account = preset is None or preset.get("creates") != "KNOWLEDGE"

        account = WhatsAppAccount.objects.filter(
            pk=request.data.get("whatsapp_account"), tenant=request.user.tenant,
        ).first()
        if account is None and needs_account:
            return Response(
                {"detail": "Choose one of your own WhatsApp numbers."}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            instance, kind = presets.activate(key, tenant=request.user.tenant, account=account,
                                              answers=request.data, actor=request.user)
        except presets.PresetError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        log_event(
            request=request, action="automation.preset_activated", tenant=request.user.tenant,
            object_type="automation_preset", object_id=instance.pk, metadata={"preset": key, "kind": kind},
        )
        if kind == "KNOWLEDGE":
            from apps.ai.serializers import AIAgentConfigSerializer

            return Response(
                {"kind": kind, "agent": AIAgentConfigSerializer(instance).data},
                status=status.HTTP_201_CREATED,
            )
        serializer = AutomationRuleSerializer if kind == "RULE" else FollowUpRuleSerializer
        return Response({"kind": kind, "rule": serializer(instance).data}, status=status.HTTP_201_CREATED)


class AutomationRecommendationView(APIView):
    """
    Opportunities found in this tenant's own data. Never acts on them.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"results": recommendations.build(request.user.tenant, user=request.user)})
