import logging

from rest_framework import generics, status
from django.http import JsonResponse
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from apps.audit.services import log_event

from .export import build_export
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import BusinessProfile, Tenant, TenantBranding, TenantSettings
from .serializers import (
    BusinessProfileSerializer,
    TenantBrandingSerializer,
    TenantSettingsSerializer,
)

logger = logging.getLogger("sendstack")


class CanEditBranding(IsAuthenticated):
    """Only the people who own a brand may change it."""

    message = "Only a reseller or superadmin can change branding."

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        return request.user.role in (request.user.Role.SUPERADMIN, request.user.Role.RESELLER)


class TenantBrandingView(generics.RetrieveUpdateAPIView):
    """This tenant's own overrides, plus what they resolve to."""

    permission_classes = [CanEditBranding]
    serializer_class = TenantBrandingSerializer

    def get_object(self):
        branding, _ = TenantBranding.objects.get_or_create(tenant=self.request.user.tenant)
        return branding


class EffectiveBrandingView(APIView):
    """
    The branding the signed-in user's app should wear.

    Split from the endpoint above because every user reads this on every page
    load, including the ones who may not edit it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.tenant_id:
            return Response(TenantBranding.DEFAULTS)
        return Response(TenantBranding.resolve(request.user.tenant))


class PublicBrandingView(APIView):
    """
    Branding for the login page, resolved from the host.

    Unauthenticated by necessity: the whole point is that someone arriving at
    a reseller's domain sees the reseller's brand *before* they have an
    account to look it up with. It exposes only what a login page paints --
    a name, a logo, a colour -- and never says whether a tenant exists,
    falling back to platform defaults for any unknown host.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        host = (request.query_params.get("host") or request.get_host() or "").split(":")[0].lower()

        branding = TenantBranding.objects.filter(custom_domain=host).select_related("tenant").first()
        if branding is None:
            return Response(TenantBranding.DEFAULTS)
        return Response(TenantBranding.resolve(branding.tenant))


class CustomerListView(APIView):
    """
    The workspaces below this account, with what each has used.

    A superadmin sees every tenant; a reseller sees only their own children.
    An ordinary user has no customers and is told so with an empty list rather
    than a 403 -- there is nothing secret about having nobody underneath you.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role == user.Role.SUPERADMIN:
            return Response({"customers": services.customer_summaries(None, include_all=True)})
        if user.role == user.Role.RESELLER and user.tenant_id:
            return Response({"customers": services.customer_summaries(user.tenant_id)})
        return Response({"customers": []})


class BusinessProfileView(generics.RetrieveUpdateAPIView):
    """
    What this business is, as both AI agents read it.

    Any signed-in user of the tenant may read and edit it, unlike branding:
    branding is the reseller's to control, but a business describing itself is
    the business's own job, and gating it behind a role means the person who
    knows the answers cannot type them in.

    Reading it creates the row, which is the one place a write on a read is
    right: the agents themselves use `BusinessProfile.for_tenant`, which never
    writes, so nothing on the message path depends on this having been opened.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = BusinessProfileSerializer

    def get_object(self):
        profile, _ = BusinessProfile.objects.get_or_create(tenant=self.request.user.tenant)
        return profile


