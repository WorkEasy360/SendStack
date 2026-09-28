"""
The public registration endpoint.

Kept in its own module rather than added to `views.py`, because everything in
this file is unauthenticated and reachable by anyone on the internet, and that
is worth being able to see at a glance rather than having to find among four
hundred lines of tenant-scoped administration.

Three layers of abuse protection, and they stop different things:

* **The scoped throttle** counts requests per client address. It stops one
  script making a thousand accounts.
* **The per-address lockout** counts attempts against the *email* being
  registered, which an attacker rotating IPs cannot walk around. It stops the
  membership-oracle attack: hammering one address to see which response comes
  back.
* **The transaction in `registration.register`** stops two simultaneous
  requests both creating an account on one address.

None of them is sufficient alone, and the second is the one people forget.
"""

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit.services import log_event
from apps.onboarding import services as onboarding
from apps.onboarding.models import FunnelEvent
from apps.subscriptions import trials
from core import lockout

from . import registration
from .serializers import CurrentUserSerializer, SendStackTokenSerializer, SignupSerializer

logger = logging.getLogger("sendstack")

#: Its own throttle scope, not the login one. Signing up and signing in have
#: different legitimate rates -- a person signs up once and may mistype a
#: password five times -- and sharing a budget would let a burst of signups
#: lock real customers out of logging in.
SIGNUP_THROTTLE_SCOPE = "signup"

#: Per-email counter, the half a rotating-IP attacker cannot escape.
SIGNUP_LOCKOUT_SCOPE = "signup"


class SignupView(APIView):
    """
    Create a workspace, its owner, and its trial.

    Answers with the same token pair a login does, so the customer lands in
    the product rather than on a "now go and sign in" page. That is a
    deliberate conversion decision and it is safe: the account was created by
    this very request, so there is no credential being guessed at and nothing
    for the tokens to reveal that the caller does not already know.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = SIGNUP_THROTTLE_SCOPE

    def get(self, request):
        """
        What the signup form needs to know before it renders.

        Public and deliberately thin: whether signups are open, and how long a
        trial runs, so the form can say "14 days" without hardcoding 14. No
        plan internals, no counts, nothing about who already has an account.
        """
        return Response({
            "signup_enabled": registration.signup_enabled(),
            "trials_enabled": trials.trials_enabled(),
            "trial_days": trials.trial_days() if trials.trials_enabled() else 0,
        })

    def post(self, request):
        if not registration.signup_enabled():
            # 403 and a plain sentence. Not a 404: pretending the endpoint
            # does not exist would leave a customer of a deployment that has
            # signups switched off staring at a broken form.
            return Response(
                {
                    "detail": "Self-service signup is not available here. Please contact us to open an account.",
                    "code": "SIGNUP_DISABLED",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data["email"]

        if lockout.is_locked(SIGNUP_LOCKOUT_SCOPE, email):
            # Same shape as the login lockout, and equally uninformative about
            # whether the address exists.
            return Response(
                {"detail": "Too many attempts. Try again later."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        onboarding.record(FunnelEvent.Name.SIGNUP_STARTED)

        try:
            user, tenant, subscription, state = registration.register(
                email=email,
                password=data["password"],
                full_name=data["full_name"],
                company=data["company"],
                plan_name=data.get("plan", ""),
                accepted_terms=data["accept_terms"],
                marketing_opt_in=data.get("marketing_opt_in", False),
                request=request,
            )
        except registration.RegistrationDisabled as exc:
            return Response({"detail": str(exc), "code": "SIGNUP_DISABLED"}, status=status.HTTP_403_FORBIDDEN)
        except registration.RegistrationRefused as exc:
            # Counted against the address, whatever the reason. Counting only
            # the "already registered" case would turn the counter itself into
            # the oracle the generic message exists to close.
            lockout.record_failure(SIGNUP_LOCKOUT_SCOPE, email)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        log_event(
            request=request, action="account.signed_up", tenant=tenant,
            object_type="accounts.User", object_id=user.pk,
            metadata={
                "workspace": tenant.name,
                "plan": subscription.plan.name if subscription else None,
                "trial": bool(subscription),
            },
        )

        return Response(self._session(user, subscription, state), status=status.HTTP_201_CREATED)

    @staticmethod
    def _session(user, subscription, state) -> dict:
        """
        The signed-in session, plus just enough to route the browser.

        Built through the same serializer login uses, so a token minted here
        carries exactly the claims a token minted there does -- there is no
        second, subtly different token shape to keep in step.
        """
        refresh = SendStackTokenSerializer.get_token(user)
        payload = {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": CurrentUserSerializer(user).data,
            # Where to send them. The server decides, because the server is
            # the only thing that knows whether a trial actually started.
            "next": "/onboarding" if state.is_guided else "/",
        }
        if subscription is None:
            payload["subscription"] = None
            payload["notice"] = (
                "Your workspace is ready. Choose a plan to start sending."
            )
        else:
            payload["subscription"] = {
                "status": subscription.status,
                "plan": subscription.plan.name,
                "trial_ends_at": subscription.trial_ends_at,
                "trial_days_remaining": subscription.trial_days_remaining(),
            }
        return payload
