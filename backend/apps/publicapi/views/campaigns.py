"""Campaigns: create, list, read, and drive the lifecycle."""

from apps.campaigns.models import Campaign
from apps.messaging.models import Message

from .. import scopes, throttling
from ..serializers import (
    CAMPAIGN_PREFIX,
    CreateCampaignSerializer,
    serialize_campaign,
    serialize_message,
)
from ..services import campaign_transition, create_campaign, resolve_campaign
from .base import IdempotentMixin, PublicAPIView


class CampaignListView(IdempotentMixin, PublicAPIView):
    """``GET /campaigns`` and ``POST /campaigns``."""

    required_scopes = [scopes.CAMPAIGNS_READ]
    method_scopes = {"POST": [scopes.CAMPAIGNS_CREATE]}

    def initial(self, request, *args, **kwargs):
        # Only the buckets are decided here; the scopes are declarative above.
        # The daily campaign ceiling is charged on creation, not on start: a
        # loop that creates ten thousand drafts has already done the damage
        # this bucket exists to prevent, whether or not it starts them.
        if request.method == "POST":
            self.rate_limit_buckets = ("requests_per_minute", "campaigns_per_day")
        else:
            self.rate_limit_buckets = ("requests_per_minute",)
        super().initial(request, *args, **kwargs)

    def get(self, request):
        queryset = Campaign.objects.filter(tenant=request.auth.tenant).order_by("-created_at")

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.strip().upper())

        return self.ok(self.paginate(queryset, request, serialize_campaign))

    def post(self, request):
        return self.run_idempotent(request, "campaigns", lambda: self._create(request))

    def _create(self, request):
        serializer = CreateCampaignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("start"):
            # Charged *before* anything is created, and in proportion to what
            # is about to be enqueued: without this a single call could put ten
            # thousand messages on the queue while spending one request. Doing
            # it first means a caller over the ceiling is refused outright
            # rather than left holding a draft campaign it cannot start.
            throttling.enforce(
                request.auth, "messages_per_minute", cost=len(data["recipients"]),
            )

        campaign = create_campaign(request.auth, data)
        self._log_extra["campaign_id"] = campaign.pk

        if data.get("start"):
            campaign = campaign_transition(request.auth, campaign, "start")
            self._log_extra["messages_sent"] = campaign.total_recipients

        campaign.refresh_from_db()
        return self.ok(serialize_campaign(campaign), status_code=201)


class CampaignDetailView(PublicAPIView):
    """``GET /campaigns/{id}``."""

    required_scopes = [scopes.CAMPAIGNS_READ]

    def get(self, request, campaign_id):
        return self.ok(serialize_campaign(resolve_campaign(request.auth, campaign_id)))


class CampaignResultsView(PublicAPIView):
    """
    ``GET /campaigns/{id}/results`` -- per-recipient outcomes.

    Separate from the campaign object because it is a different size of thing:
    the campaign is one row an integration polls, and this is a page of
    thousands it walks once when the campaign completes.
    """

    required_scopes = [scopes.CAMPAIGNS_READ]

    def get(self, request, campaign_id):
        campaign = resolve_campaign(request.auth, campaign_id)
        queryset = (
            Message.objects.filter(tenant=request.auth.tenant, campaign=campaign)
            .select_related("whatsapp_account", "contact")
            .order_by("pk")
        )
        payload = self.paginate(queryset, request, serialize_message)
        payload["campaign"] = serialize_campaign(campaign)
        return self.ok(payload)


class CampaignActionView(PublicAPIView):
    """
    ``POST /campaigns/{id}/{start|pause|resume|cancel}``.

    One view for four verbs, with the URL supplying the action. The scope
    differs per verb -- cancelling is its own permission because "stop this"
    is something an operations tool should be able to do without also being
    able to start new sends.
    """

    required_scopes = [scopes.CAMPAIGNS_SEND]

    #: verb -> the scope it needs.
    ACTION_SCOPES = {
        "start": scopes.CAMPAIGNS_SEND,
        "pause": scopes.CAMPAIGNS_SEND,
        "resume": scopes.CAMPAIGNS_SEND,
        "cancel": scopes.CAMPAIGNS_CANCEL,
    }

    def initial(self, request, *args, **kwargs):
        action = kwargs.get("action", "")
        self.required_scopes = [self.ACTION_SCOPES.get(action, scopes.CAMPAIGNS_SEND)]
        if action == "start":
            self.rate_limit_buckets = ("requests_per_minute", "campaigns_per_day")
        super().initial(request, *args, **kwargs)

    def post(self, request, campaign_id, action):
        campaign = resolve_campaign(request.auth, campaign_id)

        if action == "start":
            # Charged against the message bucket for what it is about to
            # enqueue -- see CampaignListView._create.
            from apps.campaigns.services import estimate_recipients

            throttling.enforce(
                request.auth, "messages_per_minute", cost=max(1, estimate_recipients(campaign)),
            )

        campaign = campaign_transition(request.auth, campaign, action)
        self._log_extra["campaign_id"] = campaign.pk
        if action == "start":
            self._log_extra["messages_sent"] = campaign.total_recipients

        campaign.refresh_from_db()
        return self.ok(serialize_campaign(campaign))
