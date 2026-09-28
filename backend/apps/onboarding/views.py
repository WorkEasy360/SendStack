"""
The onboarding endpoints.

There is deliberately **no endpoint that marks a step complete.** The whole
checklist is derived server-side from what the workspace contains (see
services.refresh), so the only writes a customer can make here are "hide this"
and "show it again" -- preferences about their own screen, which is the one
thing they genuinely own.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.subscriptions import services as billing
from apps.subscriptions import trials

from . import services
from .serializers import OnboardingStateSerializer


class OnboardingView(APIView):
    """
    Where this workspace has got to, and what to do next.

    Read on the dashboard and by the onboarding screen. Refreshing on read is
    what makes onboarding **resumable**: a customer who connects WhatsApp,
    closes the browser and comes back tomorrow finds that step ticked, because
    the tick is computed from the connected number rather than from something
    the previous session was supposed to have saved.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        state = services.refresh(request.user.tenant)
        return Response(self._payload(request, state))

    def post(self, request):
        """Hide or restore the checklist. The only thing a client may change here."""
        action = (request.data.get("action") or "").strip().lower()
        if action == "dismiss":
            state = services.dismiss(request.user.tenant)
        elif action == "restore":
            state = services.state_for(request.user.tenant)
            if state.dismissed_at is not None:
                state.dismissed_at = None
                state.save(update_fields=["dismissed_at", "updated_at"])
            state = services.refresh(request.user.tenant)
        else:
            return Response(
                {"detail": "Unknown action. Use 'dismiss' or 'restore'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(self._payload(request, state))

    def _payload(self, request, state) -> dict:
        data = OnboardingStateSerializer(state).data
        subscription = billing.get_active_subscription(request.user.tenant_id)
        data["trial"] = _trial_block(subscription)
        return data


def _trial_block(subscription) -> dict:
    """
    What the banner needs to say about a trial, and nothing more.

    `days_remaining` comes from the model rather than being computed in the
    browser, so the number on the banner and the number the send gate acts on
    are the same number.
    """
    if subscription is None:
        return {"active": False, "trials_available": trials.trials_enabled()}
    return {
        "active": subscription.is_trial,
        "status": subscription.status,
        "access_state": subscription.access_state(),
        "ends_at": subscription.trial_ends_at,
        "days_remaining": subscription.trial_days_remaining(),
        "trials_available": trials.trials_enabled(),
    }


class ActivationFunnelView(APIView):
    """
    The conversion funnel, for whoever runs the platform.

    Superadmin only, and it is not a customer-facing number: it counts
    workspaces across the whole deployment, which is precisely the thing one
    reseller must never see about another.

    Reports **counts, not rates.** A percentage implies the denominator is
    complete, and it is not: workspaces created before the funnel existed have
    no events, and `signup_started` is only recorded for self-service signups.
    Anybody dividing these numbers should know that, so the endpoint says so
    rather than doing the division and hiding it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        days = self._days(request)
        since = timezone.now() - timedelta(days=days) if days else None
        counts = services.funnel_counts(since=since)

        from .models import FunnelEvent

        ordered = [
            {
                "name": name,
                "label": label,
                "workspaces": counts.get(name, {}).get("workspaces", 0),
                "events": counts.get(name, {}).get("events", 0),
            }
            for name, label in FunnelEvent.Name.choices
        ]
        return Response({
            "window_days": days,
            "stages": ordered,
            "caveat": (
                "Counts are of workspaces that recorded each event. Workspaces created before "
                "funnel tracking existed have no events, so these are not conversion rates."
            ),
        })

    @staticmethod
    def _days(request) -> int:
        try:
            days = int(request.query_params.get("days", 30))
        except (TypeError, ValueError):
            return 30
        return max(min(days, 365), 0)
