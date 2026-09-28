"""
What a tenant-supplied URL has to be before this deployment will send anybody to it.

Two places take a URL somebody typed and turn it into a redirect a stranger
follows: the click-tracking short links at ``/r/<code>`` and the click-to-chat
links at ``/c/<code>``. A redirect is the browser acting on our origin's word,
so the scheme matters as much as the destination:

* ``javascript:`` in a ``Location`` header is not universally executed by modern
  browsers, but it has been, extensions and embedded webviews are their own
  question, and the same string reaches an ``href`` in the campaign preview
  where it certainly is.
* ``data:`` and ``blob:`` let the destination *be* the payload -- an attacker
  hosts nothing and still serves a page that appears to come from a link this
  business sent.
* ``file:`` and ``vbscript:`` are the same story on the platforms that honour
  them.

So the check is an allowlist of two schemes rather than a list of the dangerous
ones, which is the only form that stays correct as browsers grow new ones.

This is deliberately *not* the only defence: the URL rewriter's own pattern
already matches nothing but ``http://`` and ``https://``. That pattern is one
regex away from being widened by someone adding a feature, and this runs at the
moment of the redirect, on whatever is actually stored -- including rows written
before this existed.
"""

import re
from urllib.parse import urlsplit

#: The only two schemes a redirect may use.
ALLOWED_REDIRECT_SCHEMES = ("http", "https")

#: Control characters and whitespace. A browser strips these while parsing, so
#: "java\nscript:alert(1)" is a working javascript: URL to it and an unknown
#: scheme to a naive `startswith` check. Removing them before the scheme is read
#: is what makes the allowlist meaningful.
_STRIPPABLE = re.compile(r"[\x00-\x20\x7f]")


def normalize_redirect_url(url):
    """
    Return ``url`` cleaned up and safe to redirect to, or ``None``.

    ``None`` means "do not send anyone here", and every caller is expected to
    treat it as a dead link rather than to redirect anyway.
    """
    if not url or not isinstance(url, str):
        return None

    cleaned = _STRIPPABLE.sub("", url).strip()
    if not cleaned:
        return None

    try:
        parts = urlsplit(cleaned)
    except ValueError:
        # urlsplit raises on things like an IPv6 literal with no closing
        # bracket. A URL we cannot even parse is not one we will hand over.
        return None

    if parts.scheme.lower() not in ALLOWED_REDIRECT_SCHEMES:
        return None
    if not parts.netloc:
        # "http:///etc/passwd" and "https:" parse but point nowhere.
        return None
    if "@" in parts.netloc:
        # Embedded credentials, and the oldest phishing trick there is:
        # https://www.yourbank.com@evil.example reads as the bank to a person
        # and resolves to the attacker. Legitimate tracked links never have
        # them, so refusing costs nothing.
        return None
    if not parts.hostname:
        return None

    return cleaned


def is_safe_redirect_url(url) -> bool:
    return normalize_redirect_url(url) is not None
