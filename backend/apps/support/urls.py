"""
Support routes.

Two disjoint prefixes rather than one with a role check inside: `requests/` is
a customer's own, `triage/` is every workspace's. Keeping them apart in the URL
map means a reader can see at a glance which endpoints cross a tenant boundary.
"""

from rest_framework.routers import DefaultRouter

from .views import SupportRequestViewSet, SupportTriageViewSet

router = DefaultRouter()
router.register("requests", SupportRequestViewSet, basename="support-request")
router.register("triage", SupportTriageViewSet, basename="support-triage")

urlpatterns = router.urls
