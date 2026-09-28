from django.db.models import Avg, Count, Sum
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.bulk import BulkActionError, BulkActionMixin

from apps.ai.providers.registry import CUSTOM as CUSTOM_MODEL_PROVIDER

from . import services
from .models import CallSession, VoiceAgentConfig
from .providers.base import VoiceProviderError
from .serializers import (
    BulkCallSerializer,
    CallSessionDetailSerializer,
    CallSessionSerializer,
    StartCallSerializer,
    VoiceAgentConfigSerializer,
)


class VoiceAgentConfigView(generics.RetrieveUpdateAPIView):
    """One calling agent per tenant, reachable without knowing its id."""

    permission_classes = [IsAuthenticated]
    serializer_class = VoiceAgentConfigSerializer

    def get_object(self):
        config, _ = VoiceAgentConfig.objects.get_or_create(tenant=self.request.user.tenant)
        return config


class ClearCredentialsView(APIView):
    """
    Deleting a secret is its own action, because a blank field means "unchanged".

    Without this there is no way to take a leaked key *out* of the system short
    of typing a fake one over it.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        target = (request.data.get("target") or "").strip()
        config, _ = VoiceAgentConfig.objects.get_or_create(tenant=request.user.tenant)

        if target == "carrier":
            config.set_account_sid(None)
            config.set_auth_token(None)
            # Credentials gone means no call could succeed, so the toggle goes
            # with them rather than being left on and lying.
            config.is_enabled = False
            config.save(update_fields=["account_sid_encrypted", "auth_token_encrypted", "is_enabled", "updated_at"])
        elif target == "api_key":
            config.set_api_key(None)
            config.save(update_fields=["api_key_encrypted", "updated_at"])
        else:
            return Response(
                {"detail": "Choose what to clear: 'carrier' or 'api_key'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(VoiceAgentConfigSerializer(config).data)


class CallSessionViewSet(BulkActionMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Calls: what was placed, what was said, and what came of it."""

    permission_classes = [IsAuthenticated]
    filterset_fields = {"status": ["exact"], "outcome": ["exact"], "direction": ["exact"], "contact": ["exact"]}
    search_fields = ["to_number", "summary", "goal"]

    bulk_actions = {"cancel": "_bulk_cancel"}
    bulk_object_label = "call"

    def get_queryset(self):
        return (
            CallSession.objects.filter(tenant=self.request.user.tenant)
            .select_related("contact", "created_by")
        )

    def get_serializer_class(self):
        return CallSessionDetailSerializer if self.action == "retrieve" else CallSessionSerializer

    def _bulk_cancel(self, request, instance):
        if not instance.is_live:
            raise BulkActionError("This call has already finished.")
        services.hang_up(instance)

    @action(detail=False, methods=["post"])
    def call(self, request):
        """Place one call now."""
        serializer = StartCallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        contact = None
        if data.get("contact"):
            from apps.contacts.models import Contact

            contact = Contact.objects.filter(pk=data["contact"], tenant=request.user.tenant).first()
            if contact is None:
                return Response({"contact": "No such contact."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            session = services.start_call(
                request.user.tenant, data["to_number"],
                goal=data["goal"], context=data["context"], contact=contact, created_by=request.user,
            )
        except services.VoiceNotConfigured as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except VoiceProviderError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CallSessionSerializer(session).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="call-contacts")
    def call_contacts(self, request):
        """
        Dial a list of contacts with one goal.

        Reports per contact rather than failing the batch: a contact with no
        phone number should not stop the other ninety-nine being called, and
        the caller has to be told which one was skipped and why.
        """
        serializer = BulkCallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from apps.contacts.models import Contact

        found = {
            contact.pk: contact
            for contact in Contact.objects.filter(pk__in=data["contacts"], tenant=request.user.tenant)
        }

        succeeded, errors = [], []
        for contact_id in data["contacts"]:
            contact = found.get(contact_id)
            if contact is None:
                errors.append({"id": contact_id, "detail": "Not found."})
                continue
            if not contact.subscribed:
                errors.append({"id": contact_id, "detail": "This contact has opted out."})
                continue
            if not contact.phone:
                errors.append({"id": contact_id, "detail": "No phone number."})
                continue

            try:
                session = services.start_call(
                    request.user.tenant, contact.phone,
                    goal=data["goal"], context=data["context"], contact=contact, created_by=request.user,
                )
            except (services.VoiceNotConfigured, VoiceProviderError) as exc:
                errors.append({"id": contact_id, "detail": str(exc)})
            else:
                succeeded.append(session.pk)

        payload = {
            "requested": len(data["contacts"]),
            "succeeded": len(succeeded),
            "failed": len(errors),
            "succeeded_ids": succeeded,
            "errors": errors,
            "detail": (
                f"{len(succeeded)} call{'' if len(succeeded) == 1 else 's'} queued"
                + (f"; {len(errors)} could not be." if errors else ".")
            ),
        }
        if not succeeded:
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload, status=status.HTTP_207_MULTI_STATUS if errors else status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def hangup(self, request, pk=None):
        session = self.get_object()
        if not session.is_live:
            return Response({"detail": "This call has already finished."}, status=status.HTTP_400_BAD_REQUEST)
        services.hang_up(session)
        session.refresh_from_db()
        return Response(CallSessionSerializer(session).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        queryset = self.get_queryset()
        totals = queryset.aggregate(
            input_tokens=Sum("input_tokens"),
            output_tokens=Sum("output_tokens"),
            talk_time=Sum("duration_seconds"),
            average_turns=Avg("turns_used"),
        )
        return Response({
            "calls": queryset.count(),
            "live": queryset.filter(status__in=[
                CallSession.Status.QUEUED, CallSession.Status.RINGING, CallSession.Status.IN_PROGRESS,
            ]).count(),
            "by_status": dict(
                queryset.values_list("status").annotate(total=Count("id")).values_list("status", "total")
            ),
            "by_outcome": dict(
                queryset.exclude(outcome="").values_list("outcome")
                .annotate(total=Count("id")).values_list("outcome", "total")
            ),
            "talk_time_seconds": totals["talk_time"] or 0,
            "average_turns": round(totals["average_turns"] or 0, 1),
            "input_tokens": totals["input_tokens"] or 0,
            "output_tokens": totals["output_tokens"] or 0,
        })


class VoiceReadinessView(APIView):
    """
    Whether a call placed right now would work, and what is missing if not.

    Its own endpoint because every one of these is a different person's job to
    fix -- the carrier credentials, the model key, the tunnel -- and "calling
    is not working" is not an actionable thing to tell any of them.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        config = VoiceAgentConfig.for_tenant(request.user.tenant_id)
        from . import agent

        checks = {
            "enabled": bool(config.is_enabled),
            "carrier_credentials": bool(config.has_carrier_credentials),
            "model_api_key": bool(agent.api_key(config)),
            "model_base_url": config.model_provider != CUSTOM_MODEL_PROVIDER or bool(config.base_url),
            "public_callback_url": bool(services.public_base_url()),
        }
        problems = {
            "enabled": "The calling agent is switched off.",
            "carrier_credentials": "Add a Twilio Account SID, Auth Token and a number to call from.",
            "model_api_key": (
                f"Add an API key for {config.get_model_provider_display()}, "
                "or set AI_API_KEY for the whole deployment."
            ),
            "model_base_url": "Add the custom endpoint's base URL on the Calling Agent page.",
            "public_callback_url": (
                "Set VOICE_PUBLIC_BASE_URL to a URL Twilio can reach (a tunnel, or your public domain)."
            ),
        }
        return Response({
            "ready": all(checks.values()),
            "checks": checks,
            "blocking": [problems[name] for name, ok in checks.items() if not ok],
            "callback_base_url": services.public_base_url(),
            "calls_today": services.calls_placed_today(request.user.tenant_id),
            "daily_call_limit": config.daily_call_limit,
        })
