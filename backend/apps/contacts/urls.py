from rest_framework.routers import DefaultRouter

from .views import (
    ContactGroupViewSet,
    ContactImportViewSet,
    ContactSegmentViewSet,
    ContactTagViewSet,
    ContactViewSet,
)

router = DefaultRouter()
router.register("contacts", ContactViewSet, basename="contact")
router.register("groups", ContactGroupViewSet, basename="contact-group")
router.register("tags", ContactTagViewSet, basename="contact-tag")
router.register("imports", ContactImportViewSet, basename="contact-import")
router.register("segments", ContactSegmentViewSet, basename="contact-segment")

urlpatterns = router.urls
