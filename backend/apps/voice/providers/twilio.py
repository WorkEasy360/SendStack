"""
Twilio Programmable Voice.

Written against `requests` and the raw REST/TwiML surface rather than the
`twilio` SDK, for the same reason `apps.ai.providers.anthropic` is: what is
used here is two POSTs, an HMAC, and an XML document, and a vendored SDK would
be a dependency to keep current for no behaviour this needs.

The whole agent runs on Twilio's turn-based verbs -- `<Gather input="speech">`
does the speech recognition, `<Say>` does the speech synthesis, and each turn
of the conversation is one HTTP request to us. That fits a WSGI Django app
exactly, with no socket held open and no new service to run. The cost is the
turn boundary: Twilio waits for the caller to stop talking before it posts,
so the agent cannot interrupt or be interrupted mid-sentence the way a
media-stream agent can.
"""

import base64
import hashlib
import hmac
import logging
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

import requests
from django.conf import settings

from .base import (
    CallHandle,
    CallStatusUpdate,
    Hangup,
    InboundTurn,
    Listen,
    Pause,
    Speak,
    Transfer,
    VoiceProvider,
    VoiceProviderError,
)

logger = logging.getLogger("sendstack")

API_BASE = "https://api.twilio.com/2010-04-01"

# Twilio's speech model tuned for telephone-band audio. Materially more
# accurate than the default on a real call, and supported for the locales this
# is likely to run in (including en-IN).
SPEECH_MODEL = "phone_call"

# Twilio abandons a webhook that has not responded in 15 seconds and fails the
# call. Everything the engine does inside one turn -- retrieval, the model
# call, the tool round-trips -- has to fit comfortably inside that.
WEBHOOK_BUDGET_SECONDS = 15

_STATUS_MAP = {
    "queued": "QUEUED",
    "initiated": "QUEUED",
    "ringing": "RINGING",
    "in-progress": "IN_PROGRESS",
    "completed": "COMPLETED",
    "busy": "BUSY",
    "no-answer": "NO_ANSWER",
    "failed": "FAILED",
    "canceled": "CANCELED",
}

# Voices that carry their own language. Passing `language` alongside one of
# these is at best ignored and at worst overrides the accent the tenant chose,
# so it is only sent for Twilio's own basic voices.
_SELF_LANGUAGED_VOICE_PREFIXES = ("Polly.", "Google.")


