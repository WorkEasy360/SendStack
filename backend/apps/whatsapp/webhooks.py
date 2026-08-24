import json
import logging

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import log_event

from .connections.manager import ConnectionManager
from .models import WhatsAppAccount, WhatsAppWebhookEvent
from .providers import get_provider, get_provider_by_name
from .signals import webhook_event_stored

logger = logging.getLogger("multisender")


class WhatsAppWebhookView(APIView):
    """
    Public per-account webhook endpoint for the configured provider.

    GET handles the provider's verification handshake. POST ingests message
    events (inbound messages, delivery statuses) idempotently: each
    (account, provider_event_id) pair is stored at most once, so redelivery
    of the same event is a no-op. This endpoint is shared by every provider
    -- connection-lifecycle events (QR pairing confirmed, session expired)
    go to WhatsAppConnectionWebhookView instead, since a QR session's
    provider-side identity is the session, not yet a confirmed account.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, account_id):
        account = get_object_or_404(WhatsAppAccount, pk=account_id)
        provider = get_provider(account)

        challenge = provider.verify_webhook_challenge(account, request.query_params)
        if challenge is None:
            return Response(status=403)

        return HttpResponse(challenge, content_type="text/plain")

    def post(self, request, account_id):
        account = get_object_or_404(WhatsAppAccount, pk=account_id)
        provider = get_provider(account)

        signature = request.headers.get("X-Hub-Signature-256")
        if not provider.validate_webhook_signature(account, request.body, signature):
            logger.warning("Rejected WhatsApp webhook with invalid signature for account_id=%s", account_id)
            return Response(status=403)

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response(status=400)

        for event in provider.parse_webhook_payload(payload):
            stored_event, created = WhatsAppWebhookEvent.objects.get_or_create(
                account=account,
                provider_event_id=event.provider_event_id,
                defaults={
                    "event_type": event.event_type,
                    "payload": event.raw,
                    "related_message_id": event.message_id or "",
                    "status": event.status or "",
                },
            )
            if created:
                webhook_event_stored.send(sender=WhatsAppWebhookEvent, event=stored_event)

        return Response(status=200)


class WhatsAppConnectionWebhookView(APIView):
    """
    Provider-level webhook for connection-lifecycle events (QR bridge only
    today): pairing confirmed, session expired/errored. Looked up by
    session_id rather than account_id since the bridge only knows the
    session it created, not MultiSender's account id.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider):
        try:
            wa_provider = get_provider_by_name(provider.upper())
        except Exception:
            return Response(status=404)

        event = wa_provider.parse_connection_webhook(request.body, request.headers.get("X-Bridge-Signature"))
        if event is None:
            logger.warning("Rejected connection webhook for provider=%s: invalid signature or payload", provider)
            return Response(status=403)

        session = ConnectionManager.handle_connection_webhook(provider.upper(), event)
        if session is None:
            return Response(status=404)

        log_event(
            request=request,
            action="whatsapp_account.connection_webhook",
            tenant=session.whatsapp_account.tenant,
            object_type="whatsapp_account",
            object_id=session.whatsapp_account_id,
            metadata={"status": event.status},
        )
        return Response(status=200)
