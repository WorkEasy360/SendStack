"""Send a message, and ask what happened to one."""

from apps.messaging.models import Message

from .. import scopes
from ..errors import INVALID_REQUEST, NOT_FOUND, PublicAPIError
from ..serializers import (
    MESSAGE_PREFIX,
    SendMessageSerializer,
    parse_public_id,
    serialize_message,
)
from ..services import send_message
from .base import IdempotentMixin, PublicAPIView


class MessageSendView(IdempotentMixin, PublicAPIView):
    """
    ``POST /messages/send`` -- one message to one recipient.

    Returns 202, not 200. The message is *accepted*, not sent: the provider
    call happens in a worker, and a 200 would tell an integration that WhatsApp
    has the message when all we know is that we do. Callers that need to know
    it actually went out subscribe to `message.sent` or poll
    ``GET /messages/{id}``.
    """

    required_scopes = [scopes.MESSAGES_SEND]
    # Charges both buckets: this is the endpoint that puts work on the queue,
    # and `requests_per_minute` alone would let a caller with a generous
    # request budget bury the workers.
    rate_limit_buckets = ("requests_per_minute", "messages_per_minute")

    def post(self, request):
        return self.run_idempotent(request, "messages/send", lambda: self._send(request))

    def _send(self, request):
        serializer = SendMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        # Carried through the serializer's `validated_data` gap: `kind` is
        # validated in services._message_kind, which owns the compliance
        # meaning of the value and should be the only thing that decides it.
        payload["kind"] = request.data.get("kind")
        payload["name"] = request.data.get("name", "")

        message = send_message(request.auth, payload)

        # Picked up by the request log, so "which API call sent this?" is one
        # indexed lookup rather than a timestamp correlation.
        self._log_extra["message_id"] = message.pk
        self._log_extra["messages_sent"] = 1

        return self.ok(
            {
                "message_id": f"{MESSAGE_PREFIX}{message.pk}",
                "status": "queued",
                "channel": "whatsapp",
                "account_id": f"account_{message.whatsapp_account_id}",
                "recipient": message.to_phone,
            },
            status_code=202,
        )


class MessageDetailView(PublicAPIView):
    """``GET /messages/{id}`` -- the current status of one message."""

    required_scopes = [scopes.MESSAGES_STATUS]

    def get(self, request, message_id):
        pk = parse_public_id(message_id, MESSAGE_PREFIX)
        message = None
        if pk is not None:
            message = (
                Message.objects.filter(tenant=request.auth.tenant, pk=pk)
                .select_related("whatsapp_account", "contact")
                .first()
            )
        if message is None:
            # Filtered by tenant in the query, so another workspace's message
            # is a 404 rather than a 403 -- a 403 would confirm the id exists.
            raise PublicAPIError(
                NOT_FOUND, f"No message '{message_id}' in this workspace.", status_code=404,
            )
        return self.ok(serialize_message(message))


class MessageListView(PublicAPIView):
    """``GET /messages`` -- the workspace's messages, newest first."""

    required_scopes = [scopes.MESSAGES_READ]

    def get(self, request):
        queryset = (
            Message.objects.filter(tenant=request.auth.tenant)
            .select_related("whatsapp_account", "contact")
            .order_by("-queued_at")
        )

        status_filter = request.query_params.get("status")
        if status_filter:
            # Filtering happens in the *public* vocabulary, so a caller uses
            # the same words the API gave them. `failed` covers SKIPPED too,
            # matching what PUBLIC_STATUS publishes.
            from ..serializers import PUBLIC_STATUS

            wanted = {
                internal for internal, public in PUBLIC_STATUS.items()
                if public == status_filter.strip().lower()
            }
            if not wanted:
                raise PublicAPIError(
                    INVALID_REQUEST,
                    f"Unknown status '{status_filter}'. Use one of: "
                    f"{', '.join(sorted(set(PUBLIC_STATUS.values())))}.",
                )
            queryset = queryset.filter(status__in=wanted)

        direction = request.query_params.get("direction")
        if direction:
            queryset = queryset.filter(direction=direction.strip().upper())

        account_id = request.query_params.get("account_id")
        if account_id:
            account_pk = parse_public_id(account_id, "account_")
            queryset = queryset.filter(whatsapp_account_id=account_pk or 0)

        recipient = request.query_params.get("recipient")
        if recipient:
            queryset = queryset.filter(to_phone=recipient.strip())

        return self.ok(self.paginate(queryset, request, serialize_message))
