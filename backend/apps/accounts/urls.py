from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    CurrentUserView,
    ImpersonationExitView,
    ImpersonationStartView,
    LoginView,
    LogoutView,
    ManagedUserViewSet,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
)
from .signup_views import SignupView

router = DefaultRouter()
router.register("users", ManagedUserViewSet, basename="managed-user")

urlpatterns = [
    # Public and unauthenticated. GET describes the offer (are signups
    # open, how long is the trial); POST creates the workspace.
    path("signup/", SignupView.as_view(), name="signup"),
    path("token/", LoginView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh_v1"),
    # Blacklists the refresh token it is given, so signing out ends the
    # session on the server rather than only in this browser.
    path("logout/", LogoutView.as_view(), name="logout"),
    path("me/", CurrentUserView.as_view(), name="current_user"),
    path("password-reset/", PasswordResetRequestView.as_view(), name="password_reset_request"),
    path("password-reset/confirm/", PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("password-change/", PasswordChangeView.as_view(), name="password_change"),
    path("impersonations/", ImpersonationStartView.as_view(), name="impersonation_start"),
    path("impersonations/exit/", ImpersonationExitView.as_view(), name="impersonation_exit"),
    path("", include(router.urls)),
]
