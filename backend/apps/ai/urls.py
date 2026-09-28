from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AIAgentConfigView,
    AIStatusView,
    AIUsageViewSet,
    ClearAPIKeyView,
    KnowledgeAnswerViewSet,
    KnowledgeAskView,
    KnowledgeBaseViewSet,
    KnowledgeDocumentViewSet,
    KnowledgeRecommendationView,
    SuggestReplyView,
    SummariseView,
)

router = DefaultRouter()
router.register("knowledge-bases", KnowledgeBaseViewSet, basename="knowledge-base")
router.register("knowledge", KnowledgeDocumentViewSet, basename="knowledge-document")
router.register("answers", KnowledgeAnswerViewSet, basename="knowledge-answer")
router.register("usage", AIUsageViewSet, basename="ai-usage")

urlpatterns = [
    path("agent/", AIAgentConfigView.as_view(), name="ai-agent-config"),
    path("agent/clear-key/", ClearAPIKeyView.as_view(), name="ai-agent-clear-key"),
    path("status/", AIStatusView.as_view(), name="ai-status"),
    path("ask/", KnowledgeAskView.as_view(), name="knowledge-ask"),
    path("knowledge-recommendations/", KnowledgeRecommendationView.as_view(), name="knowledge-recommendations"),
    path("suggest-reply/", SuggestReplyView.as_view(), name="ai-suggest-reply"),
    path("summarise/", SummariseView.as_view(), name="ai-summarise"),
] + router.urls
