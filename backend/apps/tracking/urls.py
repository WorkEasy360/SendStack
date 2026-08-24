from rest_framework.routers import DefaultRouter

from .views import TrackedLinkViewSet

router = DefaultRouter()
router.register("links", TrackedLinkViewSet, basename="tracked-link")

urlpatterns = router.urls
