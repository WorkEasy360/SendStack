"""
Managing webhook endpoints through the API itself.

The same objects the Settings -> Developer -> Webhooks screen manages, exposed
so an integration can register its own receiver during installation rather than
asking the customer to paste a URL into a dashboard. That is what makes a
one-click install possible.

The secret is shown once, at creation, exactly like an API key -- but for a
different reason. An API key's secret is hashed and genuinely unrecoverable;
this one is encrypted and could be shown again, and deliberately is not. A
signing secret that can be read back is a signing secret that leaks through
every screen-share, support session and over-broad read scope that ever touches
this endpoint.
"""

from django.utils import timezone

from .. import events as event_defs
from .. import scopes
from ..errors import INVALID_REQUEST, NOT_FOUND, PublicAPIError
from ..models import WebhookDelivery, WebhookEndpoint
from ..serializers import (
    WebhookEndpointSerializer,
    serialize_delivery,
    serialize_webhook_endpoint,
)
from .base import PublicAPIView


def _int_or_none(value):
    """
    A query parameter that has to be an integer, or nothing.

    Django raises ValueError -- rendered as a 500 -- when a non-numeric string
    reaches an integer field lookup, so `?endpoint_id=abc` would be a server
    error rather than the empty result it obviously is.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _endpoint_or_404(api_key, endpoint_id):
    endpoint = WebhookEndpoint.objects.filter(tenant=api_key.tenant, pk=endpoint_id).first()
    if endpoint is None:
        raise PublicAPIError(
            NOT_FOUND, f"No webhook endpoint '{endpoint_id}' in this workspace.", status_code=404,
        )
    return endpoint


class WebhookListView(PublicAPIView):
    """``GET /webhooks`` and ``POST /webhooks``."""

    required_scopes = [scopes.WEBHOOKS_READ]
    method_scopes = {"POST": [scopes.WEBHOOKS_MANAGE]}

    def get(self, request):
        queryset = WebhookEndpoint.objects.filter(tenant=request.auth.tenant).order_by("-created_at")
        return self.ok(self.paginate(queryset, request, serialize_webhook_endpoint))

    def post(self, request):
        serializer = WebhookEndpointSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        endpoint = WebhookEndpoint(
            tenant=request.auth.tenant,
            created_by=request.auth.owner,
            name=data.get("name", ""),
            url=data["url"],
            events=data.get("events") or [],
            is_active=data.get("is_active", True),
        )
        secret = WebhookEndpoint.generate_secret()
        endpoint.set_secret(secret)
        endpoint.save()

        payload = serialize_webhook_endpoint(endpoint)
        payload["secret"] = secret
        payload["secret_notice"] = "Store this now. It is not shown again."
        return self.ok(payload, status_code=201)


class WebhookDetailView(PublicAPIView):
    """``GET``, ``PUT``/``PATCH`` and ``DELETE`` on ``/webhooks/{id}``."""

    required_scopes = [scopes.WEBHOOKS_READ]
    method_scopes = {
        "PUT": [scopes.WEBHOOKS_MANAGE],
        "PATCH": [scopes.WEBHOOKS_MANAGE],
        "DELETE": [scopes.WEBHOOKS_MANAGE],
    }

    def get(self, request, endpoint_id):
        return self.ok(serialize_webhook_endpoint(_endpoint_or_404(request.auth, endpoint_id)))

    def patch(self, request, endpoint_id):
        endpoint = _endpoint_or_404(request.auth, endpoint_id)
        serializer = WebhookEndpointSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        for field in ("name", "url", "events", "is_active"):
            if field in data:
                setattr(endpoint, field, data[field])

        if data.get("is_active"):
            # Re-enabling clears the circuit breaker. Without this an endpoint
            # auto-disabled after a bad week would be switched back on already
            # one failure away from being switched off again.
            endpoint.consecutive_failures = 0
            endpoint.auto_disabled_at = None
            endpoint.last_error = ""

        endpoint.save()
        return self.ok(serialize_webhook_endpoint(endpoint))

    def put(self, request, endpoint_id):
        return self.patch(request, endpoint_id)

    def delete(self, request, endpoint_id):
        _endpoint_or_404(request.auth, endpoint_id).delete()
        return self.ok({"deleted": True})


class WebhookRotateSecretView(PublicAPIView):
    """``POST /webhooks/{id}/rotate-secret``."""

    required_scopes = [scopes.WEBHOOKS_MANAGE]

    def post(self, request, endpoint_id):
        endpoint = _endpoint_or_404(request.auth, endpoint_id)
        secret = WebhookEndpoint.generate_secret()
        endpoint.set_secret(secret)
        endpoint.save(update_fields=["secret_encrypted", "updated_at"])

        payload = serialize_webhook_endpoint(endpoint)
        payload["secret"] = secret
        # No overlap window, deliberately: deliveries signed with the old
        # secret start failing verification immediately. A receiver that needs
        # zero-downtime rotation registers a second endpoint, cuts over, and
        # deletes the first.
        payload["secret_notice"] = "Store this now. It is not shown again, and the previous secret stopped working."
        return self.ok(payload)


class WebhookTestView(PublicAPIView):
    """
    ``POST /webhooks/{id}/test`` -- deliver a synthetic event.

    Queued through the ordinary delivery path rather than sent inline, so what
    a developer sees while testing is exactly what they will get in production:
    the same signature, the same headers, the same retry behaviour, and a row
    in the same delivery log.
    """

    required_scopes = [scopes.WEBHOOKS_MANAGE]

    def post(self, request, endpoint_id):
        endpoint = _endpoint_or_404(request.auth, endpoint_id)

        event_type = (request.data or {}).get("event") or event_defs.MESSAGE_SENT
        if event_type not in event_defs.EVENT_TYPES:
            raise PublicAPIError(
                INVALID_REQUEST,
                f"Unknown event '{event_type}'. Valid events: {', '.join(event_defs.ALL_EVENT_TYPES)}.",
            )

        envelope = event_defs.build(event_type, {
            "test": True,
            "message": "This is a test delivery from SendStack.",
        })
        delivery = WebhookDelivery.objects.create(
            tenant=endpoint.tenant,
            endpoint=endpoint,
            event_type=event_type,
            event_id=envelope["id"],
            payload=envelope,
            status=WebhookDelivery.Status.PENDING,
            next_attempt_at=timezone.now(),
        )

        from ..tasks import deliver_webhook

        deliver_webhook.delay(delivery.pk)
        return self.ok({"delivery": serialize_delivery(delivery)}, status_code=202)


class WebhookDeliveryListView(PublicAPIView):
    """``GET /webhooks/deliveries`` -- the delivery log."""

    required_scopes = [scopes.WEBHOOKS_READ]

    def get(self, request):
        queryset = WebhookDelivery.objects.filter(tenant=request.auth.tenant).order_by("-created_at")

        endpoint_id = request.query_params.get("endpoint_id")
        if endpoint_id:
            queryset = queryset.filter(endpoint_id=_int_or_none(endpoint_id) or 0)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.strip().upper())

        return self.ok(self.paginate(queryset, request, serialize_delivery))
