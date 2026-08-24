"""
QR/device-linking provider, backed by a separate, self-hosted "QR bridge"
microservice reached over HTTP + webhook.

MultiSender does not implement, host, or reverse-engineer any WhatsApp
protocol here. This module only speaks a documented HTTP contract to
whatever bridge service is configured via ``QR_PROVIDER_BASE_URL`` --
building/operating that bridge (e.g. wrapping a multi-device WhatsApp Web
client library) is a separate project. If no bridge is configured, every
method raises a clear, non-misleading ``ProviderError`` -- this adapter must
never report an account as CONNECTED without the bridge confirming it.

HTTP contract expected of the bridge service:

    POST   {base_url}/sessions
           body: {"account_id": <int>,
                  "connection_webhook_url": <str>,   # lifecycle events (QR ready, paired, expired)
                  "message_webhook_url": <str>}       # inbound messages / delivery statuses
           -> {"session_id": str, "status": "QR_READY"|"CONNECTED"|"ERROR",
               "qr": str | None,          # ready-to-render data URL
               "expires_at": iso8601 | None,
               "phone_number": str | None, "display_name": str | None,
               "provider_account_id": str | None, "error": str | None}

    GET    {base_url}/sessions/{session_id}
           -> same shape as POST /sessions

    DELETE {base_url}/sessions/{session_id}
           -> 204 No Content

    POST   {base_url}/sessions/{session_id}/messages
           body: {"to": str,    # bare digits, no "+" -- see _to_msisdn()
                  "type": "text"|"image"|..., ...}
           -> {"success": bool, "provider_message_id": str | None,
               "error": str | None,
               "ambiguous": bool}   # true (with HTTP 504) when the bridge gave
                                    # up on a send already in flight, so the
                                    # caller must not retry it -- see
                                    # SendResult.ambiguous in base.py

Auth: every request carries ``Authorization: Bearer {QR_PROVIDER_API_KEY}``.

Webhooks (bridge -> MultiSender) are HMAC-SHA256 over the raw body with
``QR_PROVIDER_WEBHOOK_SECRET``, but the two kinds land on different Django
views with different (historically-driven) header names:

    POST connection_webhook_url  ->  /api/v1/whatsapp/webhooks/qr_provider/
        header: X-Bridge-Signature: sha256=<hmac>
        body: {"session_id": str, "status": str, "phone_number": str | None,
               "display_name": str | None, "provider_account_id": str | None,
               "error": str | None}

    POST message_webhook_url  ->  /api/v1/whatsapp/webhook/{account_id}/
        header: X-Hub-Signature-256: sha256=<hmac>   (Meta's convention --
               this endpoint is shared with the Cloud API provider, whose
               webhook view was written first and hardcodes that name)
        body: {"messages": [{"id": str, "from": <digits, no "+">,
                             "type": "text"|"image"|"video"|"document"|...,
                             "body": str | None, "push_name": str | None}],
               "statuses": [{"id": str, "status": "sent"|"delivered"|"read"}]}
               -- note "from" must be the sender's phone number, not a
               WhatsApp LID; parse_webhook_payload() below translates each
               message into the Cloud API shape.
"""

import hashlib
import hmac
import logging
import re
import time
from datetime import datetime

import requests
from django.conf import settings

from .base import (
    ConnectionResult,
    ConnectionSessionResult,
    ConnectionStatusResult,
    ConnectionWebhookEvent,
    ProviderError,
    QRResult,
    SendResult,
    WebhookEvent,
    WebhookEventType,
    WhatsAppProvider,
)

logger = logging.getLogger("multisender")


# A Baileys send can legitimately take longer than a typical HTTP call --
# WhatsApp's device-list/session queries over the socket run close to
# Baileys' own ~60s default query timeout under a slow/flaky network path.
# 15s was cutting genuinely-in-progress sends short, forcing them into a
# 30s/60s/120s retry backoff instead of just letting them finish.
REQUEST_TIMEOUT_SECONDS = 45


def _to_msisdn(value: str | None) -> str:
    """
    Reduce a stored phone number to the bare digits the bridge expects.

    We store numbers in E.164 ("+918429577706"), but the bridge turns whatever
    it is given into a WhatsApp JID of the form ``{value}@s.whatsapp.net``. A
    JID with a leading "+" is malformed: WhatsApp's device-list lookup for it
    never resolves, so the send neither succeeds nor errors -- it simply hangs
    until the caller gives up. Translating our format into the bridge's wire
    format is this adapter's job, so it happens here, at the one choke point
    every send goes through.
    """
    return re.sub(r"\D", "", value or "")


