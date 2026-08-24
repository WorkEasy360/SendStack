from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils.dateparse import parse_datetime
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from core.bulk import BulkActionError, BulkActionMixin

from . import segments as smart_segments
from . import services
from .models import Activity, ContactNote, Deal, Pipeline, PipelineStage, Task
from .serializers import (
    ActivitySerializer,
    ContactNoteSerializer,
    DealSerializer,
    PipelineSerializer,
    PipelineStageSerializer,
    TaskSerializer,
    TeamOptionSerializer,
    TimelineEntrySerializer,
)


class TenantScopedViewSet(viewsets.ModelViewSet):
    """Same shape as the one in apps.contacts: every read starts tenant-filtered."""

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant)


class PipelineViewSet(TenantScopedViewSet):
    queryset = Pipeline.objects.prefetch_related("stages")
    serializer_class = PipelineSerializer
    search_fields = ["name"]

    def list(self, request, *args, **kwargs):
        # Asking for the pipelines is what creates the first one. A user who
        # opens the board should see the standard stages, not an empty screen
        # with a "create a pipeline" form in front of the thing they wanted.
        if not self.get_queryset().exists():
            services.default_pipeline(request.user.tenant)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        pipeline = serializer.save(tenant=self.request.user.tenant)
        if not pipeline.stages.exists():
            PipelineStage.objects.bulk_create([
                PipelineStage(pipeline=pipeline, name=name, kind=kind, order=index, probability=probability, color=color)
                for index, (name, kind, probability, color) in enumerate(services.DEFAULT_STAGES)
            ])

    def perform_destroy(self, instance):
        if Deal.objects.filter(pipeline=instance).exists():
            raise ValidationError({"detail": "Move or delete this pipeline's deals first."})
        instance.delete()

    @action(detail=True, methods=["get"])
    def board(self, request, pk=None):
        """The whole board in one request: stages, their deals, and totals."""
        pipeline = self.get_object()
        columns = services.pipeline_board(pipeline)
        return Response({
            "pipeline": PipelineSerializer(pipeline).data,
            "columns": [
                {
                    "stage": PipelineStageSerializer(column["stage"]).data,
                    "deals": DealSerializer(column["deals"], many=True).data,
                    "count": column["count"],
                    "value": column["value"],
                }
                for column in columns
            ],
        })


class PipelineStageViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PipelineStageSerializer
    filterset_fields = ["pipeline"]

    def get_queryset(self):
        return (
            PipelineStage.objects.filter(pipeline__tenant=self.request.user.tenant)
            .select_related("pipeline")
            .annotate(deal_count=Count("deals"))
        )

    def perform_create(self, serializer):
        pipeline = serializer.validated_data.get("pipeline")
        if pipeline is None or pipeline.tenant_id != self.request.user.tenant_id:
            raise ValidationError({"pipeline": "Choose one of your own pipelines."})
        serializer.save()

    def perform_destroy(self, instance):
        # A stage still holding deals cannot go: on_delete=PROTECT would raise
        # anyway, but the message a user gets should say what to do about it.
        if instance.deals.exists():
            raise ValidationError({"detail": "Move this stage's deals somewhere else first."})
        if instance.pipeline.stages.count() <= 1:
            raise ValidationError({"detail": "A pipeline needs at least one stage."})
        instance.delete()


