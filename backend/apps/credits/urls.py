from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AssignCreditsView, CreditTransactionViewSet, DeductCreditsView, MyCreditBalanceView

router = DefaultRouter()
router.register("transactions", CreditTransactionViewSet, basename="credit-transaction")

urlpatterns = [
    path("balance/", MyCreditBalanceView.as_view(), name="credit-balance"),
    path("assign/", AssignCreditsView.as_view(), name="credit-assign"),
    path("deduct/", DeductCreditsView.as_view(), name="credit-deduct"),
] + router.urls
