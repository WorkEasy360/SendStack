from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ActivityViewSet,
    ContactContextView,
    ContactNoteViewSet,
    ContactTimelineView,
    DealViewSet,
    PipelineStageViewSet,
    PipelineViewSet,
    SmartSegmentsView,
    TaskViewSet,
    TeamOptionsView,
    TodayView,
)

router = DefaultRouter()
router.register("pipelines", PipelineViewSet, basename="pipeline")
router.register("stages", PipelineStageViewSet, basename="pipeline-stage")
router.register("deals", DealViewSet, basename="deal")
router.register("tasks", TaskViewSet, basename="crm-task")
router.register("notes", ContactNoteViewSet, basename="contact-note")
router.register("activities", ActivityViewSet, basename="crm-activity")

urlpatterns = [
    path("today/", TodayView.as_view(), name="crm-today"),
    path("segments/", SmartSegmentsView.as_view(), name="crm-smart-segments"),
    path("team/", TeamOptionsView.as_view(), name="crm-team-options"),
    path("contacts/<int:contact_id>/timeline/", ContactTimelineView.as_view(), name="crm-contact-timeline"),
    path("contacts/<int:contact_id>/context/", ContactContextView.as_view(), name="crm-contact-context"),
    *router.urls,
]
