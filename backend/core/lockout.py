"""
Failed-attempt tracking for the endpoints where guessing is the attack.

DRF's throttles are the first line and they count *requests per client*. That
is exactly the wrong axis for credential stuffing: an attacker with a list of
passwords and a pool of addresses spends one request per address and never
trips a per-client rate at all. So this counts the other axis -- attempts
against one account -- and it is deliberately independent of where they came
from.

Three properties are load-bearing:

* **The identifier is hashed and never stored in the clear.** Cache keys end up
  in Redis, in `MONITOR` output and in anyone's dump of it; a list of the
  usernames currently under attack is not something to leave lying there.

* **An unknown username is counted exactly like a known one.** Counting only
  real accounts would make the lockout itself an oracle: lock on the eleventh
  try and the account exists, never lock and it does not. The caller must
  therefore record failures before it knows whether the account is real.

* **It fails open.** The cache is Redis with ``IGNORE_EXCEPTIONS`` on (see
  settings.CACHES), so during a Redis outage every read here returns None and
  every write is dropped. Locking every account out of a running system because
  the cache is down would be a self-inflicted outage; the rate throttles and
  the password hash still stand.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache

#: Failures before the account stops accepting attempts.
DEFAULT_MAX_ATTEMPTS = 10
#: How long it then stops for, and the window failures are counted over.
DEFAULT_LOCKOUT_SECONDS = 15 * 60


def max_attempts() -> int:
    return int(getattr(settings, "AUTH_MAX_FAILED_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))


def lockout_seconds() -> int:
    return int(getattr(settings, "AUTH_LOCKOUT_SECONDS", DEFAULT_LOCKOUT_SECONDS))


def cache_key(scope: str, identifier: str) -> str:
    """
    The cache key for one identifier in one scope, with the identifier hashed.

    Public because it is not only this module's counters that need it: the
    password-reset endpoint keeps its own per-mailbox counter on different
    thresholds (see apps.accounts.views), and it must hash the address the same
    way. A second implementation would be a second chance to leave a list of
    real email addresses sitting in Redis in the clear.
    """
    digest = hashlib.sha256((identifier or "").strip().casefold().encode("utf-8")).hexdigest()
    return f"lockout:{scope}:{digest}"


#: Kept as the old private name so nothing that already imported it breaks.
_key = cache_key


def is_locked(scope: str, identifier: str) -> bool:
    """Whether this identifier has spent its attempts and must wait."""
    if max_attempts() <= 0:
        return False
    return (cache.get(_key(scope, identifier)) or 0) >= max_attempts()


def record_failure(scope: str, identifier: str) -> int:
    """
    Count one failed attempt and return the running total.

    The window does not slide: the first failure sets the expiry and later ones
    do not push it back, so a run of bad guesses is locked out for
    ``lockout_seconds()`` from when it started rather than for as long as the
    attacker keeps trying. A legitimate user who mistyped their password a few
    times is therefore always let back in on a fixed, knowable timer.
    """
    key = _key(scope, identifier)
    if cache.add(key, 1, timeout=lockout_seconds()):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # Expired between the add and the incr. One lost count is immaterial.
        cache.add(key, 1, timeout=lockout_seconds())
        return 1


def clear(scope: str, identifier: str) -> None:
    """Forget this identifier's failures -- called on every success."""
    cache.delete(_key(scope, identifier))
