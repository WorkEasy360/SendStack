from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from rest_framework import generics, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import AssignmentPolicy, CannedReply, TeamMember, WorkingHours
from .serializers import (
    AssignmentPolicySerializer,
    CannedReplySerializer,
    TeamMemberSerializer,
    WorkingHoursSerializer,
)


def _members_with_load(tenant_id):
    """Every member of the tenant, annotated with how many open chats they hold."""
    from apps.inbox.models import Conversation

    return TeamMember.objects.filter(tenant_id=tenant_id).select_related("user").annotate(
        open_conversations=Count(
            "user__assigned_conversations",
            filter=Q(user__assigned_conversations__status=Conversation.Status.OPEN),
            distinct=True,
        )
    )


class TeamMemberViewSet(viewsets.ModelViewSet):
    """
    The tenant's agents.

    Profiles are not created by hand: a member exists because a user does, so
    listing materialises one for every user in the tenant rather than asking
    somebody to add their colleagues a second time. Only the working fields
    (availability, capacity, role) can be edited here -- who the user is
    belongs to the accounts app.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = TeamMemberSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        return _members_with_load(self.request.user.tenant_id)

    def list(self, request, *args, **kwargs):
        tenant_id = request.user.tenant_id
        existing = set(TeamMember.objects.filter(tenant_id=tenant_id).values_list("user_id", flat=True))
        missing = get_user_model().objects.filter(tenant_id=tenant_id, is_active=True).exclude(pk__in=existing)
        if missing:
            TeamMember.objects.bulk_create(
                [TeamMember(tenant_id=tenant_id, user=user) for user in missing], ignore_conflicts=True,
            )
        return Response(TeamMemberSerializer(self.get_queryset(), many=True).data)

    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        """The caller's own profile -- how an agent goes on or off shift."""
        services.ensure_member(request.user)
        member = _members_with_load(request.user.tenant_id).get(user=request.user)

        if request.method == "PATCH":
            serializer = TeamMemberSerializer(member, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            member = _members_with_load(request.user.tenant_id).get(user=request.user)

        return Response(TeamMemberSerializer(member).data)


class AssignmentPolicyView(generics.RetrieveUpdateAPIView):
    """One policy per tenant, reachable without knowing its id."""

    permission_classes = [IsAuthenticated]
    serializer_class = AssignmentPolicySerializer

    def get_object(self):
        policy, _ = AssignmentPolicy.objects.get_or_create(tenant=self.request.user.tenant)
        return policy


class WorkingHoursView(APIView):
    """
    The whole week, read and written in one go.

    A week is edited as a unit -- nobody sets Tuesday on its own -- and sending
    it whole means the client never has to reconcile seven separate requests,
    one of which may have failed.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = {row.weekday: row for row in WorkingHours.objects.filter(tenant=request.user.tenant)}
        days = [
            WorkingHoursSerializer(rows[weekday]).data
            if weekday in rows
            else {"id": None, "weekday": weekday, "is_open": False, "opens_at": "09:00:00", "closes_at": "18:00:00"}
            for weekday, _ in WorkingHours.Weekday.choices
        ]
        return Response({"days": days, "open_now": services.is_open_now(request.user.tenant_id)})

    def put(self, request):
        serializer = WorkingHoursSerializer(data=request.data.get("days", []), many=True)
        serializer.is_valid(raise_exception=True)

        weekdays = [row["weekday"] for row in serializer.validated_data]
        if len(weekdays) != len(set(weekdays)):
            return Response({"days": ["Each weekday may appear only once."]}, status=400)

        with transaction.atomic():
            WorkingHours.objects.filter(tenant=request.user.tenant).delete()
            WorkingHours.objects.bulk_create([
                WorkingHours(tenant=request.user.tenant, **row) for row in serializer.validated_data
            ])
        return self.get(request)


class CannedReplyViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = CannedReplySerializer
    search_fields = ["shortcut", "title", "body"]

    def get_queryset(self):
        return CannedReply.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def use(self, request, pk=None):
        """
        Called when an agent actually inserts this reply.

        Separate from the list read because expansion happens in the composer
        the instant they type the shortcut; counting a use at list time would
        credit every reply on the tenant every time the inbox loaded.
        """
        reply = self.get_object()
        CannedReply.objects.filter(pk=reply.pk).update(usage_count=reply.usage_count + 1)
        reply.refresh_from_db()
        return Response(self.get_serializer(reply).data)


class AgentPerformanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "agents": services.agent_performance(
                request.user.tenant_id, start=request.query_params.get("start"), end=request.query_params.get("end"),
            ),
            "open_now": services.is_open_now(request.user.tenant_id),
        })
