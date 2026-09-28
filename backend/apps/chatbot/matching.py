"""
Deciding whether one rule answers one message.

Pure functions over plain data: a rule is a dict (which is what a published
version stores) and a message is the text plus the button id. Keeping this
free of models is what lets the rule tester in the API run the *same* code the
engine runs, rather than an approximation of it that drifts.

The one thing that needs care here is the regular expression. A pattern is
written by a customer and run against text written by *their* customer, which
is the classic shape of a catastrophic-backtracking denial of service. See
``_regex_matches``.
"""

import logging
import re

from .models import ChatbotRule

logger = logging.getLogger("sendstack")

#: Longest pattern a rule may carry. Not a security control on its own -- the
#: timeout below is -- but a pattern longer than this is a mistake rather than
#: an intention, and refusing it at save time is a better error than a slow one
#: at match time.
MAX_PATTERN_LENGTH = 500

#: Text handed to a regular expression is truncated to this. Backtracking cost
#: grows with the subject as well as the pattern, and no rule needs to look
#: past the first two thousand characters of a WhatsApp message.
MAX_REGEX_SUBJECT = 2000

#: Compiled patterns, keyed by (pattern, flags). Rules are evaluated on every
#: inbound message and recompiling the same handful of patterns each time is
#: pure waste. Bounded so a tenant with a thousand rules cannot grow this
#: without limit inside a long-lived worker.
_MAX_CACHED_PATTERNS = 512
_compiled: dict = {}


class PatternError(ValueError):
    """Raised for a pattern that cannot be used. Carries a sentence for the author."""


def compile_pattern(pattern: str, *, case_sensitive: bool = False):
    """
    Compile one rule pattern, or raise ``PatternError`` with a readable reason.

    Called at save time as well as at match time, so a broken pattern is a form
    error rather than a bot that silently never matches.
    """
    if len(pattern or "") > MAX_PATTERN_LENGTH:
        raise PatternError(f"A pattern may be at most {MAX_PATTERN_LENGTH} characters.")

    flags = 0 if case_sensitive else re.IGNORECASE
    key = (pattern, flags)
    cached = _compiled.get(key)
    if cached is not None:
        return cached

    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise PatternError(f"That is not a valid pattern: {exc}.") from exc

    if len(_compiled) >= _MAX_CACHED_PATTERNS:
        _compiled.clear()
    _compiled[key] = compiled
    return compiled


def _regex_matches(pattern: str, text: str, *, case_sensitive: bool) -> bool:
    """
    Whether a customer-authored pattern matches, without letting it hang a worker.

    Python's ``re`` has no timeout, so the defence is to bound the *subject*
    rather than the clock: catastrophic backtracking is driven by input length,
    and a pattern that is merely slow on 2,000 characters is not slow enough to
    matter. A pattern that still manages to be pathological costs one worker
    one message, which is why ``search`` is also wrapped -- an exception here
    must fall through to the next rule, not end the conversation.
    """
    try:
        compiled = compile_pattern(pattern, case_sensitive=case_sensitive)
        return bool(compiled.search(text[:MAX_REGEX_SUBJECT]))
    except PatternError:
        # A stored pattern that no longer compiles (edited by hand in the
        # admin, restored from an old export). It matches nothing rather than
        # taking the bot down.
        logger.warning("Chatbot rule has an invalid pattern and was skipped: %r", pattern[:120])
        return False
    except Exception:  # noqa: BLE001 - never let one rule end a conversation
        logger.exception("Chatbot rule pattern failed while matching.")
        return False


def keywords(pattern: str) -> list:
    """The words a KEYWORD rule answers to, from a comma- or newline-separated list."""
    parts = re.split(r"[,\n]", pattern or "")
    return [part.strip() for part in parts if part.strip()]


def _word_present(needle: str, haystack: str) -> bool:
    """
    Whether ``needle`` appears in ``haystack`` as whole words.

    Whole words rather than a substring, for the reason the compliance stop-word
    list uses the same rule: a "price" keyword that fires on "surprise" is a bot
    answering a question nobody asked. Multi-word keywords are matched as a
    phrase with the same boundaries at each end.
    """
    escaped = re.escape(needle)
    # \b does not work against a non-word character at the edge (a keyword of
    # "?" or an emoji), so the boundary is only asserted where the edge
    # character is a word character in the first place.
    left = r"\b" if needle[:1].isalnum() else ""
    right = r"\b" if needle[-1:].isalnum() else ""
    return bool(re.search(f"{left}{escaped}{right}", haystack))


def rule_matches(rule: dict, *, text: str, button_id: str = "") -> bool:
    """
    Whether this rule claims this message.

    ``rule`` is the dict shape a published version stores:
    ``{match_type, pattern, case_sensitive, ...}``. Live ``ChatbotRule``
    instances are converted by ``as_dict`` before they get here, so there is
    exactly one matcher rather than one for drafts and one for published bots.
    """
    match_type = rule.get("match_type") or ChatbotRule.MatchType.CONTAINS
    pattern = (rule.get("pattern") or "").strip()
    case_sensitive = bool(rule.get("case_sensitive"))

    if match_type == ChatbotRule.MatchType.ANY:
        return True

    if match_type == ChatbotRule.MatchType.BUTTON:
        # Matched on the id and never on the label. A label is what the
        # customer saw and can be reworded; the id is the hook, and
        # `core.buttons` keeps it stable across a rename for exactly this.
        return bool(button_id) and bool(pattern) and button_id == pattern

    if not pattern:
        return False

    haystack = text if case_sensitive else (text or "").lower()
    needle = pattern if case_sensitive else pattern.lower()

    if match_type == ChatbotRule.MatchType.EXACT:
        return haystack.strip() == needle.strip()
    if match_type == ChatbotRule.MatchType.CONTAINS:
        return needle in haystack
    if match_type == ChatbotRule.MatchType.STARTS_WITH:
        return haystack.strip().startswith(needle)
    if match_type == ChatbotRule.MatchType.ENDS_WITH:
        return haystack.strip().endswith(needle)
    if match_type == ChatbotRule.MatchType.KEYWORD:
        words = keywords(pattern if case_sensitive else pattern.lower())
        return any(_word_present(word, haystack) for word in words)
    if match_type == ChatbotRule.MatchType.REGEX:
        return _regex_matches(pattern, text or "", case_sensitive=case_sensitive)
    return False


def first_match(rules, *, text: str, button_id: str = ""):
    """
    The highest-priority rule that claims this message, or None.

    ``rules`` must already be in priority order -- the published snapshot is
    written that way and ``ChatbotRule.Meta.ordering`` produces it for drafts,
    so this never re-sorts and never disagrees with what the rule list on
    screen showed.
    """
    for rule in rules:
        if not rule.get("is_active", True):
            continue
        if rule_matches(rule, text=text, button_id=button_id):
            return rule
    return None


def as_dict(rule) -> dict:
    """One ``ChatbotRule`` in the shape a published version stores."""
    return {
        "id": rule.pk,
        "name": rule.name,
        "match_type": rule.match_type,
        "pattern": rule.pattern,
        "case_sensitive": rule.case_sensitive,
        "priority": rule.priority,
        "actions": rule.actions or [],
        "cooldown_seconds": rule.cooldown_seconds,
        "is_active": rule.is_active,
    }
