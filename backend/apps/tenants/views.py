from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import BusinessProfile, Tenant, TenantBranding
from .serializers import BusinessProfileSerializer, TenantBrandingSerializer


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
