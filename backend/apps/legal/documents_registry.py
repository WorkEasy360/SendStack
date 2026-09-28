"""
The legal documents, their versions, and the placeholders they still carry.

The text lives in ``documents/*.md`` beside this module rather than in the
database or the frontend, for three reasons:

* it is reviewed and changed like code, in a diff somebody can read;
* the version and the text move together in one commit, which is the whole
  point of versioning a contract;
* the frontend renders what the API serves, so there is one copy of the wording
  and not a second one drifting in JSX.

Placeholders (``{{ legal_entity_name }}`` and friends) are filled from settings
at read time. Anything still unset renders as a visible marker rather than as an
empty string, because a Terms of Service with a blank where the company name
should be reads as finished when it is not.
"""

import datetime
import re
from pathlib import Path

from django.conf import settings

DOCUMENTS_DIR = Path(__file__).resolve().parent / "documents"

TERMS = "terms"
PRIVACY = "privacy"
DPA = "dpa"

#: slug -> (title, settings attribute holding the current version)
DOCUMENTS = {
    TERMS: ("Terms of Service", "TERMS_VERSION"),
    PRIVACY: ("Privacy Policy", "PRIVACY_VERSION"),
    DPA: ("Data Processing Agreement", "DPA_VERSION"),
}

#: The documents a customer is asked to accept. The DPA is deliberately not
#: here: it is a contract between two named entities that this deployment
#: cannot yet complete (see documents/dpa.md), and asking somebody to click
#: "I accept" on a document full of blanks would manufacture evidence of an
#: agreement that does not exist.
ACCEPTANCE_REQUIRED = (TERMS, PRIVACY)

#: How an unset placeholder renders. Deliberately loud.
UNSET = "[TO BE COMPLETED]"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def current_version(slug: str) -> str:
    _, setting_name = DOCUMENTS[slug]
    return str(getattr(settings, setting_name, "1.0"))


def _context() -> dict:
    return {
        "legal_entity_name": getattr(settings, "LEGAL_ENTITY_NAME", "") or UNSET,
        "legal_entity_address": getattr(settings, "LEGAL_ENTITY_ADDRESS", "") or UNSET,
        "legal_contact_email": (
            getattr(settings, "LEGAL_CONTACT_EMAIL", "")
            or getattr(settings, "SUPPORT_EMAIL", "")
            or UNSET
        ),
        "governing_law": getattr(settings, "LEGAL_GOVERNING_LAW", "") or UNSET,
        "product_name": "SendStack",
    }


def is_configured() -> bool:
    """
    Whether the operator has filled in the business details these documents need.

    Drives the "unreviewed draft" banner in the UI. Deliberately checks the
    settings and not the rendered text, so a document that happens to contain
    the word "draft" cannot look configured.
    """
    return all(
        getattr(settings, name, "")
        for name in ("LEGAL_ENTITY_NAME", "LEGAL_ENTITY_ADDRESS", "LEGAL_CONTACT_EMAIL", "LEGAL_GOVERNING_LAW")
    )


def _source(slug: str) -> str:
    path = DOCUMENTS_DIR / f"{slug}.md"
    return path.read_text(encoding="utf-8")


def _updated(slug: str) -> str:
    """
    The file's own modification date.

    Not a hand-maintained "last updated" line in the text: that is one more
    thing to forget, and a stale date on a legal document is a small lie.
    """
    path = DOCUMENTS_DIR / f"{slug}.md"
    try:
        stamp = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
    except OSError:
        return ""
    return stamp.strftime("%d %B %Y")


def render(slug: str) -> dict:
    """One document, ready to serve: title, version, markdown body, and its state."""
    if slug not in DOCUMENTS:
        raise KeyError(slug)

    title, _ = DOCUMENTS[slug]
    context = {**_context(), "version": current_version(slug), "updated": _updated(slug)}
    body = _PLACEHOLDER_RE.sub(lambda match: str(context.get(match.group(1), UNSET)), _source(slug))

    return {
        "slug": slug,
        "title": title,
        "version": current_version(slug),
        "updated": context["updated"],
        "body": body,
        # True while any business detail is still missing. The UI shows a
        # banner saying so; hiding it would be the dishonest option.
        "is_draft": not is_configured() or UNSET in body,
        "acceptance_required": slug in ACCEPTANCE_REQUIRED,
    }


def summary() -> list:
    """Every document without its body -- what the acceptance prompt needs."""
    return [
        {
            "slug": slug,
            "title": title,
            "version": current_version(slug),
            "acceptance_required": slug in ACCEPTANCE_REQUIRED,
        }
        for slug, (title, _) in DOCUMENTS.items()
    ]
