from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import CurrentUserView, ImpersonationExitView, ImpersonationStartView, LoginView, ManagedUserViewSet, PasswordResetConfirmView, PasswordResetRequestView

router = DefaultRouter()
router.register("users", ManagedUserViewSet, basename="managed-user")

urlpatterns = [
    path("token/", LoginView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh_v1"),
    path("me/", CurrentUserView.as_view(), name="current_user"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password_reset_request"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("impersonations/", ImpersonationStartView.as_view(), name="impersonation_start"),
    path("impersonations/exit/", ImpersonationExitView.as_view(), name="impersonation_exit"),
    path("", include(router.urls)),
]
