from django.urls import path

from .views import HealthView, LivenessView, ReadinessView

urlpatterns = [
    path("", HealthView.as_view(), name="health"),
    path("ready/", ReadinessView.as_view(), name="health_ready"),
    path("live/", LivenessView.as_view(), name="health_live"),
]
