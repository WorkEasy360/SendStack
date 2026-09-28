from django.db.models import Count, ProtectedError, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.audit.services import log_event
from apps.subscriptions.services import quota_guard
from core.bulk import BulkActionMixin

from . import health as health_service
from .connections.manager import ConnectionManager
from .limits import policy_for
from .models import AccountHealthEvent, WhatsAppAccount
from .serializers import AccountHealthEventSerializer, SendingPolicySerializer, WhatsAppAccountSerializer
from .services import WhatsAppConnectionService


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
        # The plan's device ceiling, held across the create. Two requests
        # arriving together on the last free slot both block on the tenant's
        # row inside quota_guard, so the second one counts the first one's
        # device and is refused -- see apps.subscriptions.services.quota_guard.
        #
        # Deliberately only here: connecting, reconnecting and refreshing a QR
        # all act on a device that already exists and must never be refused
        # for taking a slot it is already holding.
        with quota_guard(self.request.user.tenant_id, "devices"):
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
        # Deleting a device always succeeds, whatever it is mid-way through.
        # Provider teardown first (e.g. closes the Baileys socket and frees
        # the QR bridge session) so deletion never leaves an orphaned session
        # behind -- same leak class as refresh_qr, see
        # ConnectionManager.refresh_qr. ConnectionManager.disconnect swallows
        # ProviderError internally (logs + continues), so deletion still
        # proceeds if the provider is unreachable, and dropping the
        # connection is also what takes any campaign still running on this
        # device out of the eligible pool -- see campaigns.assignment.
        WhatsAppConnectionService.disconnect(instance)

        # Campaign.whatsapp_account and Message.whatsapp_account are PROTECT:
        # send history deliberately outlives the device it went out from, so
        # the row physically cannot be erased once anything references it.
        # That is a reporting constraint, not a reason to refuse the user --
        # this used to answer a 409 telling them to disconnect instead. So a
        # referenced device is archived: it leaves every list and picker,
        # stops counting against the plan's device limit and can never send
        # again, while the reports that join through it keep resolving. Only
        # a device nothing references is erased outright.
        archived = instance.campaigns.exists() or instance.messages.exists()
        log_event(
            request=self.request,
            action="whatsapp_account.deleted",
            tenant=instance.tenant,
            object_type="whatsapp_account",
            object_id=instance.pk,
            metadata={"account_type": instance.account_type, "name": instance.name, "archived": archived},
        )
        if archived:
            self._archive(instance)
            return
        try:
            instance.delete()
        except ProtectedError:
            # Backstop for any PROTECT relation added after the two checked
            # above -- archiving keeps the user's delete honest rather than
            # answering a bare 500.
            self._archive(instance)

    @staticmethod
    def _archive(instance):
        instance.archived_at = timezone.now()
        instance.status = WhatsAppAccount.Status.DISCONNECTED
        instance.save(update_fields=["archived_at", "status", "updated_at"])

    # Both handlers go through perform_destroy / the same disconnect service
    # the single-device endpoints use, so a device with send history is
    # archived here too rather than being refused mid-batch.
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

    @action(detail=False, methods=["get"], url_path="capabilities")
    def capabilities(self, request):
        """
        What each of this workspace's numbers can actually deliver.

        Read by the composer before a message type is offered, so somebody
        building a poll on a Cloud API number is told before they build it
        rather than after a thousand recipients received a numbered list.

        Keyed by account id, with the provider defaults alongside, because the
        composer also has to answer "could *any* of my numbers send this?"
        when no number has been chosen yet.
        """
        from .providers.registry import provider_capabilities

        accounts = self.get_queryset().only("id", "name", "provider", "status")
        return Response({
            "accounts": [
                {
                    "id": account.pk,
                    "name": account.name,
                    "provider": account.provider,
                    "status": account.status,
                    "capabilities": ConnectionManager.capabilities(account),
                }
                for account in accounts
            ],
            "providers": provider_capabilities(),
        })

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
