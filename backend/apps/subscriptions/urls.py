from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import MyUsageView, PlanViewSet, SubscriptionViewSet, TenantUsageView

router = DefaultRouter()
router.register("plans", PlanViewSet, basename="plan")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")

urlpatterns = [
    path("usage/", MyUsageView.as_view(), name="my-usage"),
    path("usage/<int:tenant_id>/", TenantUsageView.as_view(), name="tenant-usage"),
] + router.urls
