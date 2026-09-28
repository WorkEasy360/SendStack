import logging

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.services import log_event
from core.bulk import BulkActionError, BulkActionMixin

from . import preview as campaign_preview
from . import services
from .models import Campaign
from .serializers import CampaignEstimateSerializer, CampaignSerializer
from .services import CampaignError

logger = logging.getLogger("sendstack")


class CampaignViewSet(BulkActionMixin, viewsets.ModelViewSet):

    permission_classes = [IsAuthenticated]
    serializer_class = CampaignSerializer
    filterset_fields = ["status", "whatsapp_account"]
    search_fields = ["name"]

    bulk_object_label = "campaign"
    bulk_actions = {
        "delete": "_bulk_delete",
        "cancel": "_bulk_cancel",
        "pause": "_bulk_pause",
        "resume": "_bulk_resume",
    }

    def get_queryset(self):
        return Campaign.objects.filter(tenant=self.request.user.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.user.tenant, created_by=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.status != Campaign.Status.DRAFT:
            raise ValidationError("Only draft campaigns can be edited.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.status not in (Campaign.Status.DRAFT, Campaign.Status.CANCELLED, Campaign.Status.COMPLETED, Campaign.Status.FAILED):
            raise ValidationError("Pause or cancel this campaign before deleting it.")
        instance.delete()

    # Bulk handlers reuse perform_destroy and the same service functions the
    # single-object endpoints call, so a selection of six campaigns is exactly
    # six of the existing operations -- including their status rules. A
    # campaign the rules refuse (a RUNNING one, say) fails on its own and the
    # rest still go through; see core.bulk.
    def _bulk_delete(self, request, instance):
        self.perform_destroy(instance)

    def _bulk_lifecycle(self, request, instance, fn, audit_action):
        try:
            campaign = fn(instance)
        except CampaignError as exc:
            raise BulkActionError(str(exc)) from exc
        log_event(
            request=request, action=audit_action, tenant=campaign.tenant,
            object_type="campaign", object_id=campaign.pk,
        )

    def _bulk_cancel(self, request, instance):
        self._bulk_lifecycle(request, instance, services.cancel, "campaign.cancelled")

    def _bulk_pause(self, request, instance):
        self._bulk_lifecycle(request, instance, services.pause, "campaign.paused")

    def _bulk_resume(self, request, instance):
        self._bulk_lifecycle(request, instance, services.resume, "campaign.resumed")

    def _lifecycle_action(self, request, pk, fn, audit_action):
        campaign = self.get_object()
        try:
            campaign = fn(campaign)
        except CampaignError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        log_event(request=request, action=audit_action, tenant=campaign.tenant, object_type="campaign", object_id=campaign.pk)

        return Response(CampaignSerializer(campaign).data)

    @action(detail=True, methods=["get"])
    def health(self, request, pk=None):
        """
        Message counts by status for this campaign, with the recovery figures
        broken out -- how many sends are stuck in SENDING past their lease and
        how many landed in RECOVERY_REQUIRED. This is the view that tells an
        operator a campaign has silently stopped moving.
        """
        from apps.messaging.recovery import campaign_health

        return Response(campaign_health(self.get_object()))

    @action(detail=True, methods=["get"])
    def estimate(self, request, pk=None):
        campaign = self.get_object()
        estimated = services.estimate_recipients(campaign)
        serializer = CampaignEstimateSerializer({"estimated_recipients": estimated})
        logger.debug("Campaign %s estimated at %s recipient(s).", campaign.pk, estimated)
        return Response(serializer.data)

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        """
        The full pre-send picture: who is eligible, who is skipped and why, and
        what it will cost.

        `estimate` above is kept, and still answers only "how many contacts
        match the rule". This is the number a customer should be shown before
        spending anything -- see apps.campaigns.preview for why the two are
        different questions and why this one is computed from the send path's
        own rules rather than from a second copy of them.
        """
        campaign = self.get_object()
        data = campaign_preview.build(campaign)
        logger.debug(
            "Campaign %s preview: %s eligible of %s selected.",
            campaign.pk, data["eligible"], data["selected"],
        )
        return Response(data)

    @action(detail=True, methods=["post"])
    def launch(self, request, pk=None):
        return self._lifecycle_action(request, pk, services.launch, "campaign.launched")

    @action(detail=True, methods=["post"])
    def pause(self, request, pk=None):
        return self._lifecycle_action(request, pk, services.pause, "campaign.paused")

    @action(detail=True, methods=["post"])
    def resume(self, request, pk=None):
        return self._lifecycle_action(request, pk, services.resume, "campaign.resumed")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._lifecycle_action(request, pk, services.cancel, "campaign.cancelled")

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        return self._lifecycle_action(request, pk, services.retry_failed, "campaign.retried")