class TwilioVoiceProvider(VoiceProvider):
    name = "TWILIO"
    content_type = "text/xml"

    # ------------------------------------------------------------------
    # Placing and ending calls
    # ------------------------------------------------------------------

    def place_call(self, config, session, *, answer_url: str, status_url: str) -> CallHandle:
        account_sid = config.get_account_sid()
        auth_token = config.get_auth_token()
        if not account_sid or not auth_token:
            raise VoiceProviderError("Twilio credentials are not configured.", code="not_configured")
        if not config.from_number:
            raise VoiceProviderError("No Twilio 'from' number is configured.", code="not_configured")

        payload = [
            ("To", session.to_number),
            ("From", config.from_number),
            ("Url", answer_url),
            ("Method", "POST"),
            ("StatusCallback", status_url),
            ("StatusCallbackMethod", "POST"),
            ("Timeout", str(getattr(settings, "VOICE_RING_TIMEOUT_SECONDS", 40))),
        ]
        # Repeated key: Twilio wants one StatusCallbackEvent per event.
        for event in ("initiated", "ringing", "answered", "completed"):
            payload.append(("StatusCallbackEvent", event))

        if config.record_calls:
            payload.append(("Record", "true"))
            payload.append(("RecordingStatusCallback", status_url))
            payload.append(("RecordingStatusCallbackMethod", "POST"))

        # Answering-machine detection costs a few seconds of dead air on every
        # call, including the ones a human picks up, so it is only switched on
        # when the tenant has actually written something to leave on a machine.
        if config.voicemail_message.strip():
            payload.append(("MachineDetection", "DetectMessageEnd"))
            payload.append(("MachineDetectionTimeout", "15"))

        url = f"{API_BASE}/Accounts/{account_sid}/Calls.json"
        try:
            response = requests.post(
                url,
                auth=(account_sid, auth_token),
                data=payload,
                timeout=getattr(settings, "VOICE_REQUEST_TIMEOUT_SECONDS", 15),
            )
        except requests.Timeout as exc:
            # A timeout is the one case where the phone may already be
            # ringing. Saying so stops the caller retrying and dialling twice.
            raise VoiceProviderError(
                f"Timed out asking Twilio to dial: {exc}", code="timeout", ambiguous=True,
            ) from exc
        except requests.RequestException as exc:
            raise VoiceProviderError(f"Could not reach Twilio: {exc}", code="network_error") from exc

        if response.status_code >= 300:
            raise VoiceProviderError(
                f"Twilio refused the call: {self._error_detail(response)}",
                code=f"http_{response.status_code}",
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise VoiceProviderError("Twilio returned a response that was not JSON.") from exc

        return CallHandle(provider_call_id=data.get("sid", ""), status=data.get("status", ""))

    def end_call(self, config, session) -> None:
        account_sid = config.get_account_sid()
        auth_token = config.get_auth_token()
        if not (account_sid and auth_token and session.provider_call_id):
            return

        url = f"{API_BASE}/Accounts/{account_sid}/Calls/{session.provider_call_id}.json"
        try:
            response = requests.post(
                url,
                auth=(account_sid, auth_token),
                data={"Status": "completed"},
                timeout=getattr(settings, "VOICE_REQUEST_TIMEOUT_SECONDS", 15),
            )
        except requests.RequestException as exc:
            # Best effort by design: this is called while tidying up after a
            # call that has usually already ended on its own.
            logger.warning("Could not hang up Twilio call %s: %s", session.provider_call_id, exc)
            return

        if response.status_code >= 300:
            logger.warning(
                "Twilio refused to hang up call %s: %s", session.provider_call_id, self._error_detail(response),
            )

    @staticmethod
    def _error_detail(response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text[:200]
        return data.get("message") or data.get("detail") or str(data)[:200]

    # ------------------------------------------------------------------
    # TwiML
    # ------------------------------------------------------------------

    def render(self, directives, *, voice: str, language: str) -> str:
        parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<Response>"]
        for directive in directives:
            parts.append(self._render_one(directive, voice=voice, language=language))
        parts.append("</Response>")
        return "".join(parts)

    def _render_one(self, directive, *, voice: str, language: str) -> str:
        if isinstance(directive, Speak):
            return self._say(directive.text, voice=voice, language=language)

        if isinstance(directive, Pause):
            return f"<Pause length={quoteattr(str(directive.seconds))}/>"

        if isinstance(directive, Listen):
            attrs = [
                ("input", "speech"),
                ("action", directive.action_url),
                ("method", "POST"),
                # "auto" ends the turn when the speaker stops rather than after
                # a fixed pause -- the single biggest thing that makes the
                # exchange feel like a conversation instead of a form.
                ("speechTimeout", "auto"),
                ("timeout", str(directive.timeout_seconds)),
                ("language", directive.language or language),
                ("speechModel", SPEECH_MODEL),
                # Without this Twilio silently falls through to the next verb
                # when nobody speaks, and the agent never learns there was
                # silence. With it, silence arrives as an empty SpeechResult
                # and the engine can re-prompt or give up deliberately.
                ("actionOnEmptyResult", "true"),
            ]
            if directive.hints:
                attrs.append(("hints", directive.hints))

            rendered = "".join(self._say(line, voice=voice, language=language) for line in directive.lines)
            return f"<Gather {self._attrs(attrs)}>{rendered}</Gather>"

        if isinstance(directive, Transfer):
            lines = "".join(self._say(line, voice=voice, language=language) for line in directive.lines)
            attrs = [("timeout", str(directive.timeout_seconds))]
            if directive.caller_id:
                attrs.append(("callerId", directive.caller_id))
            return f"{lines}<Dial {self._attrs(attrs)}>{escape(directive.number)}</Dial>"

        if isinstance(directive, Hangup):
            lines = "".join(self._say(line, voice=voice, language=language) for line in directive.lines)
            return f"{lines}<Hangup/>"

        raise VoiceProviderError(f"Twilio cannot render {type(directive).__name__}.", code="unsupported_directive")

    @staticmethod
    def _attrs(pairs) -> str:
        return " ".join(f"{key}={quoteattr(str(value))}" for key, value in pairs)

    def _say(self, text: str, *, voice: str, language: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        attrs = [("voice", voice)] if voice else []
        if voice and not voice.startswith(_SELF_LANGUAGED_VOICE_PREFIXES) and language:
            attrs.append(("language", language))
        # The text is model output going straight into an XML document, so it
        # is escaped rather than trusted -- an ampersand in a product name is
        # otherwise a malformed TwiML document and a dropped call.
        rendered = escape(text)
        return f"<Say {self._attrs(attrs)}>{rendered}</Say>" if attrs else f"<Say>{rendered}</Say>"

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def validate_signature(self, config, request, url: str) -> bool:
        """
        Twilio's scheme: HMAC-SHA1 over the full URL with every POST parameter
        appended in sorted key order, keyed by the account's auth token.

        The URL must be exactly the one Twilio was given, including scheme and
        query string. Behind nginx the request looks like plain HTTP to Django,
        which is why the caller passes the reconstructed public URL in rather
        than letting this read `request.build_absolute_uri()`.
        """
        auth_token = config.get_auth_token()
        signature = request.headers.get("X-Twilio-Signature", "")
        if not auth_token or not signature:
            return False

        payload = url
        for key in sorted(request.POST.keys()):
            payload += key + request.POST[key]

        expected = base64.b64encode(
            hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def parse_turn(self, request) -> InboundTurn:
        confidence = request.POST.get("Confidence")
        try:
            confidence = float(confidence) if confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None

        return InboundTurn(
            speech=(request.POST.get("SpeechResult") or "").strip(),
            confidence=confidence,
            answered_by=(request.POST.get("AnsweredBy") or "").strip(),
            provider_call_id=(request.POST.get("CallSid") or "").strip(),
        )

    def parse_status(self, request) -> CallStatusUpdate:
        raw_status = (request.POST.get("CallStatus") or "").strip().lower()
        duration = request.POST.get("CallDuration") or request.POST.get("RecordingDuration") or "0"
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 0

        error = ""
        if request.POST.get("ErrorCode"):
            error = f"Twilio error {request.POST['ErrorCode']}: {request.POST.get('ErrorMessage', '')}".strip()

        return CallStatusUpdate(
            status=_STATUS_MAP.get(raw_status, ""),
            provider_call_id=(request.POST.get("CallSid") or "").strip(),
            duration_seconds=duration,
            recording_url=(request.POST.get("RecordingUrl") or "").strip(),
            error=error[:500],
        )


def signed_url(base_url: str, params: dict) -> str:
    """A callback URL with its query string, built the one way signatures expect."""
    return f"{base_url}?{urlencode(params)}" if params else base_url
