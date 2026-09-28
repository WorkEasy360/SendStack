"""
Measure what the API actually costs, per endpoint, before and after a change.

    python manage.py benchmark_api --runs 20
    python manage.py benchmark_api --runs 20 --json > baseline.json
    python manage.py benchmark_api --runs 20 --compare baseline.json

For every endpoint it reports wall time (p50/p95/p99) **and the number of SQL
queries the request ran**. Both numbers are needed and they answer different
questions: time tells you what a customer feels on this dataset, query count
tells you whether the endpoint will still be usable on a dataset ten times
larger. An endpoint that runs 3 queries in 40ms is fine; one that runs 61
queries in 40ms is an N+1 that has simply not met a big enough tenant yet.

The requests go through Django's test client rather than HTTP on purpose. The
number wanted here is the application's own cost -- routing, permissions,
serialization, SQL -- with no nginx, no TLS handshake and no network in it,
because those are the parts a code change cannot move. Absolute latency
against a real socket will be higher; the *ratios* and the query counts are
what this is for.

It authenticates as the largest seeded tenant's owner, which is the honest
case: measuring the tenant with four contacts would report that everything is
fast and prove nothing.
"""

import json
import statistics
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries
import contextlib

from django.test import Client
from django.test.utils import CaptureQueriesContext, setup_test_environment, teardown_test_environment

from apps.campaigns.models import Campaign
from apps.tenants.models import Tenant

#: (label, method, path template). `{campaign}` is filled in with a real
#: campaign id belonging to the benchmarked tenant.
ENDPOINTS = (
    ("dashboard-report", "GET", "/api/v1/reports/dashboard/"),
    ("daily-report", "GET", "/api/v1/reports/messages/daily/"),
    ("contacts-list", "GET", "/api/v1/contacts/contacts/"),
    ("contacts-list-p5", "GET", "/api/v1/contacts/contacts/?page=5"),
    # Deep OFFSET. PostgreSQL still walks every skipped row, so this is where
    # page-number pagination stops being free -- and it is a page a customer
    # reaches by scrolling, not an exotic query.
    ("contacts-deep-page", "GET", "/api/v1/contacts/contacts/?page=90&page_size=50"),
    ("contacts-search", "GET", "/api/v1/contacts/contacts/?search=Priya"),
    ("contacts-filtered", "GET", "/api/v1/contacts/contacts/?subscribed=true&lifecycle_status=ENGAGED"),
    ("campaigns-list", "GET", "/api/v1/campaigns/campaigns/"),
    ("campaign-report", "GET", "/api/v1/reports/campaigns/{campaign}/"),
    ("messages-list", "GET", "/api/v1/messaging/messages/"),
    ("messages-by-campaign", "GET", "/api/v1/messaging/messages/?campaign={campaign}"),
    ("messages-deep-page", "GET", "/api/v1/messaging/messages/?page=200&page_size=50"),
    ("devices-list", "GET", "/api/v1/whatsapp/accounts/"),
    ("templates-list", "GET", "/api/v1/templates/templates/"),
    ("billing-usage", "GET", "/api/v1/billing/usage/"),
    ("billing-plans", "GET", "/api/v1/billing/plans/"),
    ("notifications-list", "GET", "/api/v1/notifications/"),
    ("inbox-conversations", "GET", "/api/v1/inbox/conversations/"),
    ("me", "GET", "/api/v1/auth/me/"),
)


@contextlib.contextmanager
def _throttles_lifted():
    """
    Raise every rate limit out of the way for the duration of a benchmark.

    340 requests in a few seconds is far past the 300/min a real user is
    allowed, and without this every measurement after the first endpoint is a
    measurement of DRF returning 429.

    The rates are raised rather than set to ``None`` on purpose: at ``None``
    DRF skips the throttle entirely and the cache round trip a real request
    pays disappears from the timings. At a huge rate the throttle still runs,
    still reads and writes Redis, and simply never refuses.

    Overriding the *setting* does not work here. ``SimpleRateThrottle`` binds
    ``THROTTLE_RATES`` to the settings dict object once, at import, so a
    reloaded ``api_settings`` leaves the class still pointing at the original
    dict. The class attribute is therefore what has to be swapped.
    """
    from rest_framework.throttling import SimpleRateThrottle

    original = SimpleRateThrottle.THROTTLE_RATES
    SimpleRateThrottle.THROTTLE_RATES = {name: "1000000/min" for name in original}
    try:
        yield
    finally:
        SimpleRateThrottle.THROTTLE_RATES = original


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