class TenantExportView(APIView):
    """
    Download everything this workspace owns, as JSON.

    Scoped to the caller's *own* tenant and to nothing else. There is
    deliberately no `?tenant_id=` on this endpoint: an export is the highest-
    value single response the API can produce, and an endpoint that takes an id
    is an endpoint where one missing permission check hands a competitor a
    contact list. A reseller who needs a customer's export uses impersonation,
    which is audited.

    Admin-only within the workspace, because a whole-workspace export is not an
    ordinary user action -- it is the thing somebody does on their last day.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "export"

    def get(self, request):
        tenant = request.user.tenant
        if tenant is None:
            return Response({"detail": "Your account has no workspace."}, status=400)

        export = build_export(tenant)
        log_event(
            request=request, action="tenant.exported", tenant=tenant,
            object_type="tenants.Tenant", object_id=tenant.pk,
            metadata={"counts": export["manifest"]["counts"]},
        )

        response = JsonResponse(export, json_dumps_params={"indent": 2, "default": str})
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        response["Content-Disposition"] = (
            f'attachment; filename="{tenant.slug}-export-{stamp}.json"'
        )
        return response


class AccountDeletionRequestView(APIView):
    """
    Ask for this workspace to be deleted, see the ask, or take it back.

    **Nothing here deletes anything.** It records the request, starts a
    cooling-off window the customer can see and cancel within, and leaves the
    deletion itself to `manage.py delete_tenant` -- which already refuses a
    workspace with children, refuses the platform tenant, requires the slug
    typed out, and reports what it kept. See the model docstring.

    The confirmation is the **workspace's own name**, typed. Not a checkbox:
    a checkbox is one mis-click, and the thing on the other side of it is a
    business.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import AccountDeletionRequest

        pending = AccountDeletionRequest.objects.filter(
            tenant=request.user.tenant, status=AccountDeletionRequest.Status.REQUESTED,
        ).first()
        return Response({
            "workspace": request.user.tenant.name,
            "cooling_off_days": AccountDeletionRequest.COOLING_OFF_DAYS,
            "pending": None if pending is None else {
                "id": pending.pk,
                "requested_at": pending.requested_at,
                "eligible_at": pending.eligible_at,
                "reason": pending.reason,
            },
            # Said here so the confirmation screen can say it, rather than the
            # customer discovering it afterwards.
            "retained": [
                "Consent records (opt-ins and opt-outs), which are the evidence that "
                "an opt-out was honoured.",
                "Audit events, including this request.",
                "Payment records, where they are required for accounting.",
            ],
        })

    def post(self, request):
        from .models import AccountDeletionRequest

        tenant = request.user.tenant
        if tenant is None:
            return Response({"detail": "Your account has no workspace."}, status=status.HTTP_400_BAD_REQUEST)

        # A customer may only ever ask about their *own* workspace, and the
        # hierarchy above them is not theirs to delete.
        if tenant.kind != Tenant.Kind.CUSTOMER:
            return Response(
                {"detail": "Only a customer workspace can be deleted this way. Please contact us."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        confirmation = str(request.data.get("confirm") or "").strip()
        if confirmation.casefold() != tenant.name.strip().casefold():
            return Response(
                {"detail": f'Type the workspace name exactly ("{tenant.name}") to confirm.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = AccountDeletionRequest.objects.filter(
            tenant=tenant, status=AccountDeletionRequest.Status.REQUESTED,
        ).first()
        if existing is not None:
            return Response(
                {
                    "detail": "A deletion request is already open for this workspace.",
                    "eligible_at": existing.eligible_at,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        deletion_request = AccountDeletionRequest.objects.create(
            tenant=tenant,
            requested_by=request.user,
            reason=str(request.data.get("reason") or "")[:2000],
        )
        log_event(
            request=request, action="tenant.deletion_requested", tenant=tenant,
            object_type="tenants.AccountDeletionRequest", object_id=deletion_request.pk,
            metadata={"eligible_at": deletion_request.eligible_at.isoformat()},
        )
        logger.warning(
            "Workspace %s (id=%s) has requested deletion; eligible from %s.",
            tenant.slug, tenant.pk, deletion_request.eligible_at.isoformat(),
        )
        return Response(
            {
                "detail": (
                    f"We have recorded your request. Nothing is deleted before "
                    f"{deletion_request.eligible_at.date().isoformat()}, and you can cancel it "
                    f"at any time until then."
                ),
                "eligible_at": deletion_request.eligible_at,
            },
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        """Take it back. Always allowed while the request is open."""
        from .models import AccountDeletionRequest

        updated = AccountDeletionRequest.objects.filter(
            tenant=request.user.tenant, status=AccountDeletionRequest.Status.REQUESTED,
        ).update(
            status=AccountDeletionRequest.Status.CANCELLED,
            resolved_at=timezone.now(),
            resolution_note="Cancelled by the customer.",
        )
        if not updated:
            return Response(
                {"detail": "There is no open deletion request to cancel."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        log_event(
            request=request, action="tenant.deletion_cancelled", tenant=request.user.tenant,
            object_type="tenants.AccountDeletionRequest",
        )
        return Response({"detail": "Your deletion request has been cancelled. Nothing was removed."})


class TenantSettingsView(generics.RetrieveUpdateAPIView):
    """
    The workspace's sending and housekeeping preferences.

    A singleton, like the compliance policy, and created on first read so the
    settings screen never has to handle "not created yet" as a separate state.
    Scoped to the caller's own workspace and to nothing else, exactly as
    `CompliancePolicyView` is: these are one account's preferences about its
    own sending, and the tenant is read from the authenticated user rather
    than from anything in the request.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = TenantSettingsSerializer

    def get_object(self):
        settings_row, _ = TenantSettings.objects.get_or_create(tenant=self.request.user.tenant)
        return settings_row

    def perform_update(self, serializer):
        row = serializer.save()
        log_event(
            request=self.request, action="tenant.settings_updated", tenant=row.tenant,
            object_type="tenant_settings", object_id=row.pk,
            metadata={
                "delay_mode": row.delay_mode,
                "batch_size": row.batch_size,
                "sleep_enabled": row.sleep_enabled,
                "campaign_data_retention_days": row.campaign_data_retention_days,
            },
        )


class TenantStorageView(APIView):
    """
    What this workspace is storing, and what a cleanup would remove.

    GET is read-only and safe to poll. POST runs the workspace's own
    campaign-data horizon immediately -- the same function the nightly task
    calls, so "Clean up now" and "clean up automatically" cannot remove
    different things.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .storage import cleanable_summary, usage_for

        payload = usage_for(request.user.tenant)
        # What each of the offered horizons would remove, so the dropdown can
        # say "30 days -- 412 campaigns" instead of asking somebody to pick a
        # number and find out afterwards.
        requested = request.query_params.get("days")
        if requested:
            try:
                payload["preview"] = cleanable_summary(request.user.tenant_id, int(requested))
            except (TypeError, ValueError):
                payload["preview"] = None
        return Response(payload)

    def post(self, request):
        from .storage import prune_campaign_data, usage_for

        user = request.user
        # The horizon is the workspace's stored setting and is deliberately
        # not taken from the request body: a deletion that a caller can widen
        # per request is a deletion nobody has agreed to. Changing what gets
        # removed means changing the setting, which is audited.
        result = prune_campaign_data(user.tenant_id)
        log_event(
            request=request, action="tenant.storage_cleaned", tenant=user.tenant,
            object_type="tenant", object_id=user.tenant_id, metadata=result,
        )
        return Response({
            **result,
            "detail": (
                f"Removed {result['campaigns']:,} finished campaign"
                f"{'' if result['campaigns'] == 1 else 's'} older than {result['days']} days."
                if result["campaigns"]
                else "There was nothing old enough to remove."
            ),
            "usage": usage_for(user.tenant),
        })
