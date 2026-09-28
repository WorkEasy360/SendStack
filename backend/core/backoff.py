"""
How long to wait before trying an external call again.

Every retry in this codebase used a clean doubling -- 30s, 60s, 120s -- which
is right in shape and wrong in one specific, expensive way: it is the *same*
clean doubling for every task at once.

When a provider goes down, a campaign's thousand in-flight messages fail
within a second or two of each other. All thousand then wait exactly 30
seconds and retry within the same second. If the provider is still down they
all wait 60 and do it again. The retry policy that exists to be gentle
delivers, in practice, a synchronised thousand-request burst every time the
delay elapses -- which is the traffic shape most likely to keep a struggling
provider struggling, and most likely to look like an attack from the far end.

Jitter breaks the convoy up. The wait is a random point inside the window
rather than the window's edge, so the same thousand messages spread their
retries across the whole interval and the provider sees a trickle recovering
instead of a wall arriving.

"Full jitter" -- uniform between zero and the exponential ceiling -- is used
because it spreads the widest, and because the *first* retry being sometimes
almost immediate is a feature here: a provider blip that lasted two seconds is
then survived by the messages that drew a short delay, rather than every one
of them waiting the full thirty.

A floor keeps that from becoming a hot loop: a draw of 0.1s against a provider
that is genuinely down would spend a retry for nothing.
"""

import random

#: Never retry sooner than this, however low the draw. Long enough that a
#: retry is not a second attempt at the same instant, short enough to be
#: invisible against a real network round trip.
MIN_DELAY_SECONDS = 1.0

#: Never wait longer than this, however many retries have been spent. With
#: bounded retry counts (three, in every task here) the ceiling is rarely
#: reached; it exists so that raising max_retries later cannot silently
#: produce a job that sleeps for hours.
MAX_DELAY_SECONDS = 600.0


def retry_delay(retries: int, base: float = 30.0, *, cap: float = MAX_DELAY_SECONDS) -> float:
    """
    Seconds to wait before attempt number `retries` + 1.

    `base` doubles per retry to give the ceiling for this attempt, and the
    actual wait is drawn uniformly below it:

        retries=0, base=30  ->  somewhere in (1, 30]
        retries=1, base=30  ->  somewhere in (1, 60]
        retries=2, base=30  ->  somewhere in (1, 120]

    The mean is therefore about half the old fixed value, and the spread is
    what the caller is actually buying.
    """
    attempt = max(0, int(retries))
    # Bounded before the shift so a large `retries` cannot produce a number
    # too big to be a delay at all.
    ceiling = min(float(base) * (2 ** min(attempt, 16)), float(cap))
    return max(MIN_DELAY_SECONDS, random.uniform(0.0, ceiling))