class Command(BaseCommand):
    help = "Measure per-endpoint latency and SQL query count against the seeded dataset."

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=15, help="Timed requests per endpoint.")
        parser.add_argument("--warmup", type=int, default=3, help="Untimed requests first, to fill caches.")
        parser.add_argument("--tenant", default="", help="Slug to benchmark. Default: the one with most messages.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable results on stdout.")
        parser.add_argument("--compare", default="", help="Path to an earlier --json run to diff against.")
        parser.add_argument(
            "--keep-throttles",
            action="store_true",
            help="Leave rate limiting on. Off by default -- 20 runs x 17 endpoints trips the user rate "
                 "limit within seconds and every subsequent measurement is of a 429.",
        )

    def handle(self, *args, **options):
        # The test client addresses the app as "testserver", which a real
        # deployment's ALLOWED_HOSTS rightly does not contain -- without this
        # every request is a 400 from CommonMiddleware and the benchmark
        # measures the rejection path. Torn down again so a command that runs
        # inside a longer-lived process leaves settings as it found them.
        setup_test_environment()
        try:
            if options["keep_throttles"]:
                self._run(options)
            else:
                with _throttles_lifted():
                    self._run(options)
        finally:
            teardown_test_environment()

    def _run(self, options):
        user = self._user(options["tenant"])
        # A real bearer token, not `force_login`: DRF here authenticates with
        # SendStackJWTAuthentication and nothing else, so a session cookie
        # would benchmark a wall of 401s.
        client = Client(HTTP_AUTHORIZATION=f"Bearer {self._token(user)}")

        campaign_id = (
            Campaign.objects.filter(tenant=user.tenant).values_list("pk", flat=True).first() or 0
        )

        results = []
        for label, method, template in ENDPOINTS:
            path = template.format(campaign=campaign_id)
            result = self._measure(client, label, method, path, options["runs"], options["warmup"])
            results.append(result)
            if not options["json"]:
                self._print_row(result)

        if options["json"]:
            self.stdout.write(json.dumps({"tenant": user.tenant.slug, "results": results}, indent=2))
            return

        if options["compare"]:
            self._compare(results, options["compare"])

    # -- measurement -----------------------------------------------------

    def _measure(self, client, label, method, path, runs, warmup):
        send = getattr(client, method.lower())

        for _ in range(warmup):
            send(path)

        # Query count is captured on a single dedicated request rather than
        # averaged: it is a property of the code path, not a distribution, and
        # capturing every timed request would have the capture overhead land
        # inside the timings.
        reset_queries()
        with CaptureQueriesContext(connection) as captured:
            probe = send(path)
        queries = len(captured)

        # One timed request with the cache emptied first. Without it a cached
        # endpoint reports the cost of a Redis GET and nothing else, which is
        # true of the second visitor and a lie about the first -- and it is the
        # first whose query the database actually has to run.
        cache.clear()
        started = time.perf_counter()
        send(path)
        cold_ms = (time.perf_counter() - started) * 1000.0

        durations = []
        status = probe.status_code
        for _ in range(runs):
            started = time.perf_counter()
            response = send(path)
            durations.append((time.perf_counter() - started) * 1000.0)
            status = response.status_code

        return {
            "endpoint": label,
            "path": path,
            "status": status,
            "queries": queries,
            "cold_ms": round(cold_ms, 1),
            "p50_ms": round(_percentile(durations, 0.50), 1),
            "p95_ms": round(_percentile(durations, 0.95), 1),
            "p99_ms": round(_percentile(durations, 0.99), 1),
            "mean_ms": round(statistics.fmean(durations), 1),
        }

    # -- output ----------------------------------------------------------

    def _print_row(self, r):
        if not hasattr(self, "_printed_header"):
            self.stdout.write("")
            self.stdout.write(
                f"{'endpoint':<24}{'status':>7}{'queries':>9}{'cold ms':>10}"
                f"{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}"
            )
            self.stdout.write("-" * 77)
            self._printed_header = True

        style = self.style.SUCCESS if r["status"] < 400 else self.style.ERROR
        # A page that runs more queries than it has rows is the shape of an
        # N+1, so it is called out rather than left for somebody to notice.
        flag = "  <- N+1?" if r["queries"] > 25 else ""
        self.stdout.write(
            f"{r['endpoint']:<24}{style(str(r['status'])):>7}{r['queries']:>9}{r['cold_ms']:>10}"
            f"{r['p50_ms']:>9}{r['p95_ms']:>9}{r['p99_ms']:>9}{flag}"
        )

    def _compare(self, results, path):
        try:
            with open(path, encoding="utf-8") as handle:
                baseline = {r["endpoint"]: r for r in json.load(handle)["results"]}
        except (OSError, ValueError, KeyError) as exc:
            raise CommandError(f"Could not read baseline {path}: {exc}") from exc

        self.stdout.write("")
        self.stdout.write(f"{'endpoint':<24}{'queries':>18}{'p95 ms':>20}")
        self.stdout.write("-" * 62)
        for current in results:
            before = baseline.get(current["endpoint"])
            if before is None:
                continue
            self.stdout.write(
                f"{current['endpoint']:<24}"
                f"{before['queries']:>8} -> {current['queries']:<7}"
                f"{before['p95_ms']:>10} -> {current['p95_ms']:<9}"
            )

    # -- fixtures --------------------------------------------------------

    @staticmethod
    def _token(user):
        from rest_framework_simplejwt.tokens import RefreshToken

        return str(RefreshToken.for_user(user).access_token)

    def _user(self, slug):
        User = get_user_model()
        if slug:
            tenant = Tenant.objects.filter(slug=slug).first()
            if tenant is None:
                raise CommandError(f"No tenant with slug '{slug}'.")
        else:
            from django.db.models import Count

            tenant = (
                Tenant.objects.annotate(n=Count("messages"))
                .order_by("-n")
                .first()
            )
            if tenant is None:
                raise CommandError("No tenants. Run `manage.py seed_load_data` first.")

        user = User.objects.filter(tenant=tenant, is_active=True).order_by("pk").first()
        if user is None:
            raise CommandError(f"Tenant '{tenant.slug}' has no active user to authenticate as.")
        self.stdout.write(f"Benchmarking tenant '{tenant.slug}' as '{user.get_username()}'.")
        return user
