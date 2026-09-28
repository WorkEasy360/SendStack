"""
Where this server is willing to send a request that a *tenant* chose the address for.

Almost every outbound call in this codebase goes to an address the deployment
fixed: Anthropic's endpoint, Meta's Graph API, Razorpay, the QR bridge on the
internal Docker network. Two do not. `AIAgentConfig.base_url` and
`VoiceAgentConfig.base_url` exist so a customer can point the agent at their own
OpenAI-compatible endpoint -- Groq, Azure, a self-hosted vLLM or Ollama -- and
whatever they type is where the server then makes an authenticated POST.

That is a server-side request forgery primitive unless something checks it. A
tenant administrator who types

    http://169.254.169.254/latest/meta-data/iam/security-credentials/

is asking this server to read the cloud instance's own credentials from an
address only this server can reach, and the provider clients report a failing
response body back in their error message -- so the answer comes back out
through the UI. `http://postgres:5432`, `http://redis:6379` and
`http://qr-bridge:4001` are the same shape against the internal network.

    THE HARD PART IS THAT LOCALHOST IS A DOCUMENTED, LEGITIMATE VALUE.

``http://localhost:11434/v1`` is Ollama, and it is offered in the product's own
help text. So this cannot simply be "reject anything private" -- that breaks the
self-hosted case the feature was built for. It is instead a default-deny with a
deployment-level escape hatch:

* Public addresses: allowed.
* Private, loopback, link-local, multicast and reserved addresses: refused,
  **unless** the host appears in ``OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS``.

An operator running Ollama beside the app sets one environment variable and
names that host. Nobody gets the whole private range by accident, and a tenant
cannot grant it to themselves -- the allowlist is deployment configuration, not
a tenant setting.

Checked in two places on purpose:

* in the serializer, so somebody typing a bad URL is told immediately; and
* in the provider client, immediately before the request goes out, because
  that is the only check a row written before this module existed passes
  through, and the only one a DNS record that changed after it was saved has
  to get past.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings

#: The only schemes an outbound provider call may use. An allowlist rather than
#: a list of the bad ones, for the same reason core.urls_safety uses one.
ALLOWED_SCHEMES = ("http", "https")


class OutboundURLNotAllowed(ValueError):
    """A tenant-supplied URL this deployment refuses to fetch."""


def _allowed_internal_hosts() -> set:
    configured = getattr(settings, "OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS", None) or []
    return {str(host).strip().lower() for host in configured if str(host).strip()}


def _is_blocked_address(address) -> bool:
    """
    Whether an IP is one only this server, or this server's network, can reach.

    ``is_global`` alone is not enough: it is False for the addresses that
    matter here but the individual predicates are what make a failure
    explainable, and 0.0.0.0/8 and the IPv4-mapped IPv6 forms are the ones a
    naive check misses.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolve(host: str) -> list:
    """
    Every address `host` currently resolves to.

    All of them, not the first: a name that answers with one public and one
    private address would otherwise pass the check and then be connected to on
    whichever the resolver handed the HTTP client.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        # A name that does not resolve is not a security problem -- the request
        # will simply fail -- and refusing to save a URL because DNS was
        # briefly unavailable would be worse than letting it through to a
        # connection error the customer can read.
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def check_outbound_url(url: str, *, field: str = "base_url") -> str:
    """
    Return `url` unchanged, or raise `OutboundURLNotAllowed` saying why not.

    The message is written for the tenant administrator who typed it, and says
    what to do about it -- an operator has to add the host to the deployment's
    allowlist -- without naming any other internal address as a suggestion.
    """
    candidate = (url or "").strip()
    if not candidate:
        return candidate

    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise OutboundURLNotAllowed(
            f"{field} must start with http:// or https://."
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise OutboundURLNotAllowed(f"{field} has no host in it.")

    if host in _allowed_internal_hosts():
        return candidate

    if getattr(settings, "OUTBOUND_URL_ALLOW_PRIVATE", False):
        return candidate

    # A literal IP is checked as written; a name is checked against everything
    # it resolves to.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    addresses = [literal] if literal is not None else _resolve(host)
    for address in addresses:
        if _is_blocked_address(address):
            raise OutboundURLNotAllowed(
                f"{field} points at an address inside this server's own network, which is not "
                "allowed. Use a public endpoint, or ask the operator of this deployment to add "
                "this host to OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS."
            )

    return candidate


def outbound_url_problem(url: str, *, field: str = "base_url") -> str:
    """`check_outbound_url` as a message-or-empty-string, for serializer validation."""
    try:
        check_outbound_url(url, field=field)
    except OutboundURLNotAllowed as exc:
        return str(exc)
    return ""
