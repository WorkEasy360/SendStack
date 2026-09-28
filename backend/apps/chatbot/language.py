"""
Which language the customer is writing in, and what to tell the model about it.

Deliberately deterministic and free: this runs on every inbound message, and a
model call to find out what language a message is in would double the cost of
answering "hi" and add a second point of failure before the first word is read.

Three languages matter here because they are the three that arrive on one
Indian business number in one afternoon:

* **Hindi** written in Devanagari -- unambiguous, decided by the script alone.
* **English** -- Latin script, no Hindi vocabulary.
* **Hinglish** -- Hindi written in Latin script, usually mixed with English
  ("bhai delivery kab hogi?"). This is the one worth detecting, because a
  Hinglish question answered in formal Hindi reads as badly as one answered in
  formal English, and the model will not guess right unless it is told.

Detection is a hint, never a gate: an unrecognised language falls through to
AUTO, which tells the model to reply in whatever the customer used. That is the
correct behaviour for the languages this file has never heard of, which is most
of them.
"""

import re

from .models import Chatbot

#: Devanagari. Present in any quantity, the message is Hindi -- nobody writes a
#: stray Devanagari character into an English sentence by accident.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

#: Hindi function words as people actually type them in Latin script. Function
#: words rather than nouns on purpose: nouns are borrowed in both directions
#: ("delivery", "order", "payment" appear in Hinglish constantly), while "kab",
#: "hai" and "kyun" are only ever Hindi. Matching on borrowed nouns would call
#: every English message Hinglish.
_HINGLISH_MARKERS = frozenset({
    "hai", "hain", "hai", "nahi", "nahin", "kya", "kyu", "kyun", "kaise", "kaisa", "kaisi",
    "kab", "kahan", "kaha", "kitna", "kitne", "kitni", "mujhe", "mera", "meri", "mere",
    "aapka", "aapki", "aapke", "apna", "apni", "tum", "aap", "hum", "hamara", "yeh", "ye",
    "woh", "wo", "bhai", "bhaiya", "didi", "sir", "karo", "kare", "karna", "karta", "karte",
    "chahiye", "chaiye", "milega", "milegi", "hoga", "hogi", "hota", "hoti", "raha", "rahi",
    "diya", "diye", "liya", "lena", "dena", "batao", "bataye", "bolo", "acha", "accha",
    "theek", "thik", "abhi", "jaldi", "please", "krdo", "kardo", "kar", "se", "ko", "ka",
    "ki", "ke", "me", "mein", "par", "aur", "lekin", "phir", "bas", "sab", "kuch", "koi",
})

#: A message this short cannot be classified. "ok", "yes", "9" carry no signal,
#: and guessing from them is how a thread flips language half-way through.
_MIN_TOKENS_FOR_LATIN = 2

#: How many of the words have to be Hindi markers before a Latin-script message
#: counts as Hinglish. One marker in a long English sentence is a coincidence
#: ("please" is on the list because it is ubiquitous in Hinglish, and would
#: otherwise flip plain English requests).
_HINGLISH_MIN_MARKERS = 2
_HINGLISH_MIN_RATIO = 0.18

_WORD = re.compile(r"[a-z]+")


def detect(text: str) -> str:
    """
    The language of one message, as a ``Chatbot.Language`` value.

    Returns AUTO when there is not enough to go on, which is the honest answer
    for "hi", an emoji, or a phone number -- and which leaves whatever the
    session already decided in place.
    """
    stripped = (text or "").strip()
    if not stripped:
        return Chatbot.Language.AUTO

    if _DEVANAGARI.search(stripped):
        return Chatbot.Language.HINDI

    words = _WORD.findall(stripped.lower())
    if len(words) < _MIN_TOKENS_FOR_LATIN:
        return Chatbot.Language.AUTO

    markers = sum(1 for word in words if word in _HINGLISH_MARKERS)
    if markers >= _HINGLISH_MIN_MARKERS or (markers and markers / len(words) >= _HINGLISH_MIN_RATIO):
        return Chatbot.Language.HINGLISH
    return Chatbot.Language.ENGLISH


def resolve(configured: str, detected: str, remembered: str = "") -> str:
    """
    What the reply should actually be written in.

    A configured language always wins: a business that has said "always answer
    in English" has made a decision, and quietly answering in Hindi because one
    customer wrote in it would override it. On AUTO the current message decides,
    falling back to what the thread has been in so far -- which is what keeps a
    Hinglish conversation from switching to English on the one message that was
    just "ok".
    """
    if configured and configured != Chatbot.Language.AUTO:
        return configured
    if detected and detected != Chatbot.Language.AUTO:
        return detected
    return remembered or Chatbot.Language.AUTO


#: What the model is told, per resolved language. Only the non-AUTO cases have
#: an instruction: AUTO's whole point is to say nothing and let the model mirror
#: the customer, which it does well and which no instruction improves.
_INSTRUCTIONS = {
    Chatbot.Language.ENGLISH: "Always reply in English, however the customer writes.",
    Chatbot.Language.HINDI: (
        "Always reply in Hindi, written in Devanagari script, however the customer writes."
    ),
    Chatbot.Language.HINGLISH: (
        "Always reply in Hinglish -- conversational Hindi written in the Latin alphabet, mixed with "
        "English words the way people actually chat. Do not use Devanagari script and do not switch "
        "to formal English."
    ),
}


def instruction(resolved_language: str) -> str:
    """The line added to the system prompt, or "" for AUTO."""
    return _INSTRUCTIONS.get(resolved_language, "")
