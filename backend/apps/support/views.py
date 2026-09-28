"""
The support endpoints: raising a request, and answering one.

Two audiences, kept apart by two views rather than by branching inside one.
`SupportRequestViewSet` is scoped to the caller's own workspace and cannot see
another; `SupportTriageViewSet` is platform staff and sees every workspace,
which is exactly the kind of view that has to be obviously separate from the
customer's rather than the same code with a different queryset depending on a
role check three functions down.
"""

import logging

from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.audit.services import log_event

from . import services
from .models import SupportRequest
from .serializers import (
    SupportRequestAdminSerializer,
    SupportRequestCreateSerializer,
    SupportRequestSerializer,
)

logger = logging.getLogger("sendstack")


class IsPlatformStaff(BasePermission):
    """
    Whoever runs the platform, and nobody else.

    A reseller is deliberately **not** included. A reseller's customers are
    theirs commercially, but a support request can name a campaign, a device
    state and a failure pattern, and "my reseller can read every support ticket
    I file about them" is not a property anybody would choose. If per-reseller
    triage is wanted later it needs its own scoping, not an extra role here.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.role == user.Role.SUPERADMIN)


class SupportRequestViewSet(
    mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet,
):
    """
    A customer's own support requests.

    There is no update and no delete. A support request is a record of somebody
    asking for help at a particular moment, and letting it be edited afterwards
    would mean the snapshot beside it no longer describes the question it was
    taken for. A customer with more to say raises another one.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "support"

    def get_queryset(self):
        # Tenant-scoped by construction. There is no code path here that
        # reaches a request by id alone.
        return SupportRequest.objects.filter(tenant=self.request.user.tenant)

    def get_serializer_class(self):
        if self.action == "create":
            return SupportRequestCreateSerializer
        return SupportRequestSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        campaign_id = serializer.validated_data.get("campaign_id")
        support_request = SupportRequest.objects.create(
            tenant=request.user.tenant,
            created_by=request.user,
            category=serializer.validated_data["category"],
            subject=serializer.validated_data["subject"],
            message=serializer.validated_data["message"],
            campaign_id=campaign_id,
            # The id of this very request, so the log lines and queue jobs it
            # produced can be found from the ticket. See core.middleware.
            correlation_id=getattr(request, "request_id", "") or "",
            # Captured now rather than read when somebody opens the ticket: by
            # then the device has reconnected and the campaign has finished,
            # and the state that explains the problem is gone.
            context=services.capture_context(request.user.tenant, campaign_id=campaign_id),
        )

        log_event(
            request=request, action="support.request_created", tenant=request.user.tenant,
            object_type="support.SupportRequest", object_id=support_request.pk,
            metadata={"reference": support_request.reference, "category": support_request.category},
        )
        logger.info(
            "Support request %s raised by tenant %s (%s).",
            support_request.reference, request.user.tenant_id, support_request.category,
        )
        return Response(
            SupportRequestSerializer(support_request).data, status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="categories")
    def categories(self, request):
        """
        The categories a request may be filed under, from the model.

        Served rather than hardcoded in the browser so a new category appears
        in the form without a frontend deploy, and so the values the UI submits
        are always ones the model accepts.
        """
        return Response({
            "categories": [
                {"value": value, "label": label} for value, label in SupportRequest.Category.choices
            ],
        })


class SupportTriageViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet,
):
    """
    Every support request across the deployment, for whoever answers them.

    Read-mostly. The only writes are the status, the priority and an internal
    note -- the customer's own words and the captured context are read-only on
    the serializer, because a support view that can rewrite what a customer
    said is not a support view.
    """

    permission_classes = [IsPlatformStaff]
    serializer_class = SupportRequestAdminSerializer

    def get_queryset(self):
        queryset = SupportRequest.objects.select_related("tenant", "created_by")

        state = (self.request.query_params.get("state") or "").lower()
        if state == "open":
            queryset = queryset.filter(status__in=(SupportRequest.Status.OPEN, SupportRequest.Status.IN_PROGRESS))
        elif state == "closed":
            queryset = queryset.filter(status__in=(SupportRequest.Status.RESOLVED, SupportRequest.Status.CLOSED))

        category = (self.request.query_params.get("category") or "").upper()
        if category in SupportRequest.Category.values:
            queryset = queryset.filter(category=category)

        reference = (self.request.query_params.get("reference") or "").strip().upper()
        if reference:
            queryset = queryset.filter(reference=reference)

        tenant_id = self.request.query_params.get("tenant")
        if tenant_id and str(tenant_id).isdigit():
            queryset = queryset.filter(tenant_id=int(tenant_id))

        return queryset

    def perform_update(self, serializer):
        before = serializer.instance.status
        support_request = serializer.save()

        if support_request.status == SupportRequest.Status.RESOLVED and support_request.resolved_at is None:
            support_request.resolved_at = timezone.now()
            support_request.save(update_fields=["resolved_at", "updated_at"])

        # Every administrative change to somebody else's record is audited,
        # including the ones that look harmless. "Who closed this and when" is
        # the first question asked when a customer says nobody replied.
        log_event(
            request=self.request, action="support.request_updated", tenant=support_request.tenant,
            object_type="support.SupportRequest", object_id=support_request.pk,
            metadata={
                "reference": support_request.reference,
                "from": before,
                "to": support_request.status,
                "priority": support_request.priority,
            },
        )
