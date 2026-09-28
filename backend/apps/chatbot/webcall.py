"""
Calling a customer's own HTTP endpoint from inside a conversation.

This is the most dangerous action in the feature and it is small on purpose.
A URL typed into a form by one tenant is fetched *by this deployment's
servers*, from inside its network -- which is the definition of a server-side
request forgery, and the reason "just call requests.get" is not an acceptable
implementation.

What holds it:

* **HTTPS only.** A plaintext call would put whatever the bot sends -- a
  customer's phone number, their question -- on the wire.
* **The resolved address is checked, not the hostname.** A denylist of
  "localhost" and "169.254.169.254" is defeated by any hostname whose DNS
  record points there, which an attacker controls entirely. So the name is
  resolved first and every address it resolves to must be public.
* **The connection is pinned to the address that was checked.** Resolving,
  approving, and then handing the *name* to the HTTP client leaves a window in
  which a second lookup returns something different -- the classic DNS rebind.
  The socket therefore goes to the vetted IP with the hostname carried in the
  Host header and in TLS's SNI.
* **No redirects.** A 302 to ``http://169.254.169.254/`` would walk straight
  past every check above.
* **A timeout and a response cap**, because a conversation is waiting.

The response is handed to the flow as variables, never to the model as
instructions -- see ``actions._call_api``.
"""

import ipaddress
import json
import logging
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import connection as urllib3_connection

logger = logging.getLogger("sendstack")

#: How long the whole call may take. A conversation is waiting on it, and the
#: worker holding it is not doing anything else.
TIMEOUT_SECONDS = 8

#: Most bytes read from a response. An endpoint that streams a gigabyte back
#: must cost this worker a few kilobytes and a log line, not its memory.
MAX_RESPONSE_BYTES = 64 * 1024

#: Most variables one call may set, and how long each may be. A response is
#: attacker-influenced data even when the endpoint is the tenant's own -- it is
#: reached over the internet -- so what it can do to the session is bounded.
MAX_RESPONSE_FIELDS = 20

ALLOWED_METHODS = ("GET", "POST")


class WebCallError(Exception):
    """Raised when the call cannot be made or cannot be trusted. Carries a sentence for the trail."""


def _is_public(address: str) -> bool:
    """Whether one resolved IP is somewhere we are willing to send a request."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local          # 169.254.0.0/16 -- every cloud metadata service
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # Carrier-grade NAT and IPv4-mapped IPv6 both reach places `is_private`
        # does not describe on its own.
        or ip in ipaddress.ip_network("100.64.0.0/10")
        or (ip.version == 6 and ip.ipv4_mapped is not None and not _is_public(str(ip.ipv4_mapped)))
    )


def _resolve(host: str, port: int) -> str:
    """
    The one address this call is allowed to reach.

    Every address the name resolves to must be public, not merely the first:
    a name that resolves to both a public address and 127.0.0.1 would otherwise
    be approved and then connected to whichever the resolver felt like.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise WebCallError(f"'{host}' could not be resolved.") from exc

    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise WebCallError(f"'{host}' could not be resolved.")
    for address in addresses:
        if not _is_public(address):
            raise WebCallError(f"'{host}' resolves to an address inside this network and cannot be called.")
    return addresses[0]


#: Serialises the connection pinning below.
#:
#: The pin works by swapping a module-level function in urllib3 for the
#: duration of one request, and a second thread doing the same thing at the
#: same time would either restore the wrong function or -- far worse -- have
#: its request pinned to the *other* call's vetted address. Celery's default
#: prefork pool runs one task per process and would never hit this, but a
#: deployment running ``--pool=threads`` or ``--pool=gevent`` would, and an SSRF
#: guard that holds only under one pool setting is not a guard.
_PIN_LOCK = threading.Lock()


class _PinnedAdapter(HTTPAdapter):
    """
    Connects to one vetted IP while presenting the original hostname.

    ``urllib3`` resolves the URL's host itself, so pinning has to happen at the
    point it creates the socket. Overriding the module-level hook for the
    duration of one request keeps TLS verification against the *hostname*
    intact -- pinning by rewriting the URL to the IP would break certificate
    validation, which is a worse trade than the problem it solves.

    Held under ``_PIN_LOCK``: this is a global mutation, and one outbound call
    per worker at a time is an acceptable cost for a rare action with an
    eight-second ceiling.
    """

    def __init__(self, host: str, address: str, **kwargs):
        self._host = host
        self._address = address
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        with _PIN_LOCK:
            original = urllib3_connection.create_connection

            def pinned(address, *args, **kw):
                host, port = address
                return original((self._address if host == self._host else host, port), *args, **kw)

            urllib3_connection.create_connection = pinned
            try:
                return super().send(request, **kwargs)
            finally:
                urllib3_connection.create_connection = original


def validate_url(url: str) -> str:
    """Check a URL at save time, so a broken endpoint is a form error rather than a silent bot."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise WebCallError("The URL must start with https://.")
    if not parts.hostname:
        raise WebCallError("That URL has no host.")
    if parts.username or parts.password:
        raise WebCallError("Credentials in the URL are not supported. Use a header instead.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def call(url: str, *, method="GET", headers=None, payload=None) -> dict:
    """
    Make the call and return ``{"status", "data"}``.

    ``data`` is the decoded JSON object when the response is one, and
    ``{"body": "<text>"}`` otherwise -- so a flow reading ``{{api.body}}`` works
    against an endpoint that returns plain text, rather than the action simply
    failing.
    """
    method = (method or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise WebCallError(f"'{method}' is not a supported method.")

    clean = validate_url(url)
    parts = urlsplit(clean)
    port = parts.port or 443
    address = _resolve(parts.hostname, port)

    session = requests.Session()
    session.mount("https://", _PinnedAdapter(parts.hostname, address))
    try:
        response = session.request(
            method,
            clean,
            headers={"Accept": "application/json", **(headers or {})},
            json=payload if method == "POST" else None,
            timeout=TIMEOUT_SECONDS,
            # A redirect is a second URL that nothing above has checked.
            allow_redirects=False,
            stream=True,
        )
        body = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True) or b""
    except requests.Timeout as exc:
        raise WebCallError("The endpoint did not answer in time.") from exc
    except requests.RequestException as exc:
        raise WebCallError("The endpoint could not be reached.") from exc
    finally:
        session.close()

    if len(body) > MAX_RESPONSE_BYTES:
        raise WebCallError("The endpoint returned more data than this action accepts.")
    if response.is_redirect:
        raise WebCallError("The endpoint redirected, which is not supported here.")

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = {"body": text[:1000]}
    if not isinstance(data, dict):
        data = {"body": text[:1000]}

    return {"status": response.status_code, "data": data}


def flatten(data: dict, prefix: str = "api") -> dict:
    """
    Turn a response body into flat, bounded variables.

    Only scalars at the top level and one level down, which covers the shape
    real endpoints return (``{"order": {"status": "shipped"}}``) without letting
    a deeply nested response write a hundred variables.
    """
    out = {}
    for key, value in list(data.items())[:MAX_RESPONSE_FIELDS]:
        name = f"{prefix}_{key}".lower()
        if isinstance(value, dict):
            for sub_key, sub_value in list(value.items())[:MAX_RESPONSE_FIELDS]:
                if isinstance(sub_value, (str, int, float, bool)) or sub_value is None:
                    out[f"{name}_{sub_key}".lower()] = sub_value
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[name] = value
        if len(out) >= MAX_RESPONSE_FIELDS:
            break
    return out
