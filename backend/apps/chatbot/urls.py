"""
The chatbot's routes, following the platform's existing conventions.

Mounted at ``/api/v1/chatbot/`` -- a router under the SPA's own prefix, exactly
like every other dashboard app. There is deliberately **no new webhook here**:
inbound WhatsApp traffic already has one endpoint per account
(``/api/v1/whatsapp/webhook/<account_id>/``), it is already signature-verified
and already deduplicated, and a second inbound path would be a second thing to
secure for no benefit.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ChatbotCapabilitiesView,
    ChatbotConversationViewSet,
    ChatbotMediaAssetViewSet,
    ChatbotOverviewView,
    ChatbotRuleViewSet,
    ChatbotViewSet,
)

router = DefaultRouter()
router.register("bots", ChatbotViewSet, basename="chatbot")
router.register("rules", ChatbotRuleViewSet, basename="chatbot-rule")
router.register("assets", ChatbotMediaAssetViewSet, basename="chatbot-asset")
router.register("conversations", ChatbotConversationViewSet, basename="chatbot-conversation")

urlpatterns = [
    path("overview/", ChatbotOverviewView.as_view(), name="chatbot-overview"),
    path("capabilities/", ChatbotCapabilitiesView.as_view(), name="chatbot-capabilities"),
    *router.urls,
]
