from django.urls import path

from .views import ActivationFunnelView, OnboardingView

urlpatterns = [
    path("", OnboardingView.as_view(), name="onboarding-state"),
    path("funnel/", ActivationFunnelView.as_view(), name="onboarding-funnel"),
]
