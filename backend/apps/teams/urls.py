from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AgentPerformanceView,
    AssignmentPolicyView,
    CannedReplyViewSet,
    TeamMemberViewSet,
    WorkingHoursView,
)

router = DefaultRouter()
router.register("members", TeamMemberViewSet, basename="team-member")
router.register("canned-replies", CannedReplyViewSet, basename="canned-reply")

urlpatterns = [
    path("assignment-policy/", AssignmentPolicyView.as_view(), name="assignment-policy"),
    path("working-hours/", WorkingHoursView.as_view(), name="working-hours"),
    path("performance/", AgentPerformanceView.as_view(), name="agent-performance"),
] + router.urls
