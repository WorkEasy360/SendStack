"""
Official Meta WhatsApp Business Cloud API provider.

This is the only provider SendStack ships with. It talks to Meta's
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

from core.buttons import REPLY, URL, body_with_text_buttons, button_kind, plan_delivery

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

logger = logging.getLogger("sendstack")

GRAPH_API_VERSION = "v20.0"
GRAPH_API_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

REQUEST_TIMEOUT_SECONDS = 15

_MEDIA_TYPES = {"image", "video", "audio", "document", "sticker"}


class WhatsAppCloudAPIProvider(WhatsAppProvider):

    # Credential-based, not QR/pairing-based -- connecting is a single
    # synchronous credential check, so there is no QR step to expose.
    supports_qr = False

    # Interactive messages are a first-class Cloud API message type, and both
    # the reply-button and the link ("call to action URL") shapes render on
    # every current client.
    supports_interactive_buttons = True
    supports_url_buttons = True

    # A menu is `interactive.type = "list"`, which is likewise free-form and
    # not tied to an approved template.
    supports_list_messages = True

    # Polls are not. WhatsApp added them for personal and Business-app users,
    # not for the Cloud API: there is no poll message type in the Graph API at
    # any version, so a Cloud API number cannot send one at all. This is the
    # inverse of the QR provider, which sends polls natively and cannot send
    # buttons -- and it is why the composer asks the capability layer rather
    # than assuming the "official" connection is the more capable one.
    supports_poll_messages = False

    # Call buttons are template-only on the Cloud API. See core.buttons.
    supports_call_buttons = False

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
        except requests.ConnectTimeout as exc:
            # The connection was never established, so nothing was sent. Safe
            # to retry.
            raise ProviderError(
                f"Timed out connecting to WhatsApp Cloud API: {exc}", code="timeout",
            ) from exc
        except requests.Timeout as exc:
            # A read timeout: the request WAS transmitted and we simply never
            # heard back. Meta may or may not have accepted the message. This
            # must be flagged ambiguous so the send is not blindly retried into
            # a duplicate -- the Cloud API has no idempotent submission and no
            # per-message status lookup, so an ambiguous send here becomes
            # RECOVERY_REQUIRED. (The QR bridge path has always done this; the
            # Cloud API path used to fold every timeout into "network_error",
            # which is retryable -- a latent duplicate-send bug.)
            raise ProviderError(
                f"Timed out waiting for WhatsApp Cloud API: {exc}", code="timeout", ambiguous=True,
            ) from exc
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
        # SendStack's access from their Meta Business settings if desired.
        return None

    def _send(self, account, payload: dict, *, operation_id: str = "") -> SendResult:
        url = f"{GRAPH_API_BASE_URL}/{account.phone_number_id}/messages"
        payload.setdefault("messaging_product", "whatsapp")
        payload.setdefault("recipient_type", "individual")
        if operation_id:
            # Echoed back on every status webhook for this message, so a
            # delivery receipt can be tied to the right send even if the
            # provider_message_id below never reached us (worker died first).
            # It is NOT an idempotency key -- Meta has none -- so a retry of an
            # ambiguous Cloud API send is still a duplicate, which is why such
            # a send becomes RECOVERY_REQUIRED rather than being retried.
            payload["biz_opaque_callback_data"] = operation_id

        try:
            data = self._request("POST", url, account, json=payload)
        except ProviderError as exc:
            return SendResult(success=False, error=exc.message, code=exc.code or "", ambiguous=exc.ambiguous)

        messages = data.get("messages") or []
        provider_message_id = messages[0]["id"] if messages else None
        return SendResult(success=True, provider_message_id=provider_message_id)

    def send_text_message(self, account, to: str, body: str, operation_id: str = "") -> SendResult:
        return self._send(
            account,
            {
                "to": to,
                "type": "text",
                "text": {"body": body, "preview_url": True},
            },
            operation_id=operation_id,
        )

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None,
                           operation_id: str = "") -> SendResult:
        if media_type not in _MEDIA_TYPES:
            return SendResult(
                success=False, error=f"Unsupported media type '{media_type}'.", code="not_supported",
            )

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
            operation_id=operation_id,
        )

    def send_interactive_message(self, account, to: str, body: str, buttons: list,
                                 operation_id: str = "") -> SendResult:
        """
        One interactive envelope, plus body text for whatever did not fit in it.

        The Graph API has no payload carrying two kinds of button at once, so a
        mixed list is split by ``core.buttons.plan_delivery``: one kind goes
        out tappable and the rest are written into the body as labelled lines.
        Sharing that function with the composer's delivery preview is what
        makes the text below character-for-character what the author was shown.
        """
        if not buttons:
            return SendResult(
                success=False, error="This message has no buttons to send.", code="invalid_request",
            )

        tappable, spelled_out = plan_delivery(buttons)
        text = body_with_text_buttons(body, spelled_out)
        kind = button_kind(tappable)

        if kind == REPLY:
            interactive = {
                "type": "button",
                "body": {"text": text},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": button["id"], "title": button["title"]}}
                        for button in tappable
                    ]
                },
            }
        elif kind == URL:
            # A single link button is its own interactive type on the Cloud
            # API ("call to action URL"), not a variant of the reply-button
            # payload -- hence the separate branch rather than one shape.
            interactive = {
                "type": "cta_url",
                "body": {"text": text},
                "action": {
                    "name": "cta_url",
                    "parameters": {"display_text": tappable[0]["title"], "url": tappable[0]["url"]},
                },
            }
        else:
            # Nothing tappable is left -- a call button on its own. Sent as
            # plain text rather than refused: an interactive envelope with an
            # empty action is a 400 from Meta, while the labelled number is
            # exactly what a call button was always going to arrive as.
            return self.send_text_message(account, to, text, operation_id=operation_id)

        return self._send(
            account, {"to": to, "type": "interactive", "interactive": interactive},
            operation_id=operation_id,
        )

    def send_list_message(self, account, to: str, body: str, list_content: dict,
                          operation_id: str = "") -> SendResult:
        """
        A tappable menu, as ``interactive.type = "list"``.

        The row shape Meta wants is the one ``core.interactive`` already
        normalises to, with one exception: an empty ``description`` must be
        omitted rather than sent as "", which the API rejects. Section titles
        are omitted on the same rule -- a single unnamed section is legal and
        common, and sending a blank title is not.
        """
        sections = []
        for section in (list_content or {}).get("sections", []):
            rows = [
                {
                    key: value
                    for key, value in (
                        ("id", row["id"]),
                        ("title", row["title"]),
                        ("description", row.get("description") or ""),
                    )
                    if value
                }
                for row in section.get("rows", [])
            ]
            if not rows:
                continue
            entry = {"rows": rows}
            if section.get("title"):
                entry["title"] = section["title"]
            sections.append(entry)

        if not sections:
            return SendResult(
                success=False, error="This message has no list rows to send.", code="invalid_request",
            )

        interactive = {
            "type": "list",
            "body": {"text": body},
            "action": {"button": list_content.get("button") or "View options", "sections": sections},
        }
        if list_content.get("header"):
            interactive["header"] = {"type": "text", "text": list_content["header"]}
        if list_content.get("footer"):
            interactive["footer"] = {"text": list_content["footer"]}

        return self._send(
            account, {"to": to, "type": "interactive", "interactive": interactive},
            operation_id=operation_id,
        )

    def send_template_message(self, account, to, template_name, language_code, components=None,
                              operation_id: str = "") -> SendResult:
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
            operation_id=operation_id,
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
                            biz_opaque_callback_data=status_obj.get("biz_opaque_callback_data"),
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
