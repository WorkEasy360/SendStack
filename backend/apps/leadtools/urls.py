from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import GroupGrabViewSet, NumberCheckViewSet, WhatsAppGroupsView

router = DefaultRouter()
router.register("number-checks", NumberCheckViewSet, basename="number-check")
router.register("group-grabs", GroupGrabViewSet, basename="group-grab")

urlpatterns = [
    path("groups/", WhatsAppGroupsView.as_view(), name="leadtools-groups"),
] + router.urls
