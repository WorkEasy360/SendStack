r"""
Minting, parsing and verifying an API key.

The shape is ``ss_live_<prefix><secret>``:

    ss_live_7f3a91c2 4b8e0d5a91c37e6f2b840195cd7ae3f1
    \______/\______/ \______________________________/
      env    prefix              secret

* the **environment** says which side of the fence this key is on. ``live``
  keys send real messages; ``test`` keys are refused by the send endpoints, so
  a customer can point a staging deployment at production and find out by
  reading a 403 rather than by messaging their customers;
* the **prefix** is stored in clear and indexed. It is what a lookup is done
  on, what the dashboard shows in the list, and what somebody can quote in a
  support ticket without handing over their credentials;
* the **secret** is 128 bits of ``secrets.token_hex`` and is stored only as a
  SHA-256 hash. It is shown to the user exactly once, at creation.

**Why SHA-256 and not Argon2**, when passwords in this project deliberately use
Argon2: a slow KDF exists to make guessing a *low-entropy human-chosen* string
expensive. This secret is 128 bits from a CSPRNG, so guessing it is already
impossible and the slow hash buys nothing -- while costing ~50ms of CPU on
*every API request*, which at any real request rate is the whole platform. The
property that matters here is that a database dump contains nothing usable,
and a one-way hash of an unguessable value gives exactly that.
"""

import hashlib
import hmac
import secrets

#: Split out so the environment half is a whole, greppable token rather than a
#: prefix built by string concatenation at three different call sites.
LIVE_PREFIX = "ss_live_"
TEST_PREFIX = "ss_test_"

ENVIRONMENT_PREFIXES = {
    "live": LIVE_PREFIX,
    "test": TEST_PREFIX,
}

#: Hex characters in each half. 8 hex = 32 bits of prefix, which is plenty to
#: be unique per tenant and short enough to read out loud; 32 hex = 128 bits
#: of secret, which is the part that has to resist guessing.
PREFIX_LENGTH = 8
SECRET_LENGTH = 32


def generate(environment: str = "live") -> tuple[str, str, str]:
    """
    Mint a new key.

    Returns ``(full_key, prefix, hashed_secret)``. The caller stores the second
    and third and shows the first to the user once -- there is deliberately no
    function here that can turn the stored pair back into the first.
    """
    try:
        env_prefix = ENVIRONMENT_PREFIXES[environment]
    except KeyError:
        raise ValueError(f"Unknown key environment: {environment!r}") from None

    prefix = secrets.token_hex(PREFIX_LENGTH // 2)
    secret = secrets.token_hex(SECRET_LENGTH // 2)
    return f"{env_prefix}{prefix}{secret}", prefix, hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def parse(raw: str):
    """
    Split a presented key into ``(environment, prefix, secret)``.

    Returns ``None`` for anything that is not shaped like one of our keys --
    including a JWT, which matters because both arrive in the same
    ``Authorization: Bearer`` header and this is what tells them apart. Being
    unparseable is not an error worth distinguishing from being wrong: both
    end at the same 401, and a caller that could tell them apart would have a
    way to ask whether a given prefix exists.
    """
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    for environment, env_prefix in ENVIRONMENT_PREFIXES.items():
        if not raw.startswith(env_prefix):
            continue
        body = raw[len(env_prefix):]
        if len(body) != PREFIX_LENGTH + SECRET_LENGTH:
            return None
        prefix, secret = body[:PREFIX_LENGTH], body[PREFIX_LENGTH:]
        if not _is_hex(prefix) or not _is_hex(secret):
            return None
        return environment, prefix, secret
    return None


def verify(secret: str, hashed_secret: str) -> bool:
    """Constant-time comparison, so a timing signal cannot walk the hash."""
    return hmac.compare_digest(hash_secret(secret), hashed_secret or "")


def looks_like_key(raw: str) -> bool:
    """Whether this credential is ours to handle at all. See `parse`."""
    return isinstance(raw, str) and raw.strip().startswith(tuple(ENVIRONMENT_PREFIXES.values()))


def masked(environment: str, prefix: str) -> str:
    """What the dashboard and the logs show in place of a key: ``ss_live_7f3a91c2…``."""
    return f"{ENVIRONMENT_PREFIXES.get(environment, LIVE_PREFIX)}{prefix}…"


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
