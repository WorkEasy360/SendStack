from rest_framework.routers import DefaultRouter

from .views import ConversationLabelViewSet, ConversationViewSet

router = DefaultRouter()
router.register("conversations", ConversationViewSet, basename="conversation")
router.register("labels", ConversationLabelViewSet, basename="conversation-label")

urlpatterns = router.urls
