from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import ImpersonationSession


class MultiSenderJWTAuthentication(JWTAuthentication):
    """Reject tokens from ended, expired, or mismatched impersonation sessions."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        session_id = validated_token.get("impersonation_session_id")
        if not session_id:
            return user
        session = ImpersonationSession.objects.filter(
            pk=session_id,
            impersonated_user_id=user.pk,
            original_user_id=validated_token.get("original_user_id"),
            ended_at__isnull=True,
            expires_at__gt=__import__("django.utils.timezone", fromlist=["now"]).now(),
        ).first()
        if not session:
            raise AuthenticationFailed("This impersonation session is no longer active.")
        return user
