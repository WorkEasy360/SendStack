"""
Settings -> Developer, for the signed-in customer.

These are *internal* endpoints: JWT-authenticated, mounted under the SPA's own
``/api/v1/`` prefix, and using the project's ordinary error shape. They are what
the dashboard talks to. They are deliberately not part of the public API and
not reachable with an API key -- a key that could mint another key, or read the
request log of every other key, would make revocation meaningless.

The one rule they enforce beyond ordinary tenant scoping is that a full secret
is returned exactly once, by the two endpoints that create one.
"""

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.services import log_event

from .. import events as event_defs
from ..models import ApiKey, ApiRequestLog, WebhookDelivery, WebhookEndpoint
from ..scopes import READ_ONLY_SCOPES, SCOPES
from ..serializers import (
    ApiKeyCreateSerializer,
    ApiKeyReadSerializer,
    WebhookDeliveryReadSerializer,
    WebhookEndpointReadSerializer,
    WebhookEndpointSerializer,
)
from ..services import (
    create_api_key,
    revoke_api_key,
    rotate_api_key,
    set_api_key_active,
)


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


class ApiKeyViewSet(viewsets.ModelViewSet):
    """Create, list, disable, rotate, revoke and delete this workspace's API keys."""

    permission_classes = [IsAuthenticated]
    serializer_class = ApiKeyReadSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        return (
            ApiKey.objects.filter(tenant=self.request.user.tenant)
            .prefetch_related("allowed_accounts")
            .order_by("-created_at")
        )

    def create(self, request, *args, **kwargs):
        serializer = ApiKeyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        api_key, full_key = create_api_key(
            tenant=request.user.tenant,
            # The key acts as its creator. An explicit `owner` field would let
            # an admin mint a key that spends somebody else's credits, which is
            # a delegation this product has no concept of.
            owner=request.user,
            created_by=request.user,
            name=data["name"],
            scopes=data["scopes"],
            environment=data.get("environment", ApiKey.Environment.LIVE),
            expires_at=data.get("expires_at"),
            allowed_account_ids=data.get("allowed_account_ids") or [],
            requests_per_minute=data.get("requests_per_minute", 0),
            messages_per_minute=data.get("messages_per_minute", 0),
            campaigns_per_day=data.get("campaigns_per_day", 0),
        )

        log_event(
            request=request,
            action="api_key.created",
            tenant=request.user.tenant,
            object_type="ApiKey",
            object_id=api_key.pk,
            # The prefix, never the key. This row is readable by every admin on
            # the workspace and is kept for years.
            metadata={"name": api_key.name, "prefix": api_key.key_prefix, "scopes": api_key.scopes},
        )

        payload = ApiKeyReadSerializer(api_key).data
        payload["key"] = full_key
        payload["key_notice"] = "Copy this now. The full key is never shown again."
        return Response(payload, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance):
        log_event(
            request=self.request,
            action="api_key.deleted",
            tenant=self.request.user.tenant,
            object_type="ApiKey",
            object_id=instance.pk,
            metadata={"name": instance.name, "prefix": instance.key_prefix},
        )
        instance.delete()

    @action(detail=True, methods=["post"])
    def rotate(self, request, pk=None):
        api_key = self.get_object()
        replacement, full_key = rotate_api_key(api_key, actor=request.user)

        log_event(
            request=request,
            action="api_key.rotated",
            tenant=request.user.tenant,
            object_type="ApiKey",
            object_id=replacement.pk,
            metadata={"replaced_prefix": api_key.key_prefix, "new_prefix": replacement.key_prefix},
        )

        payload = ApiKeyReadSerializer(replacement).data
        payload["key"] = full_key
        payload["key_notice"] = "Copy this now. The previous key stopped working immediately."
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        api_key = self.get_object()
        revoke_api_key(api_key)
        log_event(
            request=request, action="api_key.revoked", tenant=request.user.tenant,
            object_type="ApiKey", object_id=api_key.pk, metadata={"prefix": api_key.key_prefix},
        )
        api_key.refresh_from_db()
        return Response(ApiKeyReadSerializer(api_key).data)

    @action(detail=True, methods=["post"])
    def disable(self, request, pk=None):
        api_key = self.get_object()
        set_api_key_active(api_key, False)
        log_event(
            request=request, action="api_key.disabled", tenant=request.user.tenant,
            object_type="ApiKey", object_id=api_key.pk, metadata={"prefix": api_key.key_prefix},
        )
        api_key.refresh_from_db()
        return Response(ApiKeyReadSerializer(api_key).data)

    @action(detail=True, methods=["post"])
    def enable(self, request, pk=None):
        api_key = self.get_object()
        set_api_key_active(api_key, True)
        log_event(
            request=request, action="api_key.enabled", tenant=request.user.tenant,
            object_type="ApiKey", object_id=api_key.pk, metadata={"prefix": api_key.key_prefix},
        )
        api_key.refresh_from_db()
        return Response(ApiKeyReadSerializer(api_key).data)

    @action(detail=True, methods=["get"])
    def usage(self, request, pk=None):
        """Request/error/message counts for one key over a window of days."""
        api_key = self.get_object()
        try:
            days = min(max(int(request.query_params.get("days", 7)), 1), 90)
        except (TypeError, ValueError):
            days = 7
        since = timezone.now() - timezone.timedelta(days=days)

        logs = ApiRequestLog.objects.filter(api_key=api_key, created_at__gte=since)
        totals = logs.aggregate(
            requests=Count("id"),
            errors=Count("id", filter=Q(status_code__gte=400)),
            rate_limited=Count("id", filter=Q(status_code=429)),
            messages=Count("id", filter=Q(message_id__isnull=False)),
        )

        by_endpoint = (
            logs.values("method", "path")
            .annotate(count=Count("id"), errors=Count("id", filter=Q(status_code__gte=400)))
            .order_by("-count")[:10]
        )
        by_error = (
            logs.exclude(error_code="")
            .values("error_code")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )

        return Response({
            "days": days,
            "totals": totals,
            "top_endpoints": list(by_endpoint),
            "top_errors": list(by_error),
            "lifetime": {
                "requests": api_key.request_count,
                "messages": api_key.message_count,
            },
            "rate_limits": {
                "requests_per_minute": api_key.requests_per_minute,
                "messages_per_minute": api_key.messages_per_minute,
                "campaigns_per_day": api_key.campaigns_per_day,
            },
        })

    @action(detail=False, methods=["get"], url_path="scopes")
    def available_scopes(self, request):
        """What the create-key screen renders its checkboxes from."""
        return Response({
            "scopes": [{"value": value, "label": label} for value, label in SCOPES.items()],
            "read_only_preset": list(READ_ONLY_SCOPES),
        })

    @action(detail=False, methods=["get"], url_path="logs")
    def logs(self, request):
        """The workspace's recent API calls, across every key."""
        queryset = ApiRequestLog.objects.filter(tenant=request.user.tenant).order_by("-created_at")

        key_id = request.query_params.get("api_key")
        if key_id:
            # Coerced rather than passed through: a non-numeric value reaching
            # an integer field lookup is a ValueError, which the API renders as
            # a 500 rather than the empty result it plainly is.
            queryset = queryset.filter(api_key_id=_int_or_none(key_id) or 0)
        if request.query_params.get("errors_only", "").lower() in ("1", "true", "yes"):
            queryset = queryset.filter(status_code__gte=400)

        # `select_related` so naming the key per row is one query rather than
        # 200. The name is what somebody actually recognises -- "Production
        # API" tells them which integration made the call; a bare prefix does
        # not -- but the row must survive the key being deleted, which is why
        # `key_prefix` is stored on the log and the name is only looked up.
        rows = list(queryset.select_related("api_key", "api_key__owner")[:200])
        return Response({
            "results": [
                {
                    "id": row.pk,
                    "api_key": row.api_key_id,
                    "key_prefix": row.key_prefix,
                    "key_name": row.api_key.name if row.api_key else "",
                    # Masked, never the key. Rendered in the UI as
                    # "Production API ss_live_7f3a91c2..." -- enough to identify,
                    # never enough to replay. See ApiRequestLog's docstring.
                    "key_masked": row.api_key.masked_key if row.api_key else row.key_prefix,
                    "user": row.api_key.owner.username if row.api_key and row.api_key.owner else "",
                    "method": row.method,
                    "path": row.path,
                    "status_code": row.status_code,
                    "error_code": row.error_code,
                    "duration_ms": row.duration_ms,
                    "ip_address": row.ip_address,
                    "message_id": f"msg_{row.message_id}" if row.message_id else None,
                    "campaign_id": f"camp_{row.campaign_id}" if row.campaign_id else None,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        })


class WebhookEndpointViewSet(viewsets.ModelViewSet):
    """Create, list, edit, test and delete this workspace's webhook endpoints."""

    permission_classes = [IsAuthenticated]
    serializer_class = WebhookEndpointReadSerializer

    def get_queryset(self):
        return WebhookEndpoint.objects.filter(tenant=self.request.user.tenant).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = WebhookEndpointSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        endpoint = WebhookEndpoint(
            tenant=request.user.tenant,
            created_by=request.user,
            name=data.get("name", ""),
            url=data["url"],
            events=data.get("events") or [],
            is_active=data.get("is_active", True),
        )
        secret = WebhookEndpoint.generate_secret()
        endpoint.set_secret(secret)
        endpoint.save()

        log_event(
            request=request, action="webhook.created", tenant=request.user.tenant,
            object_type="WebhookEndpoint", object_id=endpoint.pk,
            # The URL is the customer's own and belongs in the trail. The secret
            # is not, and never appears in one.
            metadata={"url": endpoint.url, "events": endpoint.events},
        )

        payload = WebhookEndpointReadSerializer(endpoint).data
        payload["secret"] = secret
        payload["secret_notice"] = "Copy this now. It is not shown again."
        return Response(payload, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        endpoint = self.get_object()
        serializer = WebhookEndpointSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        for field in ("name", "url", "events", "is_active"):
            if field in data:
                setattr(endpoint, field, data[field])
        if data.get("is_active"):
            # Re-enabling clears the circuit breaker -- see the public
            # equivalent in views/webhooks.py for why.
            endpoint.consecutive_failures = 0
            endpoint.auto_disabled_at = None
            endpoint.last_error = ""
        endpoint.save()
        return Response(WebhookEndpointReadSerializer(endpoint).data)

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        log_event(
            request=self.request, action="webhook.deleted", tenant=self.request.user.tenant,
            object_type="WebhookEndpoint", object_id=instance.pk, metadata={"url": instance.url},
        )
        instance.delete()

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        endpoint = self.get_object()
        secret = WebhookEndpoint.generate_secret()
        endpoint.set_secret(secret)
        endpoint.save(update_fields=["secret_encrypted", "updated_at"])

        log_event(
            request=request, action="webhook.secret_rotated", tenant=request.user.tenant,
            object_type="WebhookEndpoint", object_id=endpoint.pk, metadata={"url": endpoint.url},
        )

        payload = WebhookEndpointReadSerializer(endpoint).data
        payload["secret"] = secret
        payload["secret_notice"] = "Copy this now. The previous secret stopped working immediately."
        return Response(payload)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """Queue a synthetic delivery through the real delivery path."""
        endpoint = self.get_object()
        event_type = (request.data or {}).get("event") or event_defs.MESSAGE_SENT
        if event_type not in event_defs.EVENT_TYPES:
            return Response(
                {"detail": f"Unknown event '{event_type}'."}, status=status.HTTP_400_BAD_REQUEST,
            )

        envelope = event_defs.build(event_type, {
            "test": True,
            "message": "This is a test delivery from SendStack.",
        })
        delivery = WebhookDelivery.objects.create(
            tenant=endpoint.tenant, endpoint=endpoint, event_type=event_type,
            event_id=envelope["id"], payload=envelope,
            status=WebhookDelivery.Status.PENDING, next_attempt_at=timezone.now(),
        )

        from ..tasks import deliver_webhook

        deliver_webhook.delay(delivery.pk)
        return Response(
            WebhookDeliveryReadSerializer(delivery).data, status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=False, methods=["get"], url_path="events")
    def available_events(self, request):
        return Response({
            "events": [{"value": value, "label": label} for value, label in event_defs.EVENT_TYPES.items()],
        })

    @action(detail=False, methods=["get"], url_path="deliveries")
    def deliveries(self, request):
        queryset = WebhookDelivery.objects.filter(tenant=request.user.tenant).order_by("-created_at")

        endpoint_id = request.query_params.get("endpoint")
        if endpoint_id:
            queryset = queryset.filter(endpoint_id=_int_or_none(endpoint_id) or 0)
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.strip().upper())

        rows = list(queryset[:200])
        return Response({"results": WebhookDeliveryReadSerializer(rows, many=True).data})
