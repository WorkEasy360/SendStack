import json
import logging
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection as db_connection
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import events

logger = logging.getLogger("multisender")

_TICKET_TTL_SECONDS = 60
_TICKET_PREFIX = "multisender:rt-ticket:"

# How long one stream is held open before the client is asked to reconnect.
# A bounded lifetime keeps a worker thread from being owned forever by a tab
# somebody left open in January; EventSource reconnects by itself, so the
# reconnect is invisible.
_MAX_STREAM_SECONDS = 600
_HEARTBEAT_SECONDS = 25


class StreamTicketView(APIView):
    """
    A short-lived, single-use ticket for opening the event stream.

    EventSource cannot send an Authorization header, so the stream has to be
    authenticated by something in the URL. Putting the JWT there would write a
    long-lived credential into every access log and proxy cache; a ticket that
    dies after a minute and cannot be replayed does the same job without
    leaving anything worth stealing behind.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ticket = secrets.token_urlsafe(32)
        cache.set(f"{_TICKET_PREFIX}{ticket}", request.user.pk, _TICKET_TTL_SECONDS)
        return Response({"ticket": ticket, "expires_in": _TICKET_TTL_SECONDS})


def _user_for_ticket(ticket):
    if not ticket:
        return None
    key = f"{_TICKET_PREFIX}{ticket}"
    user_id = cache.get(key)
    if user_id is None:
        return None
    # Single use: a ticket that has been redeemed cannot open a second stream,
    # so one leaking from a log is worth nothing.
    cache.delete(key)

    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id, is_active=True).first()


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def stream(request):
    """
    The live event stream for the caller's tenant.

    A plain Django view rather than a DRF one: the response is an open socket,
    not a serialised object, and DRF's content negotiation has nothing to add.
    """
    user = _user_for_ticket(request.GET.get("ticket"))
    if user is None or not user.tenant_id:
        return StreamingHttpResponse(
            iter([_frame({"type": "error", "data": {"detail": "Invalid or expired ticket."}})]),
            content_type="text/event-stream",
            status=status.HTTP_401_UNAUTHORIZED,
        )

    tenant_id = user.tenant_id
    pubsub = events.subscribe(tenant_id)

    # This connection is about to be held open for minutes. Hanging on to a
    # database connection for all of it would exhaust the pool long before the
    # worker threads ran out; anything the stream needs later opens its own.
    # Never inside a transaction, though -- closing the connection out from
    # under an open atomic block would roll it back, which is exactly what a
    # test (every one of which runs in one) would do.
    if not db_connection.in_atomic_block:
        db_connection.close()

    def generate():
        started = time.monotonic()
        last_beat = started
        events.touch_presence(tenant_id, user.pk)

        yield _frame({"type": "connected", "data": {"tenant": tenant_id, "user": user.pk}})
        if pubsub is None:
            # Say so explicitly rather than holding a silent socket open: the
            # client needs to know to fall back to polling.
            yield _frame({"type": "unavailable", "data": {"detail": "Live updates are not available."}})
            return

        try:
            while time.monotonic() - started < _MAX_STREAM_SECONDS:
                message = pubsub.get_message(timeout=1.0)
                if message and message.get("type") == "message":
                    raw = message["data"]
                    yield f"data: {raw.decode() if isinstance(raw, bytes) else raw}\n\n"

                now = time.monotonic()
                if now - last_beat >= _HEARTBEAT_SECONDS:
                    last_beat = now
                    # Doubles as the presence heartbeat: someone with a stream
                    # open is at their desk, and this is the only signal that
                    # says so without asking the browser to poll for it.
                    events.touch_presence(tenant_id, user.pk)
                    yield ": ping\n\n"
        except GeneratorExit:
            raise
        except Exception:
            logger.warning("Real-time stream for tenant %s ended early.", tenant_id, exc_info=True)
        finally:
            try:
                pubsub.close()
            except Exception:
                pass

    response = StreamingHttpResponse(generate(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    # Belt and braces with the nginx config: some proxies honour this header
    # where they would otherwise buffer the stream into uselessness.
    response["X-Accel-Buffering"] = "no"
    return response


class TypingView(APIView):
    """
    Say that this agent is typing, to everyone else looking at the inbox.

    Published and never stored: by the time a "was typing" row could be read
    back it would already be a lie.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        conversation_id = request.data.get("conversation")
        if not conversation_id:
            return Response({"detail": "A conversation is required."}, status=status.HTTP_400_BAD_REQUEST)

        events.publish(request.user.tenant_id, "typing", {
            "conversation": int(conversation_id),
            "user": request.user.pk,
            "username": request.user.username,
        })
        return Response({"published": True})


class PresenceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"online_user_ids": events.online_user_ids(request.user.tenant_id)})

    def post(self, request):
        """Heartbeat for a client that is not holding a stream open."""
        events.touch_presence(request.user.tenant_id, request.user.pk)
        return Response({"ok": True})


class RealtimeStatusView(APIView):
    """Whether live updates work in this deployment, so the client knows whether to poll."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        available = events.publish(request.user.tenant_id, "ping", {"user": request.user.pk})
        return Response({
            "available": available,
            "poll_interval_seconds": getattr(settings, "REALTIME_FALLBACK_POLL_SECONDS", 15),
        })
