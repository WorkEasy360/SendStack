from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import CompliancePolicyView, ConsentEventViewSet, KeywordPreviewView

router = DefaultRouter()
router.register("consent-events", ConsentEventViewSet, basename="consent-event")

urlpatterns = [
    path("policy/", CompliancePolicyView.as_view(), name="compliance-policy"),
    path("keyword-preview/", KeywordPreviewView.as_view(), name="compliance-keyword-preview"),
] + router.urls
