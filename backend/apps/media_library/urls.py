from rest_framework.routers import DefaultRouter

from .views import MediaAssetViewSet

router = DefaultRouter()
router.register("assets", MediaAssetViewSet, basename="media-asset")

urlpatterns = router.urls
