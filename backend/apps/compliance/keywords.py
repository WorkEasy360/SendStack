"""
Recognising "stop messaging me" in an inbound message.

Opting out has to work in whatever words the contact happens to use, so this
matches a curated multi-language keyword set rather than a single magic word.
Two rules keep it from firing on ordinary conversation:

  * the phrase must appear as whole words, never as a substring -- otherwise
    "stop" matches "stopover" and "baja" matches "trabajando";
  * the whole message must be short (see MAX_MESSAGE_WORDS). Someone whose
    entire message is "please stop" is opting out; someone who writes a
    paragraph containing "I'll never stop recommending you" is not.

Only unambiguous phrases are in the defaults. "cancel"/"cancelar" are
deliberately absent: on a commerce account they far more often mean "cancel
my order", and a tenant that wants them can add them to their policy.
"""

import unicodedata

#: Longest a message may be, in words, and still be read as an opt-out. Long
#: messages that merely mention a keyword are ordinary conversation.
MAX_MESSAGE_WORDS = 4

#: The id carried by the unsubscribe button appended to campaign messages.
#: Tapping it is an opt-out no matter what the button was labelled, so the
#: label can be translated freely without breaking the behaviour.
OPT_OUT_BUTTON_ID = "opt_out"

STOP_KEYWORDS = frozenset({
    # English
    "stop", "stopall", "stop all", "unsubscribe", "unsub", "unsubscribe me",
    "opt out", "optout", "remove me", "delete me", "no more messages",
    "stop messages", "stop messaging me", "do not message me", "dont message me",
    "leave me alone", "take me off",
    # Hindi / Hinglish
    "band karo", "bandh karo", "band kro", "band kar", "hatao", "mujhe hatao",
    "message band karo", "बंद", "बंद करो", "रोको", "हटाओ", "मुझे हटाओ",
    # Bengali
    "বন্ধ", "বন্ধ করুন",
    # Tamil / Telugu / Marathi
    "நிறுத்து", "ఆపు", "थांबवा",
    # Spanish
    "baja", "dar de baja", "darse de baja", "parar", "no mas mensajes",
    # Portuguese
    "sair", "descadastrar", "pare",
    # French
    "desabonner", "désabonner", "arreter", "arrêter",
    # German
    "abmelden", "stopp",
    # Indonesian / Malay
    "berhenti", "henti",
    # Arabic
    "ايقاف", "إيقاف", "الغاء", "إلغاء", "توقف",
    # Turkish
    "durdur",
    # Russian
    "стоп", "отписаться",
    # Thai
    "หยุด",
})

START_KEYWORDS = frozenset({
    # English
    "start", "unstop", "subscribe", "resubscribe", "resume", "opt in", "optin",
    # Hindi / Hinglish
    "chalu karo", "shuru karo", "suru karo", "शुरू", "शुरू करो", "चालू करो",
    # Spanish / Portuguese
    "empezar", "iniciar", "alta", "comecar", "começar",
    # French / German
    "demarrer", "démarrer", "anmelden",
    # Indonesian / Arabic / Russian
    "mulai", "ابدا", "البدء", "старт", "подписаться",
})


def normalize(text: str) -> str:
    """
    Lowercased words with punctuation and emoji removed.

    NFKC first so that a full-width or decomposed "STOP" folds onto the same
    string as a plain one -- a contact typing from a phone keyboard produces
    either without meaning anything different by it.

    Combining marks are kept alongside letters and digits, which is not a
    detail: `str.isalnum()` is False for them, so dropping them would turn the
    Hindi "बंद करो" into "ब द कर" and quietly break opt-out in every Indic
    script, plus Arabic, Thai and Hebrew -- exactly the languages this list
    exists to cover.
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    kept = [
        character if (character.isalnum() or unicodedata.category(character).startswith("M")) else " "
        for character in folded
    ]
    return " ".join("".join(kept).split())


def _contains_phrase(words: list[str], phrase: str) -> bool:
    """True when `phrase`'s words appear consecutively in `words`."""
    needle = phrase.split()
    if not needle or len(needle) > len(words):
        return False
    return any(
        words[start:start + len(needle)] == needle
        for start in range(len(words) - len(needle) + 1)
    )


def matches(text: str, keywords, extra=()) -> bool:
    """
    Whether this message asks for the action `keywords` stands for.

    `extra` is the tenant's own additions, normalised here rather than at the
    call site so a policy can hold them exactly as they were typed in.
    """
    words = normalize(text).split()
    if not words or len(words) > MAX_MESSAGE_WORDS:
        return False

    candidates = set(keywords)
    candidates.update(normalize(keyword) for keyword in extra or ())
    return any(phrase and _contains_phrase(words, phrase) for phrase in candidates)


def is_stop(text: str, extra=()) -> bool:
    return matches(text, STOP_KEYWORDS, extra)


def is_start(text: str, extra=()) -> bool:
    return matches(text, START_KEYWORDS, extra)
