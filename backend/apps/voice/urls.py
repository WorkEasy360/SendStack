from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CallSessionViewSet, ClearCredentialsView, VoiceAgentConfigView, VoiceReadinessView

router = DefaultRouter()
router.register("calls", CallSessionViewSet, basename="call-session")

urlpatterns = [
    path("agent/", VoiceAgentConfigView.as_view(), name="voice-agent-config"),
    path("agent/clear-secret/", ClearCredentialsView.as_view(), name="voice-agent-clear-secret"),
    path("readiness/", VoiceReadinessView.as_view(), name="voice-readiness"),
] + router.urls