# Types the bridge can report that map onto a Cloud API media envelope. Anything
# else (audio, sticker, "unknown") is passed through under its own name and
# renders as an explicit "unsupported message type" line rather than an empty one.
_BRIDGE_MEDIA_TYPES = frozenset({"image", "video", "document"})


def _as_cloud_api_message(message: dict) -> dict:
    """
    Translate one bridge inbound message into the Cloud API's message shape.

    The bridge reports a flat object -- ``{"id", "from", "type", "body",
    "push_name"}`` -- while Cloud API nests the content under a per-type key
    (``{"text": {"body": ...}}``, ``{"image": {"caption": ...}}``). Inbox
    ingestion reads the Cloud API shape only, so translating here is what keeps
    apps.inbox free of provider branching.
    """
    message_type = (message.get("type") or "text").lower()
    body = message.get("body") or ""
    button_id = message.get("button_id") or ""

    if button_id:
        # The bridge flattens every shape of tapped button into one
        # `button_id`; it is expanded back into the Cloud API's interactive
        # reply here so apps.inbox has a single shape to read.
        message_type = "interactive"
        content = {"interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": body}}}
    elif message_type in _BRIDGE_MEDIA_TYPES:
        content = {message_type: {"caption": body}}
    elif message_type == "text":
        content = {"text": {"body": body}}
    else:
        content = {}

    return {
        "id": message.get("id"),
        "from": message.get("from") or "",
        "type": message_type,
        **content,
    }


def _parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _reachable_link(link: str) -> str:
    """
    The attachment URL as the bridge can actually fetch it.

    Baileys downloads the link from inside the qr-bridge container, so a link
    built for the browser ("http://localhost/media/x.png") resolves to that
    container itself and the send fails. When both media base URLs are
    configured, the public origin is swapped for the internal one on the way
    out only -- what is stored on the message stays the link a person can open.
    Unconfigured, this is a no-op.
    """
    public = getattr(settings, "MEDIA_PUBLIC_BASE_URL", "")
    internal = getattr(settings, "MEDIA_INTERNAL_BASE_URL", "")
    if link and public and internal and link.startswith(public):
        return internal + link[len(public):]
    return link


