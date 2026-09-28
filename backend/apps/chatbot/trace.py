"""
The trail one inbound message leaves behind.

Two destinations, written from one call. A structured log line goes to the
worker's stdout for an operator, and a ``ChatbotEvent`` row goes to the
database for the customer's own debug panel -- both carrying the same
``correlation_id``, so a line in a log and a row on a screen can be matched up
without guessing.

Everything here is best-effort. Tracing is how you find out why a reply did
not go out; it must never be the reason one did not.
"""

import logging
import time
import uuid

from .models import ChatbotEvent

logger = logging.getLogger("sendstack")

#: Nothing with a name like this is ever written into an event's metadata,
#: whatever a caller passes. The debug panel is visible to a tenant's own
#: administrators, and a provider token that reached it would be readable by
#: anybody with a login rather than only by whoever holds the .env file.
_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "credential", "signature")

#: A metadata value longer than this is truncated. A retrieved chunk or a model
#: reply can be thousands of characters, and a trace table that stores every
#: one of them in full grows faster than the message table it describes.
MAX_VALUE_CHARS = 600


def new_correlation_id() -> str:
    """One id for one inbound message's whole journey."""
    return uuid.uuid4().hex


def _safe(value, depth=0):
    """A value fit to store: no secrets, bounded size, JSON-serialisable."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {
            str(key): ("[redacted]" if _is_secret(key) else _safe(item, depth + 1))
            for key, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in list(value)[:40]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text[:MAX_VALUE_CHARS] + ("..." if len(text) > MAX_VALUE_CHARS else "")


def _is_secret(key) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


class Trace:
    """
    One message's trail, with the owning ids remembered so callers do not
    repeat them at every step.

    Deliberately not a context manager per step: the steps are not nested, they
    are a sequence, and a `with` block per step would put three lines of
    scaffolding around every one-line decision in the engine.
    """

    __slots__ = ("correlation_id", "tenant_id", "conversation_id", "chatbot_id", "message_id", "_clock", "_rows")

    def __init__(self, *, tenant_id, conversation_id=None, chatbot_id=None, message_id=None, correlation_id=None):
        self.correlation_id = correlation_id or new_correlation_id()
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.chatbot_id = chatbot_id
        self.message_id = message_id
        self._clock = time.monotonic()
        self._rows = []

    def bind(self, *, conversation_id=None, chatbot_id=None, message_id=None) -> None:
        """Fill in an id that was not known when the trace started."""
        if conversation_id is not None:
            self.conversation_id = conversation_id
        if chatbot_id is not None:
            self.chatbot_id = chatbot_id
        if message_id is not None:
            self.message_id = message_id

    def step(self, step, detail="", *, outcome=ChatbotEvent.Outcome.OK, duration_ms=None, **metadata):
        """
        Record one decision. Never raises.

        ``duration_ms`` defaults to the time since the previous step, which is
        what makes the panel show where a slow reply actually went -- almost
        always retrieval or the model call, and almost never where people
        guess.
        """
        now = time.monotonic()
        elapsed = int((now - self._clock) * 1000) if duration_ms is None else int(duration_ms)
        self._clock = now

        try:
            row = ChatbotEvent(
                tenant_id=self.tenant_id,
                conversation_id=self.conversation_id,
                chatbot_id=self.chatbot_id,
                message_id=self.message_id,
                correlation_id=self.correlation_id,
                step=step,
                outcome=outcome,
                detail=(detail or "")[:500],
                metadata=_safe(metadata),
                duration_ms=max(elapsed, 0),
            )
            self._rows.append(row)
        except Exception:  # noqa: BLE001 - a lost trace row is never worth a lost reply
            logger.debug("Could not build a chatbot trace row.", exc_info=True)

        log = logger.info if outcome == ChatbotEvent.Outcome.OK else logger.warning
        log(
            "chatbot step=%s outcome=%s correlation_id=%s tenant_id=%s conversation_id=%s "
            "chatbot_id=%s message_id=%s duration_ms=%s detail=%s",
            step, outcome, self.correlation_id, self.tenant_id, self.conversation_id,
            self.chatbot_id, self.message_id, elapsed, detail,
        )
        return self

    def skipped(self, step, detail="", **metadata):
        return self.step(step, detail, outcome=ChatbotEvent.Outcome.SKIPPED, **metadata)

    def failed(self, step, detail="", **metadata):
        return self.step(step, detail, outcome=ChatbotEvent.Outcome.ERROR, **metadata)

    def flush(self) -> int:
        """
        Write everything recorded so far, in one statement.

        Buffered rather than written per step because a single inbound message
        produces a dozen rows, and a dozen round trips on the hot path is a
        measurable cost paid for nothing -- nobody reads the trail until after
        the reply has gone out.

        The rows carry ids that were bound *after* some of them were built
        (the conversation and the bot are not known at RECEIVED), so they are
        back-filled here rather than being wrong on the screen.
        """
        if not self._rows:
            return 0
        rows, self._rows = self._rows, []
        for row in rows:
            if row.conversation_id is None:
                row.conversation_id = self.conversation_id
            if row.chatbot_id is None:
                row.chatbot_id = self.chatbot_id
            if row.message_id is None:
                row.message_id = self.message_id
        try:
            ChatbotEvent.objects.bulk_create(rows)
            return len(rows)
        except Exception:  # noqa: BLE001
            logger.warning("Could not write the chatbot trace for %s.", self.correlation_id, exc_info=True)
            return 0
