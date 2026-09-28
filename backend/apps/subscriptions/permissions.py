"""
Entitlement as a DRF permission, for endpoints whose *whole* purpose is one
plan feature.

Most enforcement in this codebase is not here: a feature is usually one action
on a viewset that also does three unrelated things, or it is reached from a
Celery task that has no request at all, and a permission class can protect
neither. Those call `apps.subscriptions.services.require_feature` directly.
This exists for the case where an entire route is the feature.

::

    class SomeCustomerFacingAPI(APIView):
        permission_classes = [IsAuthenticated, RequiresFeature.for_feature("api_access")]
"""

from rest_framework.permissions import BasePermission

from .services import feature_enabled, feature_message


class RequiresFeature(BasePermission):
    """Refuses unless the caller's plan includes `feature`."""

    feature = ""

    @classmethod
    def for_feature(cls, feature: str):
        return type(
            f"Requires{feature.title().replace('_', '')}",
            (cls,),
            {"feature": feature},
        )

    def has_permission(self, request, view):
        feature = getattr(view, "required_feature", None) or self.feature
        if not feature:
            return True
        tenant_id = getattr(request.user, "tenant_id", None)
        if not feature_enabled(tenant_id, feature):
            # Written onto the instance so DRF's 403 body carries the sentence
            # the customer needs rather than "You do not have permission".
            self.message = feature_message(tenant_id, feature)
            return False
        return True


#: The public REST API a customer would drive programmatically does not exist
#: yet -- Phase 2 established the boundary, and building the API itself is
#: deferred (see docs/ENTITLEMENTS.md). When it lands, its router mounts
#: behind this and nothing else needs deciding.
RequiresApiAccess = RequiresFeature.for_feature("api_access")
