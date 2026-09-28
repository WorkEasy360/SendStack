"""
QR/device-linking provider, backed by a separate, self-hosted "QR bridge"
microservice reached over HTTP + webhook.

SendStack does not implement, host, or reverse-engineer any WhatsApp
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

    POST   {base_url}/sessions/{session_id}/numbers/check      # the number filter
           body: {"numbers": [<digits, no "+">, ...]}          # at most NUMBER_CHECK_BATCH
           -> {"results": [{"number": str, "exists": bool, "jid": str | None}]}
           -> 409 {"error", "code": "not_connected"} when the session has no live socket

    GET    {base_url}/sessions/{session_id}/groups             # the group grabber, step one
           -> {"groups": [{"id": str, "subject": str, "member_count": int,
                           "announce": bool, "is_community": bool, "created_at": iso8601 | None}]}

    POST   {base_url}/sessions/{session_id}/groups/members     # the group grabber, step two
           body: {"group_ids": [str, ...]}
           -> {"groups": [{"id": str, "subject": str, "hidden_count": int,
                           "members": [{"phone": <digits>, "jid": str, "name": str, "is_admin": bool}]}]}

Auth: every request carries ``Authorization: Bearer {QR_PROVIDER_API_KEY}``.

Webhooks (bridge -> SendStack) are HMAC-SHA256 over the raw body with
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
from urllib.parse import urlsplit

import requests
from django.conf import settings

from .base import (
    ConnectionResult,
    ConnectionSessionResult,
    ConnectionStatusResult,
    ConnectionWebhookEvent,
    GroupMember,
    GroupMembers,
    GroupSummary,
    NumberLookup,
    ProviderError,
    QRResult,
    SendResult,
    WebhookEvent,
    WebhookEventType,
    WhatsAppProvider,
)

logger = logging.getLogger("sendstack")


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


def _own_media_hosts() -> set[str]:
    """
    Hostnames that serve *this* deployment's media, whoever wrote the link.

    A media link is absolute and is frozen into the template and every message
    copied from it at the moment it is created, so a workspace accumulates
    links from every origin it has ever been reached on: the public domain
    today, a domain it moved off last year, and "127.0.0.1:8000" from a Django
    run outside Docker while the containers were up on the same database. All
    of them name the same file in the same MEDIA_ROOT, so all of them have to
    be recognised as ours.

    The loopback names are here because that is the origin a developer's
    browser stamps in, and it is the one that fails most confusingly: the
    bridge resolves it to *itself* and reports a connection refused that looks
    like the send never left.
    """
    hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    public = getattr(settings, "MEDIA_PUBLIC_BASE_URL", "")
    if public:
        host = urlsplit(public).hostname
        if host:
            hosts.add(host.lower())
    # A host Django is configured to answer for is a host it can have written
    # a media link with. "*" is skipped deliberately -- it means "any host",
    # and treating it as such here would claim someone else's CDN as ours and
    # rewrite a genuinely external attachment into a 404.
    for entry in getattr(settings, "ALLOWED_HOSTS", None) or []:
        host = (entry or "").strip().lstrip(".").lower()
        if host and host != "*":
            hosts.add(host)
    return hosts


def _reachable_link(link: str) -> str:
    """
    The attachment URL as the bridge can actually fetch it.

    Baileys downloads the link from inside the qr-bridge container, so a link
    built for the browser ("http://localhost/media/x.png") resolves to that
    container itself and the send fails. The origin is swapped for the
    internal one on the way out only -- what is stored on the message stays
    the link a person can open.

    The match is on *what the link points at*, not on the exact origin string:
    any host of ours (see _own_media_hosts) serving a path under MEDIA_URL is
    the same file in the same MEDIA_ROOT, and the bridge should fetch it from
    the one origin that resolves for it. Matching the configured public origin
    literally -- as this did -- meant a link carrying any other origin of ours
    was handed over untouched and the send died on a connection refused.

    A link to somewhere that is genuinely not ours is left exactly as it is:
    an attachment hosted on a customer's own CDN must still be fetched from
    that CDN. With no internal origin configured this is a no-op.
    """
    internal = getattr(settings, "MEDIA_INTERNAL_BASE_URL", "")
    if not link or not internal:
        return link

    parts = urlsplit(link)
    # Relative, or not something with an origin to replace ("data:", a bare
    # path). Nothing to do, and nothing safe to guess.
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return link

    media_url = getattr(settings, "MEDIA_URL", "") or "/media/"
    if not parts.path.startswith(media_url):
        return link
    if parts.hostname.lower() not in _own_media_hosts():
        return link

    tail = parts.path
    if parts.query:
        tail = f"{tail}?{parts.query}"
    return internal + tail


class WhatsAppQRBridgeProvider(WhatsAppProvider):
    """Talks to an external QR/device-linking bridge service over HTTP."""

    supports_qr = True

    # False, although `send_interactive_message` below is implemented and the
    # bridge really does relay a native-flow interactive message (see
    # sendInteractive() in qr-bridge/src/providers/baileysProvider.ts).
    #
    # WhatsApp reserves interactive messages for the official Cloud API. A
    # linked-device sender can put one on the wire and have it accepted -- the
    # send succeeds, a provider message id comes back, the row goes to
    # DELIVERED -- and the recipient's phone renders the body text with no
    # buttons under it. Measured on a live connection here: 15 button messages
    # sent, zero taps ever received, and no error anywhere to explain it.
    #
    # Claiming support we do not have is worse than having none, because it is
    # what suppresses the numbered-list fallback in
    # `messaging.services._send_with_buttons`. The customer is asked a question
    # with no way to answer it, and every part of the system reports success.
    # So the flag states what actually reaches a phone, and the send path
    # degrades to numbered options that `automation.services` maps back to the
    # original button ids.
    #
    # RE-MEASURED 2026-09-03, on a QR-linked WhatsApp *Business* account
    # (creds platform `smba`) against Baileys 6.7.24 -- because "it was only
    # tested on a personal number" is the obvious objection to the paragraph
    # above, and it turned out not to be the explanation. All five wire shapes
    # WhatsApp has ever carried buttons in were sent to a real handset. Every
    # one returned HTTP 200 with a provider message id:
    #
    #   native_flow         (interactiveMessage in a viewOnce wrapper)  never arrived
    #   native_flow_v1      (+ messageParamsJson/messageVersion/footer) never arrived
    #   interactive_direct  (interactiveMessage, no wrapper)            never arrived
    #   buttons_legacy      (buttonsMessage)            arrived as plain text
    #   template            (templateMessage)           arrived as plain text
    #
    # Two things follow, and the second is the dangerous one:
    #
    #   1. No envelope renders tappable buttons on a linked device. Being a
    #      Business account does not change this.
    #   2. The three `interactiveMessage` shapes are **silently discarded in
    #      transit** -- the message never reaches the recipient at all, while
    #      every layer reports a successful send. That is strictly worse than
    #      the flattening this flag already prevents: not "asked a question
    #      they cannot answer", but "never asked anything".
    #
    # So flipping this to True would not merely fail to produce buttons; on the
    # shape `send_interactive_message` actually uses it would make campaign
    # messages vanish. Do not flip it without repeating the handset comparison
    # -- qr-bridge/src/config.ts documents how, and the bridge accepts a
    # per-request `format` so the test costs one curl per shape.
    supports_interactive_buttons = False

    # A link button rides on the same native-flow interactive message and
    # therefore does not render either. It degrades to the URL printed in the
    # text, which is the whole of what tapping it would have done.
    supports_url_buttons = False

    # Nor a menu. `interactive.type = "list"` is a Cloud API construct; the
    # older Baileys listMessage shape stopped rendering on modern clients for
    # the same reason buttonsMessage did. It degrades to a numbered list.
    supports_list_messages = False

    # Polls, on the other hand, this connection genuinely does send -- they
    # are a personal/Business-app WhatsApp feature, which is exactly what a
    # linked device is, and Baileys relays them as a native poll the recipient
    # votes in. This is the one message type where a QR number is *more*
    # capable than the official API, which has no poll type at all.
    supports_poll_messages = True

    # Both lookups are things a phone can do and the official API cannot: a
    # linked device asks the same contact-sync query the app itself runs when
    # you add somebody to your address book, and it sees every group the
    # phone is in. Neither is available on a Cloud API number at any version.
    supports_number_check = True
    supports_group_listing = True

    #: Most numbers one bridge call carries. Mirrors
    #: NUMBER_LOOKUP_MAX_PER_REQUEST in qr-bridge/src/providers/base.ts; the
    #: bridge refuses anything larger, so this adapter splits above it.
    NUMBER_CHECK_BATCH = 500

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
                # The bridge names a few of its refusals ("not_connected",
                # "lookup_failed"); anything unnamed is the generic code every
                # existing caller already handles.
                code=str(data.get("code") or "provider_error")[:64],
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

    def send_text_message(self, account, to: str, body: str, operation_id: str = "") -> SendResult:
        return self._send(
            account, {"to": to, "type": "text", "text": {"body": body}}, operation_id=operation_id,
        )

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None,
                           operation_id: str = "") -> SendResult:
        return self._send(
            account,
            {
                "to": to,
                "type": media_type,
                "media": {"link": _reachable_link(media_link), "caption": caption, "filename": filename},
            },
            operation_id=operation_id,
        )

    def send_interactive_message(self, account, to: str, body: str, buttons: list,
                                 operation_id: str = "") -> SendResult:
        if not buttons:
            return SendResult(
                success=False, error="This message has no buttons to send.", code="invalid_request",
            )
        return self._send(
            account,
            {
                "to": to,
                "type": "interactive",
                "interactive": {"body": body, "buttons": buttons},
            },
            operation_id=operation_id,
        )

    def send_poll_message(self, account, to: str, poll_content: dict, operation_id: str = "") -> SendResult:
        """
        Send a native poll.

        ``selectable_count`` is how the multi-answer switch reaches WhatsApp:
        1 lets the recipient pick one option, and the option count lets them
        pick any number. It is computed here rather than in the bridge because
        the poll's own shape is what determines it, and the bridge should not
        have to re-derive a rule this side already knows.
        """
        options = list((poll_content or {}).get("options") or [])
        if len(options) < 2:
            return SendResult(
                success=False, error="A poll needs at least two options.", code="invalid_request",
            )

        return self._send(
            account,
            {
                "to": to,
                "type": "poll",
                "poll": {
                    "question": poll_content.get("question") or "",
                    "options": options,
                    "selectable_count": len(options) if poll_content.get("allow_multiple") else 1,
                },
            },
            operation_id=operation_id,
        )

    def send_template_message(self, account, to, template_name, language_code, components=None,
                              operation_id: str = "") -> SendResult:
        # Session-based (non-Cloud-API) WhatsApp connections do not use
        # Meta's approved-template system -- templates are a Cloud API/BSP
        # concept tied to Meta's messaging limits, not something a
        # personal/Business-app-linked session sends.
        raise ProviderError(
            "Template messages are only available on WhatsApp API (Cloud API) connections.",
            code="not_supported",
        )

    def _send(self, account, payload: dict, *, operation_id: str = "") -> SendResult:
        if not account.provider_session_id:
            return SendResult(
                success=False, error="WhatsApp account has no active session.", code="no_session",
            )

        to = _to_msisdn(payload.get("to"))
        if not to:
            return SendResult(
                success=False, error="Recipient phone number is not a valid number.", code="invalid_recipient",
            )
        payload = {**payload, "to": to}
        if operation_id:
            # The bridge keys its dedup store on this: a second POST with the
            # same send_operation_id returns the first result and does NOT
            # send again. It also lets us ask "what happened to this send?"
            # after a lost response -- see get_send_operation_status.
            payload["send_operation_id"] = operation_id

        started = time.monotonic()
        try:
            data = self._request("POST", f"/sessions/{account.provider_session_id}/messages", json=payload)
        except ProviderError as exc:
            logger.warning(
                "QR send failed account_id=%s to=%s type=%s in %.3fs: %s (code=%s ambiguous=%s)",
                account.pk, to, payload.get("type"), time.monotonic() - started,
                exc.message, exc.code, exc.ambiguous,
            )
            return SendResult(
                success=False, error=exc.message, ambiguous=exc.ambiguous, code=exc.code or "",
            )

        result = SendResult(
            success=data.get("success", False),
            provider_message_id=data.get("provider_message_id"),
            error=data.get("error"),
            ambiguous=bool(data.get("ambiguous")),
            # The bridge's own classification where it sent one. Trimmed and
            # coerced to a string: this reaches a CharField, and it is a value
            # from another service rather than one this codebase produced.
            code=str(data.get("code") or "")[:64],
        )
        logger.info(
            "QR send account_id=%s to=%s type=%s success=%s in %.3fs provider_message_id=%s",
            account.pk, to, payload.get("type"), result.success, time.monotonic() - started,
            result.provider_message_id,
        )
        return result

    def _require_session(self, account) -> str:
        if not account.provider_session_id:
            raise ProviderError("WhatsApp account has no active session.", code="no_session")
        return account.provider_session_id

    def check_numbers(self, account, phones: list[str]) -> list[NumberLookup]:
        session_id = self._require_session(account)
        phones = list(phones)
        results: list[NumberLookup] = []
        for start in range(0, len(phones), self.NUMBER_CHECK_BATCH):
            batch = phones[start:start + self.NUMBER_CHECK_BATCH]
            # Asked by bare digits (see _to_msisdn) and matched back the same
            # way: the bridge echoes each number exactly as it was sent.
            wire = [_to_msisdn(phone) for phone in batch]
            askable = [number for number in wire if number]
            answers: dict[str, dict] = {}
            if askable:
                data = self._request(
                    "POST", f"/sessions/{session_id}/numbers/check", json={"numbers": askable},
                )
                for row in data.get("results") or []:
                    if isinstance(row, dict) and row.get("number") is not None:
                        answers[str(row["number"])] = row
            for phone, number in zip(batch, wire):
                row = answers.get(number) if number else None
                results.append(NumberLookup(
                    phone=phone,
                    exists=bool(row and row.get("exists")),
                    jid=(str(row.get("jid"))[:64] if row and row.get("jid") else None),
                ))
        return results

    def list_groups(self, account) -> list[GroupSummary]:
        session_id = self._require_session(account)
        data = self._request("GET", f"/sessions/{session_id}/groups")
        groups = []
        for row in data.get("groups") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            groups.append(GroupSummary(
                id=str(row["id"])[:64],
                subject=str(row.get("subject") or "")[:255],
                member_count=int(row.get("member_count") or 0),
                announce=bool(row.get("announce")),
                is_community=bool(row.get("is_community")),
                created_at=_parse_iso8601(row.get("created_at")),
            ))
        return groups

    def get_group_members(self, account, group_ids: list[str]) -> list[GroupMembers]:
        session_id = self._require_session(account)
        data = self._request(
            "POST", f"/sessions/{session_id}/groups/members", json={"group_ids": list(group_ids)},
        )
        groups = []
        for row in data.get("groups") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            members = []
            for member in row.get("members") or []:
                if not isinstance(member, dict):
                    continue
                digits = re.sub(r"\D", "", str(member.get("phone") or ""))
                if not digits:
                    continue
                members.append(GroupMember(
                    phone=f"+{digits}",
                    jid=str(member.get("jid") or "")[:64],
                    name=str(member.get("name") or "").strip()[:150],
                    is_admin=bool(member.get("is_admin")),
                ))
            groups.append(GroupMembers(
                id=str(row["id"])[:64],
                subject=str(row.get("subject") or "")[:255],
                members=members,
                hidden_count=int(row.get("hidden_count") or 0),
            ))
        return groups

    def get_send_operation_status(self, account, operation_id: str) -> dict | None:
        """
        Ask the bridge what became of one send, by its ``send_operation_id``.

        The recovery sweep calls this when a worker died after POSTing a
        message but before recording the reply. The bridge answers from its
        dedup store::

            GET /sessions/{session_id}/operations/{operation_id}
            -> 200 {"known": bool, "sent": bool,
                    "provider_message_id": str | None, "ambiguous": bool}
            -> 404 {"known": false}   (never saw this operation id)

        Returns ``{"unreachable": True}`` if the bridge cannot be reached, so
        the sweep marks the message UNKNOWN rather than guessing.
        """
        if not account.provider_session_id or not operation_id:
            return {"known": False, "sent": False}
        try:
            data = self._request(
                "GET", f"/sessions/{account.provider_session_id}/operations/{operation_id}",
            )
        except ProviderError as exc:
            if exc.code == "provider_error" and "404" in (exc.message or ""):
                return {"known": False, "sent": False}
            logger.warning(
                "QR operation lookup failed account_id=%s operation_id=%s: %s",
                account.pk, operation_id, exc.message,
            )
            return {"unreachable": True}
        return {
            "known": bool(data.get("known")),
            "sent": bool(data.get("sent")),
            "provider_message_id": data.get("provider_message_id"),
            "ambiguous": bool(data.get("ambiguous")),
        }

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
                    # Echoed by the bridge on statuses for a send it received a
                    # send_operation_id with -- lets a receipt be tied back to
                    # the right message when the provider_message_id was lost.
                    biz_opaque_callback_data=status_obj.get("send_operation_id"),
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

    # Everything is False, including text and media, which every other
    # provider declares True. That is the honest answer here: this stand-in
    # cannot deliver anything at all, and a capability dict that claimed
    # otherwise would have the composer offer message types against a
    # connection with no bridge behind it.
    supports_text = False
    supports_media = False

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

    def send_text_message(self, account, to, body, operation_id: str = ""):
        self._unavailable()

    def check_numbers(self, account, phones):
        self._unavailable()

    def list_groups(self, account):
        self._unavailable()

    def get_group_members(self, account, group_ids):
        self._unavailable()

    def send_media_message(self, account, to, media_type, media_link, caption=None, filename=None,
                           operation_id: str = ""):
        self._unavailable()

    def send_interactive_message(self, account, to, body, buttons, operation_id: str = ""):
        self._unavailable()

    def send_list_message(self, account, to, body, list_content, operation_id: str = ""):
        self._unavailable()

    def send_poll_message(self, account, to, poll_content, operation_id: str = ""):
        self._unavailable()

    def send_template_message(self, account, to, template_name, language_code, components=None,
                              operation_id: str = ""):
        self._unavailable()

    def verify_webhook_challenge(self, account, query_params):
        return None

    def validate_webhook_signature(self, account, raw_body, signature_header):
        return False

    def parse_webhook_payload(self, payload):
        return []

    def parse_connection_webhook(self, raw_body, signature_header):
        return None
