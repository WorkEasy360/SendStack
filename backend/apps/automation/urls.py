from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AutomationPresetActivateView,
    AutomationPresetView,
    AutomationRecommendationView,
    AutomationRuleViewSet,
    FollowUpRuleViewSet,
    WorkflowRunViewSet,
    WorkflowViewSet,
)

router = DefaultRouter()
router.register("rules", AutomationRuleViewSet, basename="automation-rule")
router.register("follow-ups", FollowUpRuleViewSet, basename="follow-up-rule")
router.register("workflows", WorkflowViewSet, basename="workflow")
router.register("workflow-runs", WorkflowRunViewSet, basename="workflow-run")

urlpatterns = [
    path("presets/", AutomationPresetView.as_view(), name="automation-presets"),
    path("presets/<str:key>/activate/", AutomationPresetActivateView.as_view(), name="automation-preset-activate"),
    path("recommendations/", AutomationRecommendationView.as_view(), name="automation-recommendations"),
    *router.urls,
]
