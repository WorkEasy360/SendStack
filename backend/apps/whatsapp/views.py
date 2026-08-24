from django.db.models import Count, ProtectedError, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.audit.services import log_event
from core.bulk import BulkActionMixin

from . import health as health_service
from .limits import policy_for
from .models import AccountHealthEvent, WhatsAppAccount
from .serializers import AccountHealthEventSerializer, SendingPolicySerializer, WhatsAppAccountSerializer
from .services import WhatsAppConnectionService


class DeviceHasHistory(APIException):
    """A device that campaigns or messages still reference cannot be deleted."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This device has send history and cannot be deleted."
    default_code = "device_has_history"


def _plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class WhatsAppAccountViewSet(BulkActionMixin, viewsets.ModelViewSet):

    serializer_class = WhatsAppAccountSerializer
    permission_classes = [IsAuthenticated]
    # Declared here (defaulting to none) so the `connect`/`refresh-qr`
    # actions below can override it via @action(throttle_scope=...) --
    # DRF's ViewSetMixin.as_view() only accepts action kwargs that match an
    # attribute already defined on the class.
    throttle_scope = None

    bulk_object_label = "device"
    bulk_actions = {"delete": "_bulk_delete", "disconnect": "_bulk_disconnect"}

    def get_queryset(self):
        # Annotated rather than counted per row: the devices page lists every
        # account, so a per-instance count would be one extra query each.
        from apps.campaigns.models import Campaign

        from .serializers import _ACTIVE_CAMPAIGN_STATUSES

        return WhatsAppAccount.objects.filter(
            tenant=self.request.user.tenant
        ).annotate(
            active_campaign_count=Count(
                "campaigns",
                filter=Q(campaigns__status__in=_ACTIVE_CAMPAIGN_STATUSES(Campaign)),
                distinct=True,
            )
        ).order_by("-created_at")  # restated: Django drops Meta.ordering on aggregate queries

    def list(self, request, *args, **kwargs):
        # Reconcile abandoned pairing attempts before rendering. Nothing polls
        # an account once the connection wizard closes, so without this sweep a
        # QR that was never scanned leaves the card stuck on "Waiting for scan"
        # forever, with neither Reconnect nor Delete offered. See
        # ConnectionManager.expire_stale for why it never calls the provider.
        WhatsAppConnectionService.expire_stale(
            WhatsAppAccount.objects.filter(tenant=request.user.tenant)
        )
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        account = serializer.save(
            user=self.request.user,
            tenant=self.request.user.tenant,
        )
        log_event(
            request=self.request,
            action="whatsapp_account.created",
            tenant=account.tenant,
            object_type="whatsapp_account",
            object_id=account.pk,
            metadata={"account_type": account.account_type, "connection_method": account.connection_method},
        )

    def perform_destroy(self, instance):
        # Campaign.whatsapp_account and Message.whatsapp_account are both
        # PROTECT -- send history deliberately outlives the device it went
        # out from. That has to be checked *before* anything destructive
        # happens: this used to disconnect the provider and write a
        # "deleted" audit entry first, so a delete the database then refused
        # left the device torn down, still listed, falsely logged as
        # deleted, and the user looking at an unexplained 500.
        campaigns = instance.campaigns.count()
        messages = instance.messages.count()
        if campaigns or messages:
            referenced = " and ".join(
                part for part in (
                    _plural(campaigns, "campaign") if campaigns else "",
                    _plural(messages, "message") if messages else "",
                ) if part
            )
            raise DeviceHasHistory(
                f'"{instance.name}" cannot be deleted: {referenced} still reference it, '
                "and that history is kept for reporting. Disconnect the device instead."
            )

        # Provider teardown (e.g. closes the Baileys socket and frees the QR
        # bridge session) so deleting an account never leaves an orphaned
        # session behind -- same leak class as refresh_qr, see
        # ConnectionManager.refresh_qr. ConnectionManager.disconnect already
        # swallows ProviderError internally (logs + continues), so deletion
        # always proceeds even if the provider is unreachable.
        WhatsAppConnectionService.disconnect(instance)
        log_event(
            request=self.request,
            action="whatsapp_account.deleted",
            tenant=instance.tenant,
            object_type="whatsapp_account",
            object_id=instance.pk,
            metadata={"account_type": instance.account_type, "name": instance.name},
        )
        try:
            instance.delete()
        except ProtectedError as exc:
            # Backstop for any PROTECT relation added after the explicit
            # checks above -- a 409 naming the model beats a bare 500.
            models = sorted({obj._meta.verbose_name for obj in exc.protected_objects})
            raise DeviceHasHistory(
                f'"{instance.name}" cannot be deleted: it is still referenced by {", ".join(models)}.'
            ) from exc

    # Both handlers go through perform_destroy / the same disconnect service
    # the single-device endpoints use, so a device with send history is still
    # refused (409 -> reported per device) rather than deleted in bulk.
    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)

    def _bulk_disconnect(self, request, instance):
        WhatsAppConnectionService.disconnect(instance)
        log_event(
            request=request, action="whatsapp_account.disconnected", tenant=instance.tenant,
            object_type="whatsapp_account", object_id=instance.pk,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="connect",
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="whatsapp_connect",
    )
    def connect(self, request, pk=None):
        account = self.get_object()
        result = WhatsAppConnectionService.start_connection(account)
        log_event(
            request=request,
            action="whatsapp_account.connect_started",
            tenant=account.tenant,
            object_type="whatsapp_account",
            object_id=account.pk,
            metadata={"status": result["status"]},
        )
        if not result["success"]:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "account_id": account.pk,
                "status": result["status"],
                "session_id": result["session_id"],
                "expires_at": result["expires_at"],
                "message": result["message"],
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="connection-status")
    def connection_status(self, request, pk=None):
        account = self.get_object()
        result = WhatsAppConnectionService.get_status(account)
        return Response(
            {
                "account_id": account.pk,
                "status": result["status"],
                "message": result["message"],
                "expires_at": result["expires_at"],
                "phone_number": account.phone_number,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="qr")
    def qr(self, request, pk=None):
        account = self.get_object()
        result = WhatsAppConnectionService.get_qr(account)
        if not result["success"]:
            return Response({"detail": result["message"]}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "account_id": account.pk,
                "qr_data": result["qr_data"],
                "expires_at": result["expires_at"],
                "status": result["status"],
            },
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="refresh-qr",
        throttle_classes=[ScopedRateThrottle],
        throttle_scope="whatsapp_connect",
    )
    def refresh_qr(self, request, pk=None):
        account = self.get_object()
        result = WhatsAppConnectionService.refresh_qr(account)
        log_event(
            request=request,
            action="whatsapp_account.qr_refreshed",
            tenant=account.tenant,
            object_type="whatsapp_account",
            object_id=account.pk,
            metadata={"status": result["status"]},
        )
        if not result["success"]:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "account_id": account.pk,
                "status": result["status"],
                "session_id": result["session_id"],
                "expires_at": result["expires_at"],
                "message": result["message"],
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="disconnect")
    def disconnect(self, request, pk=None):
        account = self.get_object()
        result = WhatsAppConnectionService.disconnect(account)
        log_event(
            request=request,
            action="whatsapp_account.disconnected",
            tenant=account.tenant,
            object_type="whatsapp_account",
            object_id=account.pk,
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="test-api")
    def test_api(self, request, pk=None):
        account = self.get_object()
        if account.connection_method != WhatsAppAccount.ConnectionMethod.CLOUD_API:
            return Response(
                {"detail": "Test Connection is only available for WhatsApp API accounts."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = WhatsAppConnectionService.test_credentials(account)
        log_event(
            request=request,
            action="whatsapp_account.api_tested",
            tenant=account.tenant,
            object_type="whatsapp_account",
            object_id=account.pk,
            metadata={"success": result["success"]},
        )
        if not result["success"]:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)
        return Response(result, status=status.HTTP_200_OK)

    # -- Sender health ------------------------------------------------------
    #
    # A number's health is computed on demand from live counts rather than
    # stored, so these never disagree with the message log they are derived
    # from. See apps/whatsapp/health.py.

    @action(detail=False, methods=["get"], url_path="health")
    def health_overview(self, request):
        """One snapshot per device, for the devices page."""
        return Response([
            {"account_id": account.pk, "name": account.name, **health_service.health_snapshot(account)}
            for account in self.get_queryset()
        ])

    @action(detail=True, methods=["get"], url_path="health")
    def health(self, request, pk=None):
        account = self.get_object()
        return Response({"account_id": account.pk, "name": account.name, **health_service.health_snapshot(account)})

    @action(detail=False, methods=["get"], url_path="health-events")
    def health_events(self, request):
        events = AccountHealthEvent.objects.filter(
            account__tenant=request.user.tenant
        ).select_related("account")[:100]
        return Response(AccountHealthEventSerializer(events, many=True).data)

    @action(detail=True, methods=["get", "patch"], url_path="sending-policy")
    def sending_policy(self, request, pk=None):
        account = self.get_object()
        policy = policy_for(account)

        if request.method == "GET":
            return Response(SendingPolicySerializer(policy).data)

        serializer = SendingPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        # Pausing and resuming go through the health service rather than the
        # serializer, so that either one is recorded as an event with a reason
        # attached -- a number that stopped sending must never be a mystery.
        wants_pause = serializer.validated_data.pop("is_paused", None)
        serializer.save()

        if wants_pause is True and not policy.is_paused:
            health_service.pause_sending(account, "Paused by a team member.", policy=policy)
        elif wants_pause is False and policy.is_paused:
            health_service.resume_sending(account, detail="Resumed by a team member.", policy=policy)

        log_event(
            request=request, action="whatsapp_account.sending_policy_updated", tenant=account.tenant,
            object_type="whatsapp_account", object_id=account.pk,
            metadata={k: v for k, v in serializer.validated_data.items() if not hasattr(v, "isoformat")},
        )
        return Response(SendingPolicySerializer(policy_for(account)).data)

    @action(detail=True, methods=["post"], url_path="pause-sending")
    def pause_sending(self, request, pk=None):
        account = self.get_object()
        minutes = request.data.get("minutes")
        health_service.pause_sending(
            account,
            (request.data.get("reason") or "Paused by a team member.")[:255],
            minutes=int(minutes) if minutes else None,
        )
        log_event(
            request=request, action="whatsapp_account.sending_paused", tenant=account.tenant,
            object_type="whatsapp_account", object_id=account.pk,
        )
        return Response(SendingPolicySerializer(policy_for(account)).data)

    @action(detail=True, methods=["post"], url_path="resume-sending")
    def resume_sending(self, request, pk=None):
        account = self.get_object()
        health_service.resume_sending(account, detail="Resumed by a team member.")
        log_event(
            request=request, action="whatsapp_account.sending_resumed", tenant=account.tenant,
            object_type="whatsapp_account", object_id=account.pk,
        )
        return Response(SendingPolicySerializer(policy_for(account)).data)
