from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.tenants.models import Tenant

from . import services
from .models import Plan, Subscription
from .serializers import PlanSerializer, SubscriptionSerializer


class IsReseller(IsAuthenticated):
    """Defining and selling plans belongs to whoever has customers."""

    message = "Only a reseller or superadmin can manage plans."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return request.user.role in (request.user.Role.SUPERADMIN, request.user.Role.RESELLER)


class PlanViewSet(viewsets.ModelViewSet):
    """
    The plans this account sells.

    Scoped to the caller's own tenant so one reseller can never read, edit or
    price another's. The platform's own plans (`owner=None`) are visible to a
    superadmin only, which is what keeps a reseller's list theirs alone.
    """

    permission_classes = [IsReseller]
    serializer_class = PlanSerializer
    filterset_fields = {"is_active": ["exact"]}
    search_fields = ["name", "description"]

    def get_queryset(self):
        user = self.request.user
        # Ordered explicitly: an aggregate annotation does not carry
        # Meta.ordering, and an unordered queryset pages inconsistently.
        queryset = Plan.objects.annotate(subscriber_count=Count("subscriptions")).order_by(
            "price_cents", "name"
        )
        if user.role == user.Role.SUPERADMIN:
            return queryset.filter(Q(owner__isnull=True) | Q(owner_id=user.tenant_id))
        return queryset.filter(owner_id=user.tenant_id)

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user.tenant)


class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    Which customer is on which plan.

    A reseller may only subscribe tenants below their own, and only to plans
    they own -- checked here rather than trusted from the payload, because
    both ids arrive from the browser.
    """

    permission_classes = [IsReseller]
    serializer_class = SubscriptionSerializer
    filterset_fields = {"status": ["exact"], "plan": ["exact"]}

    def get_queryset(self):
        user = self.request.user
        queryset = Subscription.objects.select_related("plan", "tenant")
        if user.role == user.Role.SUPERADMIN:
            return queryset
        return queryset.filter(tenant__parent_id=user.tenant_id)

    def _may_manage(self, tenant, plan):
        user = self.request.user
        if user.role == user.Role.SUPERADMIN:
            return True
        if plan.owner_id != user.tenant_id:
            return False
        return tenant.parent_id == user.tenant_id

    def perform_create(self, serializer):
        tenant = serializer.validated_data["tenant"]
        plan = serializer.validated_data["plan"]
        if not self._may_manage(tenant, plan):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You cannot put that workspace on that plan.")
        subscription = serializer.save()
        log_event(
            request=self.request, action="subscription.plan_assigned", tenant=tenant,
            object_type="subscription", object_id=subscription.pk,
            metadata={"plan": plan.name, "status": subscription.status},
        )

    def perform_update(self, serializer):
        # A plan change is the entitlement event most worth having a record
        # of, because it is the one that explains why something a customer
        # could do last week is refused this week. Never destructive: moving
        # a tenant onto a smaller plan changes what they may *add*, and
        # deletes nothing they already have -- see docs/ENTITLEMENTS.md.
        before = self.get_object()
        previous_plan = before.plan.name
        tenant = serializer.validated_data.get("tenant", before.tenant)
        plan = serializer.validated_data.get("plan", before.plan)
        if not self._may_manage(tenant, plan):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You cannot put that workspace on that plan.")
        subscription = serializer.save()
        if subscription.plan.name != previous_plan:
            log_event(
                request=self.request, action="subscription.plan_changed", tenant=subscription.tenant,
                object_type="subscription", object_id=subscription.pk,
                metadata={"from": previous_plan, "to": subscription.plan.name},
            )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """
        End the subscription at the end of the period already paid for.

        Not immediately: `current_period_end` is what the customer bought, and
        `Subscription.access_state` keeps granting the plan's entitlements
        until that date passes. Cancelling therefore takes nothing away today,
        which is the behaviour a customer expects and the only one that does
        not make "cancel" a destructive button.
        """
        subscription = self.get_object()
        # Read while the subscription is still live, so this is the end of the
        # window they are currently *in* rather than one interval past an
        # anchor that may be months old. A subscription written before the
        # period was enforced has no end date at all, and cancelling must not
        # be the thing that invents one in the past.
        _, period_end = subscription.current_period()
        subscription.status = Subscription.Status.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.current_period_end = period_end
        subscription.save(update_fields=[
            "status", "cancelled_at", "current_period_end", "updated_at",
        ])
        log_event(
            request=request, action="subscription.cancelled", tenant=subscription.tenant,
            object_type="subscription", object_id=subscription.pk,
            metadata={"plan": subscription.plan.name, "until": subscription.current_period_end.isoformat()},
        )
        return Response(self.get_serializer(subscription).data)

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        """
        Start a fresh period, which is what resets the monthly allowance.

        Explicit rather than automatic: nothing here talks to a payment
        processor, so the moment a customer's month starts again is a decision
        their reseller makes. The *usage window* does move on its own -- see
        `Subscription.current_period` -- so a customer who is never renewed by
        hand still gets their monthly allowance back each month; what this
        does is re-anchor the period and clear a cancellation or an arrears.
        """
        subscription = self.get_object()
        previous = subscription.status
        subscription.renew()
        log_event(
            request=request, action="subscription.renewed", tenant=subscription.tenant,
            object_type="subscription", object_id=subscription.pk,
            metadata={"plan": subscription.plan.name, "previous_status": previous},
        )
        return Response(self.get_serializer(subscription).data)


class MyUsageView(APIView):
    """This workspace's plan, limits and what it has used."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(services.quota_state(request.user.tenant_id))


class TenantUsageView(APIView):
    """
    The same, for one customer, read by their reseller.

    Refuses any tenant that is not below the caller so a reseller cannot walk
    the id space and read a competitor's numbers.
    """

    permission_classes = [IsReseller]

    def get(self, request, tenant_id):
        tenant = Tenant.objects.filter(pk=tenant_id).first()
        if tenant is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if user.role != user.Role.SUPERADMIN and tenant.parent_id != user.tenant_id:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(services.quota_state(tenant_id))
