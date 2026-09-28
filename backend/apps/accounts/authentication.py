from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import ImpersonationSession


class SendStackJWTAuthentication(JWTAuthentication):
    """
    The two things a signed token cannot tell us on its own.

    A JWT is valid because it is signed, and it stays valid for its whole
    lifetime whatever happens afterwards. Two changes have to take effect
    before that lifetime is up:

    * an impersonation session that was ended or expired, and
    * a workspace that was suspended.

    Both are checked here rather than in each view, because a check a view has
    to remember is a check the next view forgets.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)

        # A suspended workspace is a deliberate act by an operator, usually for
        # abuse or non-payment. Left to the token's own expiry, the customer
        # would keep full access for up to fifteen minutes and -- because the
        # refresh endpoint is a different view that never loads a user -- could
        # keep renewing indefinitely. So the door closes on the next request.
        #
        # Deliberately not `user.is_active`: that is a *different* decision
        # (this individual account is disabled) and SimpleJWT already refuses
        # those. This is about the whole workspace.
        tenant = user.tenant if user.tenant_id else None
        if tenant is not None and not tenant.is_active:
            raise AuthenticationFailed("This workspace is suspended. Contact support.")

        session_id = validated_token.get("impersonation_session_id")
        if not session_id:
            return user
        session = ImpersonationSession.objects.filter(
            pk=session_id,
            impersonated_user_id=user.pk,
            original_user_id=validated_token.get("original_user_id"),
            ended_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()
        if not session:
            raise AuthenticationFailed("This impersonation session is no longer active.")
        return user
