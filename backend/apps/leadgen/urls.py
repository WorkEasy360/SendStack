from rest_framework.routers import DefaultRouter

from .views import ClickToChatLinkViewSet, LeadEventViewSet

router = DefaultRouter()
router.register("links", ClickToChatLinkViewSet, basename="click-to-chat-link")
router.register("leads", LeadEventViewSet, basename="lead-event")

urlpatterns = router.urls
