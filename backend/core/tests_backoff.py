"""
Retry delays are spread out rather than synchronised.

The property being tested is statistical, so the tests are written as bounds
and distributions rather than exact values -- a test that asserted a particular
random number would be a test of the seed.
"""

from django.test import SimpleTestCase

from core.backoff import MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, retry_delay


class RetryDelayTests(SimpleTestCase):
    def test_the_ceiling_doubles_with_each_retry(self):
        """
        Later attempts may wait longer, which is the exponential half.

        Asserted over many draws rather than one, because any single draw from
        a wider window can legitimately be small.
        """
        first = max(retry_delay(0, base=30) for _ in range(200))
        third = max(retry_delay(2, base=30) for _ in range(200))

        self.assertLessEqual(first, 30.0)
        self.assertGreater(third, 30.0)
        self.assertLessEqual(third, 120.0)

    def test_delays_are_spread_not_identical(self):
        """
        The whole point: a thousand messages failing together must not all come
        back at the same instant.
        """
        draws = [retry_delay(1, base=30) for _ in range(200)]

        self.assertGreater(len(set(draws)), 100, "delays are not being jittered")
        # And they should actually fill the window rather than cluster.
        self.assertLess(min(draws), 20.0)
        self.assertGreater(max(draws), 40.0)

    def test_never_shorter_than_the_floor(self):
        """
        A draw near zero would be a retry at the same instant, which is a hot
        loop against a provider that is already refusing.
        """
        for _ in range(500):
            self.assertGreaterEqual(retry_delay(0, base=30), MIN_DELAY_SECONDS)

    def test_never_longer_than_the_cap(self):
        """
        Bounded even if somebody raises max_retries a long way later.

        Without the cap, retry 20 on a 30s base is a delay of a year.
        """
        for retries in (0, 3, 10, 50, 1000):
            with self.subTest(retries=retries):
                self.assertLessEqual(retry_delay(retries, base=30), MAX_DELAY_SECONDS)

    def test_negative_retries_are_treated_as_the_first_attempt(self):
        self.assertLessEqual(retry_delay(-5, base=30), 30.0)

    def test_the_mean_is_about_half_the_window(self):
        """
        Full jitter, not "exponential plus a little noise".

        This is what makes a brief provider blip survivable: roughly half the
        held messages come back in under fifteen seconds rather than every one
        of them waiting the full thirty.
        """
        draws = [retry_delay(0, base=30) for _ in range(2000)]
        mean = sum(draws) / len(draws)

        self.assertGreater(mean, 10.0)
        self.assertLess(mean, 20.0)


class CallSiteTests(SimpleTestCase):
    """Every retry in the codebase goes through the helper."""

    def test_no_task_still_uses_a_bare_exponential_countdown(self):
        """
        Guards the pattern, not one file.

        The old form was `countdown=30 * (2**self.request.retries)`. A new task
        copied from an old one is how it comes back, and it comes back
        silently: the retry still works, it is just synchronised again.
        """
        import re
        from pathlib import Path

        from django.conf import settings

        pattern = re.compile(r"countdown\s*=\s*\d+\s*\*\s*\(\s*2\s*\*\*")
        offenders = []
        for path in Path(settings.BASE_DIR).glob("apps/*/tasks.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)

        self.assertEqual(
            offenders, [],
            f"these tasks retry on an unjittered exponential: {offenders}. Use core.backoff.retry_delay.",
        )