class WhatsAppQRBridgeProvider(WhatsAppProvider):
    """Talks to an external QR/device-linking bridge service over HTTP."""

    supports_qr = True

    # The bridge sends buttons as a native-flow interactive message -- see
    # sendInteractive() in qr-bridge/src/providers/baileysProvider.ts.
    supports_interactive_buttons = True

    def __init__(self, base_url: str, api_key: str, webhook_secret: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._webhook_secret = webhook_secret

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            # A fresh connection per call rather than a persistent
            # requests.Session: this provider is a long-lived singleton
            # (registry.py) reused for a worker's whole lifetime, while sends
            # are tens of seconds apart (campaign pacing), so there is nothing
            # for a pool to amortise -- and an idle pooled socket is one more
            # thing that can go stale between calls.
            response = requests.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.ConnectTimeout as exc:
            # Never reached the bridge, so nothing can have been sent.
            raise ProviderError(f"Timed out reaching the QR provider: {exc}", code="timeout") from exc
        except requests.Timeout as exc:
            # A read timeout means the request was already transmitted and we
            # simply never heard back -- the bridge may or may not have acted
            # on it. Flag it so sends are not blindly retried into duplicates.
            raise ProviderError(
                f"Timed out waiting for the QR provider: {exc}", code="timeout", ambiguous=True
            ) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach the QR provider: {exc}", code="network_error") from exc

        if response.status_code == 204:
            return {}

        try:
            data = response.json()
        except ValueError:
            if response.ok:
                raise ProviderError("QR provider returned a response that could not be parsed.", code="invalid_response")
            data = {}

        if response.status_code in (401, 403):
            raise ProviderError(data.get("error") or "QR provider rejected the configured API key.", code="authentication_error")

        if not response.ok:
            raise ProviderError(
                data.get("error") or f"QR provider returned HTTP {response.status_code}.",
                code="provider_error",
                # The bridge sets this when it gave up on a send that was
                # already in flight to WhatsApp (see its own send timeout).
                ambiguous=bool(data.get("ambiguous")),
            )

        return data

    def _session_result(self, data: dict) -> ConnectionSessionResult:
        return ConnectionSessionResult(
            status=data.get("status", "ERROR"),
            session_id=data.get("session_id"),
            qr_data=data.get("qr"),
            qr_expires_at=_parse_iso8601(data.get("expires_at")),
            phone_number=data.get("phone_number"),
            display_name=data.get("display_name"),
            provider_account_id=data.get("provider_account_id"),
            error=data.get("error"),
        )

    def start_connection(self, account) -> ConnectionSessionResult:
        # Two separate callback URLs -- connection lifecycle (QR ready,
        # paired, expired) vs. inbound messages/delivery statuses -- so the
        # bridge never has to guess one from the other. See module docstring.
        data = self._request(
            "POST",
            "/sessions",
            json={
                "account_id": account.pk,
                "connection_webhook_url": f"{settings.BACKEND_PUBLIC_URL}/api/v1/whatsapp/webhooks/qr_provider/",
                "message_webhook_url": f"{settings.BACKEND_PUBLIC_URL}/api/v1/whatsapp/webhook/{account.pk}/",
            },
        )
        return self._session_result(data)

    def get_connection_status(self, account, session_id: str | None) -> ConnectionStatusResult:
        if not session_id:
            return ConnectionStatusResult(status="DISCONNECTED")
        data = self._request("GET", f"/sessions/{session_id}")
        return ConnectionStatusResult(
            status=data.get("status", "ERROR"),
            qr_data=data.get("qr"),
            qr_expires_at=_parse_iso8601(data.get("expires_at")),
            phone_number=data.get("phone_number"),
            display_name=data.get("display_name"),
            provider_account_id=data.get("provider_account_id"),
            error=data.get("error"),
        )

    def get_qr(self, account, session_id: str) -> QRResult:
        data = self._request("GET", f"/sessions/{session_id}")
        qr_data = data.get("qr")
        if not qr_data:
            raise ProviderError("No QR code is available for this session.", code="no_qr")
        return QRResult(qr_data=qr_data, expires_at=_parse_iso8601(data.get("expires_at")))

    def disconnect(self, account, session_id: str | None) -> None:
        if session_id:
            self._request("DELETE", f"/sessions/{session_id}")

    def get_account_info(self, account) -> ConnectionResult:
        if not account.provider_session_id:
            return ConnectionResult(connected=False, error="No active session.")
        status_result = self.get_connection_status(account, account.provider_session_id)
        return ConnectionResult(
            connected=status_result.status == "CONNECTED",
            phone_number=status_result.phone_number,
            display_name=status_result.display_name,
            error=status_result.error,
        )

    def send_text_message(self, account, to: str, body: str) -> SendResult:
        return self._send(account, {"to": to, "type": "text", "text": {"body": body}})

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None) -> SendResult:
        return self._send(
            account,
            {
                "to": to,
                "type": media_type,
                "media": {"link": _reachable_link(media_link), "caption": caption, "filename": filename},
            },
        )

    def send_interactive_message(self, account, to: str, body: str, buttons: list) -> SendResult:
        if not buttons:
            return SendResult(success=False, error="This message has no buttons to send.")
        return self._send(
            account,
            {
                "to": to,
                "type": "interactive",
                "interactive": {"body": body, "buttons": buttons},
            },
        )

    def send_template_message(self, account, to, template_name, language_code, components=None) -> SendResult:
        # Session-based (non-Cloud-API) WhatsApp connections do not use
        # Meta's approved-template system -- templates are a Cloud API/BSP
        # concept tied to Meta's messaging limits, not something a
        # personal/Business-app-linked session sends.
        raise ProviderError(
            "Template messages are only available on WhatsApp API (Cloud API) connections.",
            code="not_supported",
        )

    def _send(self, account, payload: dict) -> SendResult:
        if not account.provider_session_id:
            return SendResult(success=False, error="WhatsApp account has no active session.")

        to = _to_msisdn(payload.get("to"))
        if not to:
            return SendResult(success=False, error="Recipient phone number is not a valid number.")
        payload = {**payload, "to": to}

        started = time.monotonic()
        try:
            data = self._request("POST", f"/sessions/{account.provider_session_id}/messages", json=payload)
        except ProviderError as exc:
            logger.warning(
                "QR send failed account_id=%s to=%s type=%s in %.3fs: %s (code=%s ambiguous=%s)",
                account.pk, to, payload.get("type"), time.monotonic() - started,
                exc.message, exc.code, exc.ambiguous,
            )
            return SendResult(success=False, error=exc.message, ambiguous=exc.ambiguous)

        result = SendResult(
            success=data.get("success", False),
            provider_message_id=data.get("provider_message_id"),
            error=data.get("error"),
            ambiguous=bool(data.get("ambiguous")),
        )
        logger.info(
            "QR send account_id=%s to=%s type=%s success=%s in %.3fs provider_message_id=%s",
            account.pk, to, payload.get("type"), result.success, time.monotonic() - started,
            result.provider_message_id,
        )
        return result

    def verify_webhook_challenge(self, account, query_params: dict) -> str | None:
        # The bridge posts events directly (see parse_connection_webhook) --
        # it has no separate GET verification handshake to answer.
        return None

    def validate_webhook_signature(self, account, raw_body: bytes, signature_header: str | None) -> bool:
        return self._valid_signature(raw_body, signature_header)

    def _valid_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        if not self._webhook_secret or not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        provided = signature_header.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    def parse_webhook_payload(self, payload: dict) -> list[WebhookEvent]:
        # Inbound messages are translated to the Cloud API's shape here so that
        # message ingestion in apps.inbox stays provider-agnostic.
        events: list[WebhookEvent] = []
        for message in payload.get("messages", []):
            message_id = message.get("id")
            if not message_id:
                continue
            events.append(
                WebhookEvent(
                    event_type=WebhookEventType.INBOUND_MESSAGE,
                    provider_event_id=message_id,
                    raw={
                        "message": _as_cloud_api_message(message),
                        "profile_name": message.get("push_name") or "",
                    },
                )
            )
        for status_obj in payload.get("statuses", []):
            message_id = status_obj.get("id")
            status = status_obj.get("status")
            if not message_id or not status:
                continue
            events.append(
                WebhookEvent(
                    event_type=WebhookEventType.MESSAGE_STATUS,
                    provider_event_id=f"{message_id}:{status}",
                    raw=status_obj,
                    message_id=message_id,
                    status=status.upper(),
                )
            )
        return events

    def parse_connection_webhook(self, raw_body: bytes, signature_header: str | None) -> ConnectionWebhookEvent | None:
        if not self._valid_signature(raw_body, signature_header):
            return None
        import json

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

        session_id = payload.get("session_id")
        status = payload.get("status")
        if not session_id or not status:
            return None

        return ConnectionWebhookEvent(
            session_id=session_id,
            status=status,
            phone_number=payload.get("phone_number"),
            display_name=payload.get("display_name"),
            provider_account_id=payload.get("provider_account_id"),
            error=payload.get("error"),
        )


