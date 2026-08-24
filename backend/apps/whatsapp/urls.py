from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import WhatsAppAccountViewSet
from .webhooks import WhatsAppConnectionWebhookView, WhatsAppWebhookView


router = DefaultRouter()

router.register(
    "accounts",
    WhatsAppAccountViewSet,
    basename="whatsapp-account",
)

urlpatterns = [
    path(
        "webhook/<int:account_id>/",
        WhatsAppWebhookView.as_view(),
        name="whatsapp-webhook",
    ),
    path(
        "webhooks/<str:provider>/",
        WhatsAppConnectionWebhookView.as_view(),
        name="whatsapp-connection-webhook",
    ),
] + router.urls
