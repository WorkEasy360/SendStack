"""
Provider abstraction for placing and steering phone calls.

No code outside this package should know how a specific carrier works. The
call engine (``apps.voice.services``) decides *what should happen next* and
says so as a list of directives -- speak this, then listen; transfer; hang up
-- and the provider turns that into whatever document its carrier speaks.
That split is what keeps the agent, the tools and the transcript free of
TwiML, and it is the same shape ``apps.whatsapp.providers`` uses.

The directive set is deliberately tiny. Everything a turn-based voice agent
does is "say something", "say something and then wait for a reply", "put this
call on a human", or "stop" -- a richer vocabulary would be a carrier feature
list leaking into the engine.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class VoiceProviderError(Exception):
    """Raised when a carrier call fails in a way callers must handle."""

    def __init__(self, message, *, code=None, ambiguous=False):
        super().__init__(message)
        self.message = message
        self.code = code
        #: True when the request may already have reached the carrier, so we
        #: cannot tell whether the phone is ringing. Callers must not retry an
        #: ambiguous dial -- doing so risks calling somebody twice.
        self.ambiguous = ambiguous


# ---------------------------------------------------------------------------
# Directives: what the engine wants to happen next
# ---------------------------------------------------------------------------

@dataclass
class Speak:
    """Say something. Nothing is expected back."""

    text: str


@dataclass
class Listen:
    """
    Say the lines, then wait for the person to speak and post what they said.

    The prompt is part of this directive rather than a separate `Speak` so the
    carrier can arm the recogniser *before* the audio starts playing -- that is
    what lets somebody interrupt the agent instead of waiting politely for it
    to finish.
    """

    action_url: str
    lines: list = field(default_factory=list)
    timeout_seconds: int = 6
    language: str = "en-IN"
    hints: str = ""


@dataclass
class Transfer:
    """Bridge this call to a person and step out of the way."""

    number: str
    caller_id: str = ""
    timeout_seconds: int = 30
    lines: list = field(default_factory=list)


@dataclass
class Hangup:
    """End the call, optionally saying goodbye first."""

    lines: list = field(default_factory=list)


@dataclass
class Pause:
    """Wait, for the beep on an answering machine or a beat before speaking."""

    seconds: int = 1


# ---------------------------------------------------------------------------
# What comes back from the carrier
# ---------------------------------------------------------------------------

@dataclass
class CallHandle:
    """The carrier's identity for a call we just asked for."""

    provider_call_id: str
    status: str = ""


@dataclass
class InboundTurn:
    """What the carrier heard, normalised."""

    speech: str = ""
    confidence: float | None = None
    #: Set on the first webhook when answering-machine detection is on:
    #: "human", "machine_start", "machine_end_beep", "fax", "unknown".
    answered_by: str = ""
    provider_call_id: str = ""


@dataclass
class CallStatusUpdate:
    """A lifecycle event: ringing, answered, finished."""

    #: One of ``CallSession.Status`` values, already mapped.
    status: str
    provider_call_id: str = ""
    duration_seconds: int = 0
    recording_url: str = ""
    error: str = ""


class VoiceProvider(ABC):
    """One carrier."""

    name = ""
    #: Content type of the documents `render` produces.
    content_type = "application/xml"

    @abstractmethod
    def place_call(self, config, session, *, answer_url: str, status_url: str) -> CallHandle:
        """Ask the carrier to dial. Raises VoiceProviderError if it will not."""

    @abstractmethod
    def end_call(self, config, session) -> None:
        """Hang up a call that is still up. Best-effort: a finished call is not an error."""

    @abstractmethod
    def render(self, directives, *, voice: str, language: str) -> str:
        """Turn engine directives into the carrier's call-control document."""

    @abstractmethod
    def validate_signature(self, config, request, url: str) -> bool:
        """Whether this webhook really came from the carrier."""

    @abstractmethod
    def parse_turn(self, request) -> InboundTurn:
        """Read a speech-result webhook."""

    @abstractmethod
    def parse_status(self, request) -> CallStatusUpdate:
        """Read a call-status webhook."""


class NotConfiguredProvider(VoiceProvider):
    """
    Stands in for a carrier that has not been set up.

    Returned instead of None so that "no credentials" fails at the point of
    use with a sentence a person can act on, rather than as an AttributeError
    three frames away.
    """

    name = "NOT_CONFIGURED"

    def __init__(self, message="The voice provider is not configured.", code="not_configured"):
        self._message = message
        self._code = code

    def _fail(self):
        raise VoiceProviderError(self._message, code=self._code)

    def place_call(self, config, session, *, answer_url, status_url):
        self._fail()

    def end_call(self, config, session):
        self._fail()

    def render(self, directives, *, voice, language):
        self._fail()

    def validate_signature(self, config, request, url):
        return False

    def parse_turn(self, request):
        self._fail()

    def parse_status(self, request):
        self._fail()
