"""
Official Meta WhatsApp Business Cloud API provider.

This is the only provider MultiSender ships with. It talks to Meta's
supported, licensed HTTP API (https://developers.facebook.com/docs/whatsapp/cloud-api)
using each account's own phone_number_id / access_token. It deliberately does
NOT implement any unofficial/reverse-engineered WhatsApp Web protocol -- that
would violate WhatsApp's terms of service and risk numbers being banned,
which is not acceptable for a commercial product.
"""

import hashlib
import hmac
import logging

import requests

from core.buttons import REPLY, URL, button_kind

from .base import (
    ConnectionResult,
    ConnectionSessionResult,
    ConnectionStatusResult,
    ProviderError,
    QRResult,
    SendResult,
    WebhookEvent,
    WebhookEventType,
    WhatsAppProvider,
)

logger = logging.getLogger("multisender")

GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

REQUEST_TIMEOUT_SECONDS = 15

_MEDIA_TYPES = {"image", "video", "audio", "document", "sticker"}


class WhatsAppCloudAPIProvider(WhatsAppProvider):

    # Credential-based, not QR/pairing-based -- connecting is a single
    # synchronous credential check, so there is no QR step to expose.
    supports_qr = False

    # Interactive messages are a first-class Cloud API message type.
    supports_interactive_buttons = True

    def _headers(self, account) -> dict:
        token = account.get_access_token()
        if not token:
            raise ProviderError("This account has no access token configured.", code="missing_credentials")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, account, **kwargs) -> dict:
        try:
            # A fresh connection per call, not a persistent requests.Session
            # -- see the matching comment in qr_bridge.py's _request(). A
            # session reused across a Celery worker's whole lifetime, with
            # sends spaced out over time, ends up trying to write into
            # connections the remote server already closed for being idle,
            # which hangs the read for the full timeout instead of erroring.
            response = requests.request(
                method,
                url,
                headers=self._headers(account),
                timeout=REQUEST_TIMEOUT_SECONDS,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach WhatsApp Cloud API: {exc}", code="network_error") from exc

        try:
            data = response.json()
        except ValueError:
            data = {}

        if not response.ok:
            message = data.get("error", {}).get("message", f"HTTP {response.status_code}")
            raise ProviderError(message, code=data.get("error", {}).get("code"))

        return data

    def get_account_info(self, account) -> ConnectionResult:
        if not account.phone_number_id:
            return ConnectionResult(connected=False, error="phone_number_id is not configured.")

        url = f"{GRAPH_API_BASE_URL}/{account.phone_number_id}"
        try:
            data = self._request(
                "GET",
                url,
                account,
                params={"fields": "display_phone_number,verified_name"},
            )
        except ProviderError as exc:
            return ConnectionResult(connected=False, error=exc.message)

        return ConnectionResult(
            connected=True,
            phone_number=data.get("display_phone_number"),
            display_name=data.get("verified_name"),
        )

    def start_connection(self, account) -> ConnectionSessionResult:
        # Cloud API has no async pairing step -- "starting a connection" and
        # "checking the connection" are the same synchronous credential check.
        result = self.get_account_info(account)
        if not result.connected:
            return ConnectionSessionResult(status="ERROR", error=result.error)
        return ConnectionSessionResult(
            status="CONNECTED",
            phone_number=result.phone_number,
            display_name=result.display_name,
        )

    def get_connection_status(self, account, session_id: str | None) -> ConnectionStatusResult:
        result = self.get_account_info(account)
        if not result.connected:
            return ConnectionStatusResult(status="ERROR", error=result.error)
        return ConnectionStatusResult(
            status="CONNECTED",
            phone_number=result.phone_number,
            display_name=result.display_name,
        )

    def get_qr(self, account, session_id: str) -> QRResult:
        raise ProviderError(
            "WhatsApp API connections use credentials, not a QR code.",
            code="not_supported",
        )

    def disconnect(self, account, session_id: str | None) -> None:
        # Nothing to revoke on Meta's side from here -- the user must remove
        # MultiSender's access from their Meta Business settings if desired.
        return None

    def _send(self, account, payload: dict) -> SendResult:
        url = f"{GRAPH_API_BASE_URL}/{account.phone_number_id}/messages"
        payload.setdefault("messaging_product", "whatsapp")
        payload.setdefault("recipient_type", "individual")

        try:
            data = self._request("POST", url, account, json=payload)
        except ProviderError as exc:
            return SendResult(success=False, error=exc.message)

        messages = data.get("messages") or []
        provider_message_id = messages[0]["id"] if messages else None
        return SendResult(success=True, provider_message_id=provider_message_id)

    def send_text_message(self, account, to: str, body: str) -> SendResult:
        return self._send(
            account,
            {
                "to": to,
                "type": "text",
                "text": {"body": body, "preview_url": True},
            },
        )

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None) -> SendResult:
        if media_type not in _MEDIA_TYPES:
            return SendResult(success=False, error=f"Unsupported media type '{media_type}'.")

        media_object = {"link": media_link}
        if caption and media_type in ("image", "video", "document"):
            media_object["caption"] = caption
        if filename and media_type == "document":
            media_object["filename"] = filename

        return self._send(
            account,
            {
                "to": to,
                "type": media_type,
                media_type: media_object,
            },
        )

    def send_interactive_message(self, account, to: str, body: str, buttons: list) -> SendResult:
        kind = button_kind(buttons)

        if kind == REPLY:
            interactive = {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": button["id"], "title": button["title"]}}
                        for button in buttons
                    ]
                },
            }
        elif kind == URL:
            # A single link button is its own interactive type on the Cloud
            # API ("call to action URL"), not a variant of the reply-button
            # payload -- hence the separate branch rather than one shape.
            interactive = {
                "type": "cta_url",
                "body": {"text": body},
                "action": {
                    "name": "cta_url",
                    "parameters": {"display_text": buttons[0]["title"], "url": buttons[0]["url"]},
                },
            }
        else:
            return SendResult(success=False, error="This message has no buttons to send.")

        return self._send(account, {"to": to, "type": "interactive", "interactive": interactive})

    def send_template_message(self, account, to, template_name, language_code, components=None) -> SendResult:
        return self._send(
            account,
            {
                "to": to,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language_code},
                    "components": components or [],
                },
            },
        )

    def verify_webhook_challenge(self, account, query_params: dict) -> str | None:
        mode = query_params.get("hub.mode")
        token = query_params.get("hub.verify_token")
        challenge = query_params.get("hub.challenge")

        if mode == "subscribe" and token and account.webhook_verify_token and hmac.compare_digest(
            token, account.webhook_verify_token
        ):
            return challenge

        return None

    def validate_webhook_signature(self, account, raw_body: bytes, signature_header: str | None) -> bool:
        app_secret = account.get_app_secret()
        if not app_secret or not signature_header or not signature_header.startswith("sha256="):
            return False

        expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        provided = signature_header.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    def parse_webhook_payload(self, payload: dict) -> list[WebhookEvent]:
        events: list[WebhookEvent] = []

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                for status_obj in value.get("statuses", []):
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

                contacts_by_wa_id = {c.get("wa_id"): c for c in value.get("contacts", []) if c.get("wa_id")}

                for message_obj in value.get("messages", []):
                    message_id = message_obj.get("id")
                    if not message_id:
                        continue
                    sender_contact = contacts_by_wa_id.get(message_obj.get("from"))
                    events.append(
                        WebhookEvent(
                            event_type=WebhookEventType.INBOUND_MESSAGE,
                            provider_event_id=message_id,
                            raw={
                                "message": message_obj,
                                "profile_name": (sender_contact or {}).get("profile", {}).get("name", ""),
                            },
                        )
                    )

        return events
