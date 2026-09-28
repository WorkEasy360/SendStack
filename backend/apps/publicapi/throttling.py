"""
Per-key rate limiting, in three buckets that answer three different questions.

* **requests/minute** protects the web tier. Any call counts.
* **messages/minute** protects the *workers*, which is the resource that
  actually runs out. One API call can enqueue one message or ten thousand, so
  counting requests alone lets a single well-behaved caller bury the send
  queue while looking entirely reasonable on the request graph.
* **campaigns/day** protects everyone downstream of a mistake -- a loop that
  creates a campaign per iteration is a class of bug that costs real money and
  real phone numbers, and a daily ceiling is the only one of the three that
  catches it before the damage.

Fixed windows in Redis, not a token bucket. A fixed window lets a caller spend
its whole minute in the first second, which is a real and accepted weakness;
what it buys is a counter that is one INCR, that needs no read-modify-write,
and that cannot drift when two web workers serve the same key concurrently.
For protecting a queue from an integration, that trade is the right way round.

**Redis is not the authority on whether you may send.** `django_redis` here is
configured with ``IGNORE_EXCEPTIONS`` (see settings.CACHES), so a Redis outage
makes these counters no-op and every request is allowed. That is deliberate:
the credit gate, the plan allowance and the per-number sending limits all sit
behind this and all live in PostgreSQL. Rate limiting failing open degrades
fairness; rate limiting failing closed would take the whole platform down with
one cache blip.
"""

import time

from django.conf import settings
from django.core.cache import cache

from .errors import RATE_LIMITED, PublicAPIError

#: The three buckets: attribute on ApiKey -> (settings default, window seconds).
BUCKETS = {
    "requests_per_minute": ("PUBLIC_API_DEFAULT_REQUESTS_PER_MINUTE", 60),
    "messages_per_minute": ("PUBLIC_API_DEFAULT_MESSAGES_PER_MINUTE", 60),
    "campaigns_per_day": ("PUBLIC_API_DEFAULT_CAMPAIGNS_PER_DAY", 86400),
}


class RateLimitResult:
    """What the caller needs to build the X-RateLimit-* headers."""

    __slots__ = ("bucket", "limit", "remaining", "reset_at", "allowed")

    def __init__(self, bucket, limit, remaining, reset_at, allowed):
        self.bucket = bucket
        self.limit = limit
        self.remaining = remaining
        self.reset_at = reset_at
        self.allowed = allowed

    @property
    def retry_after(self) -> int:
        return max(1, int(self.reset_at - time.time()))

    def headers(self) -> dict:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_at)),
            # Which of the three buckets these numbers describe. Without it,
            # a client hitting the message ceiling sees request headers that
            # say it has plenty of room left and cannot tell why it was cut off.
            "X-RateLimit-Bucket": self.bucket,
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after)
        return headers


def limit_for(api_key, bucket: str) -> int:
    """
    The effective ceiling for this key and bucket.

    0 on the key means "use the deployment default" -- see the note on
    ApiKey.requests_per_minute for why that is not "unlimited".
    """
    setting_name, _ = BUCKETS[bucket]
    configured = getattr(api_key, bucket, 0) or 0
    if configured > 0:
        return configured
    return int(getattr(settings, setting_name, 0) or 0)


def check(api_key, bucket: str, *, cost: int = 1, consume: bool = True) -> RateLimitResult:
    """
    Take `cost` from the bucket and say whether it fitted.

    `consume=False` inspects without spending, which is what the response
    headers on an already-counted request use so a single call is never
    charged twice.
    """
    _, window = BUCKETS[bucket]
    limit = limit_for(api_key, bucket)
    now = time.time()
    window_start = int(now // window) * window
    reset_at = window_start + window

    if limit <= 0:
        # No ceiling configured anywhere. Report it honestly rather than
        # inventing a number: a client that sees a limit of 0 and remaining
        # of 0 would back off forever.
        return RateLimitResult(bucket, limit, limit, reset_at, True)

    cache_key = f"publicapi:rl:{api_key.pk}:{bucket}:{window_start}"

    if not consume:
        used = cache.get(cache_key) or 0
        return RateLimitResult(bucket, limit, limit - used, reset_at, used <= limit)

    # `cache.incr` needs the key to exist, and `add` is the atomic
    # "create only if absent" that makes the pair safe under concurrency: the
    # loser of the race falls through to incr and counts correctly.
    if cache.add(cache_key, cost, timeout=int(reset_at - now) + 1):
        used = cost
    else:
        try:
            used = cache.incr(cache_key, cost)
        except ValueError:
            # The key expired between `add` and `incr` -- the window rolled
            # over underneath us. Start the new window with this request in it.
            cache.set(cache_key, cost, timeout=window)
            used = cost

    return RateLimitResult(bucket, limit, limit - used, reset_at, used <= limit)


def enforce(api_key, bucket: str, *, cost: int = 1) -> RateLimitResult:
    """`check`, but raise the 429 rather than returning it."""
    result = check(api_key, bucket, cost=cost)
    if not result.allowed:
        error = PublicAPIError(
            RATE_LIMITED,
            f"Rate limit exceeded for {bucket.replace('_', ' ')}. Retry in {result.retry_after}s.",
            status_code=429,
            extra={"bucket": bucket, "limit": result.limit, "retry_after": result.retry_after},
        )
        # Carried on the exception so the 429 reports the bucket that actually
        # stopped the request. Without this the response would describe
        # whichever bucket was checked *last before* the failure -- telling a
        # caller blocked by the message ceiling that it has plenty of request
        # budget left, which is true and useless.
        error.rate_limit_headers = result.headers()
        raise error
    return result