class DealViewSet(BulkActionMixin, TenantScopedViewSet):
    queryset = Deal.objects.select_related("contact", "stage", "pipeline", "owner")
    serializer_class = DealSerializer
    filterset_fields = ["status", "stage", "pipeline", "owner", "contact"]
    search_fields = ["name", "contact__name", "contact__phone", "notes"]
    ordering_fields = ["created_at", "updated_at", "value", "expected_close_date"]

    bulk_object_label = "deal"
    bulk_actions = {"delete": "_bulk_delete", "assign": "_bulk_assign"}

    def perform_create(self, serializer):
        tenant = self.request.user.tenant
        pipeline = serializer.validated_data.get("pipeline") or services.default_pipeline(tenant)
        stage = serializer.validated_data.get("stage")
        if stage is None:
            stage = pipeline.stages.filter(kind=PipelineStage.Kind.OPEN).order_by("order", "pk").first()
        if stage is None:
            raise ValidationError({"stage": "This pipeline has no open stage to put a deal in."})
        deal = serializer.save(
            tenant=tenant, pipeline=pipeline, stage=stage,
            created_by=self.request.user,
            owner=serializer.validated_data.get("owner") or serializer.validated_data["contact"].owner,
        )
        # Routed through the same service hook the contact screen uses, so a
        # deal created through the API gets an identical timeline entry,
        # lifecycle promotion and rescore.
        services.record_deal_created(deal, actor=self.request.user)

    def perform_update(self, serializer):
        previous_stage_id = serializer.instance.stage_id
        deal = serializer.save()
        if deal.stage_id != previous_stage_id:
            previous = PipelineStage.objects.filter(pk=previous_stage_id).first()
            services.record_deal_moved(deal, previous, actor=self.request.user)

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        """Move one deal to another stage -- what the board's dropdown calls."""
        deal = self.get_object()
        stage = PipelineStage.objects.filter(
            pk=request.data.get("stage"), pipeline__tenant=request.user.tenant,
        ).first()
        if stage is None:
            return Response({"detail": "Choose one of your own stages."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            services.move_deal(deal, stage, actor=request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        log_event(
            request=request, action="crm.deal_moved", tenant=request.user.tenant,
            object_type="deal", object_id=deal.pk, metadata={"stage": stage.name},
        )
        return Response(DealSerializer(deal).data)

    def _bulk_delete(self, request, instance):
        instance.delete()

    def _bulk_assign(self, request, instance):
        owner = _team_member(request, request.data.get("owner"))
        instance.owner = owner
        instance.save(update_fields=["owner", "updated_at"])


class TaskViewSet(BulkActionMixin, TenantScopedViewSet):
    queryset = Task.objects.select_related("contact", "assigned_to", "deal")
    serializer_class = TaskSerializer
    filterset_fields = ["status", "kind", "assigned_to", "contact", "deal"]
    search_fields = ["title", "notes", "contact__name", "contact__phone"]
    ordering_fields = ["due_at", "created_at", "status"]

    bulk_object_label = "task"
    bulk_actions = {"complete": "_bulk_complete", "delete": "_bulk_delete", "assign": "_bulk_assign"}

    def get_queryset(self):
        queryset = super().get_queryset()
        scope = self.request.query_params.get("scope")
        if scope == "mine":
            queryset = queryset.filter(assigned_to=self.request.user)
        elif scope == "overdue":
            from django.utils import timezone

            queryset = queryset.filter(status=Task.Status.OPEN, due_at__lt=timezone.now())
        elif scope == "today":
            from django.utils import timezone

            end = timezone.now().replace(hour=23, minute=59, second=59, microsecond=0)
            queryset = queryset.filter(status=Task.Status.OPEN, due_at__lte=end)
        return queryset

    def perform_create(self, serializer):
        serializer.save(
            tenant=self.request.user.tenant,
            created_by=self.request.user,
            assigned_to=serializer.validated_data.get("assigned_to") or self.request.user,
        )
        task = serializer.instance
        if task.contact_id:
            services.activity.record(
                task.contact, Activity.Kind.TASK_CREATED, f'Task added: "{task.title}"',
                actor=self.request.user, metadata={"task_id": task.pk, "due_at": task.due_at.isoformat()},
            )


    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        task = services.complete_task(self.get_object(), actor=request.user)
        return Response(TaskSerializer(task).data)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        task = self.get_object()
        task.status = Task.Status.OPEN
        task.completed_at = None
        task.save(update_fields=["status", "completed_at", "updated_at"])
        return Response(TaskSerializer(task).data)

    def _bulk_complete(self, request, instance):
        services.complete_task(instance, actor=request.user)

    def _bulk_delete(self, request, instance):
        instance.delete()

    def _bulk_assign(self, request, instance):
        instance.assigned_to = _team_member(request, request.data.get("assigned_to"))
        instance.save(update_fields=["assigned_to", "updated_at"])


class ContactNoteViewSet(TenantScopedViewSet):
    queryset = ContactNote.objects.select_related("author", "contact")
    serializer_class = ContactNoteSerializer
    filterset_fields = ["contact"]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def perform_create(self, serializer):
        note = serializer.save(tenant=self.request.user.tenant, author=self.request.user)
        services.activity.record(
            note.contact, Activity.Kind.NOTE_ADDED,
            note.body.strip().splitlines()[0][:200] if note.body.strip() else "Note added",
            actor=self.request.user, metadata={"note_id": note.pk},
        )


class ActivityViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ActivitySerializer
    filterset_fields = ["contact", "kind"]

    def get_queryset(self):
        return Activity.objects.filter(tenant=self.request.user.tenant).select_related("actor")


def _team_member(request, user_id):
    """Resolve a user id to somebody on the caller's own team, or None."""
    if not user_id:
        return None
    return get_user_model().objects.filter(pk=user_id, tenant=request.user.tenant).first()


class TodayView(APIView):
    """The dashboard's "what should I do next" panel."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        digest = services.today_digest(request.user.tenant, user=request.user)
        return Response({
            "generated_at": digest["generated_at"],
            "items": digest["items"],
            "tasks": TaskSerializer(digest["tasks"], many=True).data,
        })


class SmartSegmentsView(APIView):
    """Every smart segment with its live size."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        with_counts = request.query_params.get("counts", "1") != "0"
        return Response({"segments": smart_segments.catalog(request.user.tenant, with_counts=with_counts)})


class ContactTimelineView(APIView):
    """The merged CRM + message history for one contact."""

    permission_classes = [IsAuthenticated]

    def get(self, request, contact_id):
        from apps.contacts.models import Contact

        contact = Contact.objects.filter(pk=contact_id, tenant=request.user.tenant).first()
        if contact is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        before = parse_datetime(request.query_params.get("before") or "") or None
        try:
            limit = max(1, min(int(request.query_params.get("limit", 40)), 100))
        except (TypeError, ValueError):
            limit = 40

        entries = services.contact_timeline(contact, limit=limit, before=before)
        return Response({
            "results": TimelineEntrySerializer(entries, many=True).data,
            # The cursor for the next page: the oldest thing on this one.
            "next_before": entries[-1]["at"] if len(entries) == limit else None,
        })


class TeamOptionsView(APIView):
    """Who can be an owner or assignee, for every picker in the CRM."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        users = (
            get_user_model().objects
            .filter(tenant=request.user.tenant, is_active=True)
            .order_by("username")[:200]
        )
        return Response({"results": TeamOptionSerializer(users, many=True).data})
