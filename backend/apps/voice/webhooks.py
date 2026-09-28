"""
The carrier's three callbacks into us.

These are plain Django views rather than DRF ones, for the same reason
``tracking.redirect_link`` is: they are public endpoints that a third party
posts form data to and expects XML back, not part of the JSON API. They live
at the root of the URL space (``/voice/...``) because the carrier is given
these URLs literally, and a signature is computed over the URL exactly as
given.

Authentication is two independent halves, and both must hold:
  - the unguessable per-call token in the path says *which* call this is;
  - the carrier's signature over the URL and body says the request is really
    from the carrier.
Neither alone is enough. The token appears in carrier logs and support
tickets; the signature says nothing about which call it belongs to.
"""

import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services
from .models import CallSession, VoiceAgentConfig
from .providers.registry import get_provider

logger = logging.getLogger("sendstack")


def _authorised(session, request, kind: str) -> bool:
    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    if not getattr(settings, "VOICE_VALIDATE_SIGNATURES", True):
        # Off only in development, where the tunnel URL the carrier signed and
        # the URL this process reconstructs can legitimately differ.
        return True

    provider = get_provider(config)
    return provider.validate_signature(config, request, services.callback_url(session, kind))


def _xml(body: str, content_type: str) -> HttpResponse:
    return HttpResponse(body, content_type=content_type)


@csrf_exempt
@require_POST
def answer(request, token):
    """The call connected. Greet the person, or leave a message on a machine."""
    session = get_object_or_404(CallSession, webhook_token=token)
    if not _authorised(session, request, "answer"):
        logger.warning("Rejected voice answer webhook with a bad signature for call %s.", session.pk)
        return HttpResponseForbidden("Invalid signature.")

    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    turn = get_provider(config).parse_turn(request)

    if turn.provider_call_id and not session.provider_call_id:
        # Inbound calls, and outbound ones whose dial response we never saw.
        session.provider_call_id = turn.provider_call_id
        session.save(update_fields=["provider_call_id"])

    body, content_type = services.safe_render(session, services.on_answer(session, turn))
    return _xml(body, content_type)


@csrf_exempt
@require_POST
def turn(request, token):
    """One exchange: what they said, and what the agent says back."""
    session = get_object_or_404(CallSession, webhook_token=token)
    if not _authorised(session, request, "turn"):
        logger.warning("Rejected voice turn webhook with a bad signature for call %s.", session.pk)
        return HttpResponseForbidden("Invalid signature.")

    if not session.is_live:
        # A webhook arriving after the call was ended from our side. Say
        # nothing and close the line rather than starting a new conversation.
        body, content_type = services.safe_render(session, [])
        return _xml(body, content_type)

    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    inbound = get_provider(config).parse_turn(request)
    body, content_type = services.safe_render(session, services.on_turn(session, inbound))
    return _xml(body, content_type)


@csrf_exempt
@require_POST
def incoming(request, token):
    """
    Somebody rang the tenant's number.

    Addressed by a per-tenant token rather than a per-call one, because there
    is no call yet -- this URL is what Twilio is configured with on the number
    itself, and it is the only thing that says which tenant is being rung.
    """
    config = get_object_or_404(VoiceAgentConfig, inbound_token=token)
    provider = get_provider(config)

    if getattr(settings, "VOICE_VALIDATE_SIGNATURES", True):
        url = f"{services.public_base_url()}/voice/incoming/{token}/"
        if not provider.validate_signature(config, request, url):
            logger.warning("Rejected inbound voice webhook with a bad signature for tenant %s.", config.tenant_id)
            return HttpResponseForbidden("Invalid signature.")

    if not (config.is_enabled and config.answer_inbound):
        # Configured on the number but switched off here. Hanging up quietly
        # is better than a Twilio error tone: the caller hears the line close
        # rather than a fault.
        return _xml(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>', provider.content_type,
        )

    turn = provider.parse_turn(request)
    caller_number = (request.POST.get("From") or "").strip()
    session = services.start_inbound_call(
        config, caller_number=caller_number, provider_call_id=turn.provider_call_id,
    )

    body, content_type = services.safe_render(session, services.on_answer(session, turn))
    return _xml(body, content_type)


@csrf_exempt
@require_POST
def status(request, token):
    """
    Lifecycle: ringing, answered, finished, recording ready.

    Answers 204 rather than a document -- the carrier is reporting, not asking
    what to do next, and anything returned here would be ignored.
    """
    session = get_object_or_404(CallSession, webhook_token=token)
    if not _authorised(session, request, "status"):
        logger.warning("Rejected voice status webhook with a bad signature for call %s.", session.pk)
        return HttpResponseForbidden("Invalid signature.")

    config = VoiceAgentConfig.for_tenant(session.tenant_id)
    services.on_status(session, get_provider(config).parse_status(request))
    return HttpResponse(status=204)