class NotConfiguredProvider(WhatsAppProvider):
    """
    Registered under QR_PROVIDER when QR_PROVIDER_BASE_URL is unset, or when
    it's set but QR_PROVIDER_API_KEY/WEBHOOK_SECRET are missing (see
    registry.py) -- the latter would otherwise surface as a misleading
    "network_error" once the bridge refuses to start without its own copy of
    those secrets (see qr-bridge/src/config.ts).

    Every method fails clearly rather than allowing a QR account to silently
    behave as if it were connected -- see spec requirement: never fake a
    connection.
    """

    supports_qr = True

    def __init__(self, message: str = "QR provider is not configured.", code: str = "not_configured"):
        self._message = message
        self._code = code

    def _unavailable(self):
        raise ProviderError(self._message, code=self._code)

    def start_connection(self, account) -> ConnectionSessionResult:
        self._unavailable()

    def get_connection_status(self, account, session_id):
        self._unavailable()

    def get_qr(self, account, session_id):
        self._unavailable()

    def disconnect(self, account, session_id):
        self._unavailable()

    def get_account_info(self, account) -> ConnectionResult:
        self._unavailable()

    def send_text_message(self, account, to, body):
        self._unavailable()

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None):
        self._unavailable()

    def send_interactive_message(self, account, to, body, buttons):
        self._unavailable()

    def send_template_message(self, account, to, template_name, language_code, components=None):
        self._unavailable()

    def verify_webhook_challenge(self, account, query_params):
        return None

    def validate_webhook_signature(self, account, raw_body, signature_header):
        return False

    def parse_webhook_payload(self, payload):
        return []

    def parse_connection_webhook(self, raw_body, signature_header):
        return None
