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

    Since WorkEasy360 sign-in (docs/workeasy360-sso.md) three more, for the
    same reason:

    * ``WORKEASY360_SSO_ONLY=true`` refuses every password session at once,
      however long its token still has to run;
    * an account that belongs to a WorkEasy360 person is used through a
      WorkEasy360 session only. A SimpleJWT token for it -- left over from
      before it was linked, or minted by an impersonation -- is refused, so
      nothing outside WorkEasy360 can act as that person or survive their
      WorkEasy360 sign-out;
    * a password session in a workspace linked to a WorkEasy360 organization
      is subject to that organization's entitlement, on WorkEasy360's
      schedule (apps.workeasy360.authentication.enforce_entitlement).
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result

        from apps.workeasy360 import services as workeasy360
        from apps.workeasy360.authentication import (
            WorkEasy360AuthenticationFailed,
            enforce_entitlement,
            sign_in_required,
        )

        if workeasy360.sso_only():
            raise sign_in_required()
        if workeasy360.has_workeasy360_identity(user):
            raise WorkEasy360AuthenticationFailed(
                "This account signs in with WorkEasy360.", code="workeasy360_account",
            )
        organization_id = workeasy360.linked_organization_id(user.tenant_id)
        if organization_id is not None:
            if token.get("impersonation_session_id"):
                # Started before the workspace was linked: it ends at the link,
                # because nobody outside the organization acts as its people.
                raise WorkEasy360AuthenticationFailed(
                    "This account's organization signs in with WorkEasy360.", code="workeasy360_account",
                )
            enforce_entitlement(request, organization_id=organization_id, subject=None)
        return user, token

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
