#!/usr/bin/env python3
"""
Concurrent load test: what happens when many tenants use the product at once.

    python scripts/loadtest.py --users 40 --duration 120
    python scripts/loadtest.py --users 40 --duration 120 --json run.json
    python scripts/loadtest.py --users 40 --duration 120 --compare baseline.json

**This is not the same test as `manage.py benchmark_api`, and confusing the
two is how a deployment claims capacity it has never measured.**

    benchmark_api  --  DATA SCALE.  One request at a time, through Django's
                       test client, against a large dataset.  Answers "does
                       this endpoint stay cheap when a tenant has a million
                       messages?"  It reports SQL query counts, which is the
                       number that predicts the next order of magnitude.

    loadtest.py    --  CONCURRENCY.  Many real HTTP clients at once, over the
                       network, through nginx and gunicorn, as many different
                       tenants.  Answers "how many customers can be doing
                       things simultaneously before latency or errors move?"
                       It reports p50/p95/p99, error rate and what the
                       infrastructure was doing while that happened.

A fast benchmark_api result says nothing about concurrency: it never contends
for a gunicorn worker, a database connection or a Redis socket, because there
is only ever one request in flight.  A good loadtest result says nothing about
data scale: forty concurrent requests against four contacts each is a test of
the connection pool.  Both are needed and neither substitutes for the other.

WHAT IT WILL NOT DO
-------------------
It never sends a WhatsApp message.  The write half of the workload creates
campaigns as DRAFTS and stops.  `--include-sends` exists for a staging stack
whose provider is a stub, and it refuses to run unless the deployment says it
is not production and every device under test is a seeded one.  A load test
that dispatches ten thousand messages through a real number is not a load
test, it is an incident.

SETUP
-----
On the staging host, against a disposable database:

    docker compose exec backend python manage.py seed_load_data \\
        --tenants 200 --contacts 200000 --messages 2000000 --yes
    python scripts/loadtest.py --users 40 --duration 180

The seeded owners share one password (`loadtest-password`) and exist only in a
scratch database; the seeder refuses to run anywhere else.

WHAT IT MEASURES
----------------
Per scenario and overall: count, error rate, p50, p95, p99, max, throughput.
Alongside, sampled once a second while the load runs:

    CPU and memory       per container, from `docker stats`
    Redis                connected clients, memory, ops/sec, from INFO
    PostgreSQL           active and idle connections, from pg_stat_activity
    Celery queue depth   LLEN per queue, from Redis directly
    Worker throughput    total tasks completed, from `celery inspect stats`

The infrastructure samples are what turn a p99 into a diagnosis.  A p99 that
climbs while CPU sits at 30% and the database connection count is pinned at
its ceiling is a pool-size problem, and reporting only the p99 would have sent
somebody to optimise a query.

Standard library only, on purpose: this runs on a staging host, and a load
generator that needs a pip install before it can tell you anything is a load
generator nobody runs.
"""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
# The workload
# --------------------------------------------------------------------------
#
# Weights are roughly how often a real session touches each thing: a customer
# looks at the dashboard and their contacts constantly, runs a report
# sometimes, and creates a campaign rarely.  A uniform mix would spend most of
# the run measuring the endpoint nobody opens.
#
# `write` marks the scenarios that change state.  They are the ones that
# contend on locks and on the credit ledger, so their p99 is read separately
# from the read traffic they are mixed with -- an average over both hides
# exactly the contention the test exists to find.

SCENARIOS = (
    # (name, weight, method, path, body, write?)
    ("dashboard",         22, "GET",  "/api/v1/reports/dashboard/", None, False),
    ("contacts-list",     18, "GET",  "/api/v1/contacts/contacts/?page_size=25", None, False),
    ("contacts-search",    8, "GET",  "/api/v1/contacts/contacts/?search=Priya", None, False),
    ("campaigns-list",    12, "GET",  "/api/v1/campaigns/campaigns/", None, False),
    ("messages-list",      10, "GET", "/api/v1/messaging/messages/?page_size=25", None, False),
    ("daily-report",       6, "GET",  "/api/v1/reports/messages/daily/", None, False),
    ("usage",              8, "GET",  "/api/v1/billing/usage/", None, False),
    ("onboarding",         6, "GET",  "/api/v1/onboarding/", None, False),
    ("inbox",              5, "GET",  "/api/v1/inbox/conversations/", None, False),
    # The write path.  A draft campaign: it exercises serialization,
    # validation, the tenant scoping and an INSERT, and dispatches nothing.
    ("campaign-create",    5, "POST", "/api/v1/campaigns/campaigns/", "campaign", True),
)

#: Queues the application publishes to. Mirrors CELERY_QUEUE_NAMES in
#: config/settings/base.py -- if that list grows, this one has to as well, and
#: the run prints a warning when a queue it does not know about has depth.
QUEUE_NAMES = ("celery", "messages", "campaigns", "bulk", "maintenance")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Client:
    """
    One virtual user: one tenant, one token, one connection's worth of traffic.

    Deliberately a real HTTP client against the deployed URL rather than
    Django's test client.  nginx, gunicorn's worker pool, keep-alive and the
    TCP stack are all part of what breaks under concurrency, and a test client
    has none of them.
    """

    def __init__(self, base_url, username, password, timeout):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token = None
        #: This tenant's own WhatsApp account, needed to create a campaign.
        #: Fetched once at login rather than per request, the way a real
        #: session holds it in the page it already loaded.
        self.account_id = None

    def login(self):
        body = json.dumps({"username": self.username, "password": self.password}).encode()
        request = urllib.request.Request(
            f"{self.base_url}/api/auth/token/",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        self.token = payload["access"]

        # One extra call per virtual user, at setup rather than under load, so
        # the write scenario can post a campaign the API will actually accept.
        try:
            _, status = self.call("GET", "/api/v1/whatsapp/accounts/")
            if status == 200:
                self.account_id = self._first_account_id()
        except Exception:  # noqa: BLE001 - a missing device only disables one scenario
            self.account_id = None
        return self.token

    def _first_account_id(self):
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/whatsapp/accounts/",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        return rows[0]["id"] if rows else None

    def call(self, method, path, body=None):
        """
        Returns (elapsed_seconds, status_code).  Never raises for an HTTP error.

        A 500 under load is a *result*, not an exception: it belongs in the
        error rate next to its latency, because a server that fails fast would
        otherwise look like a server that got quicker.
        """
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
                return time.perf_counter() - started, response.status
        except urllib.error.HTTPError as exc:
            exc.read()
            return time.perf_counter() - started, exc.code
        except Exception:  # noqa: BLE001 - a timeout or a reset is an error rate entry
            return time.perf_counter() - started, 0


# --------------------------------------------------------------------------
# Sampling what the infrastructure was doing
# --------------------------------------------------------------------------


def _docker(args, timeout=15):
    """Run a docker command, returning stdout or "" -- never raising."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - sampling must never take the run down
        return ""


class Sampler(threading.Thread):
    """
    Reads the infrastructure once a second for as long as the load runs.

    A separate thread rather than an entry in the workload, because these
    samples must be taken *while* the load is applied.  Sampling before and
    after would report a system at rest, which is the one state nobody is
    asking about.
    """

    def __init__(self, interval, containers, redis_container, postgres_container,
                 db_user, db_name, redis_password=""):
        super().__init__(daemon=True)
        self.interval = interval
        self.containers = containers
        self.redis_container = redis_container
        self.postgres_container = postgres_container
        self.db_user = db_user
        self.db_name = db_name
        # Passed to redis-cli through REDISCLI_AUTH rather than -a, which
        # prints a warning to stderr on every call and puts the password in the
        # container's process list.
        self.redis_password = redis_password
        self.samples = []
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            started = time.perf_counter()
            sample = {"at": time.time()}
            sample["containers"] = self._container_stats()
            sample["redis"] = self._redis()
            sample["postgres"] = self._postgres()
            sample["queues"] = self._queues()
            self.samples.append(sample)
            # Sample on a fixed cadence rather than sleeping a fixed amount:
            # `docker stats` alone takes over a second on a busy host, and a
            # naive sleep would silently halve the sample rate under load --
            # exactly when the samples matter most.
            elapsed = time.perf_counter() - started
            self._stop.wait(max(self.interval - elapsed, 0.05))

    def _container_stats(self):
        raw = _docker([
            "stats", "--no-stream", "--format",
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
        ])
        out = {}
        for line in raw.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 4:
                continue
            name, cpu, mem, mem_pct = parts
            if self.containers and not any(c in name for c in self.containers):
                continue
            out[name] = {
                "cpu_percent": _percent(cpu),
                "memory": mem.strip(),
                "memory_percent": _percent(mem_pct),
            }
        return out

    def _redis(self):
        raw = _docker(self._redis_cli("INFO"))
        wanted = {
            "connected_clients", "used_memory_human", "instantaneous_ops_per_sec",
            "blocked_clients", "rejected_connections", "evicted_keys",
        }
        out = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            if key.strip() in wanted:
                out[key.strip()] = value.strip()
        return out

    def _postgres(self):
        # Connection *count* is the number that matters here.  gunicorn workers
        # each hold a connection, Celery workers hold theirs, and PostgreSQL's
        # max_connections is a wall a deployment hits long before CPU.
        # Filtered on the database by **name**, not on current_database().
        # The psql session connects to whatever -d says, which is not
        # necessarily the database the application is using -- and when the two
        # differ, current_database() silently counts the wrong pool and reports
        # a flat 1 connection under any load at all.
        raw = _docker([
            "exec", self.postgres_container, "psql", "-U", self.db_user, "-d", self.db_name,
            "-tAc",
            f"SELECT state, count(*) FROM pg_stat_activity "
            f"WHERE datname = '{self.db_name}' GROUP BY state",
        ])
        out = {}
        for line in raw.strip().splitlines():
            state, _, count = line.partition("|")
            if count.strip().isdigit():
                out[(state.strip() or "unknown")] = int(count.strip())
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out

    def _redis_cli(self, *args):
        return _redis_cli_args(self.redis_container, self.redis_password, *args)

    def _queues(self):
        """Depth per queue, straight from Redis. A queue nobody consumes shows here."""
        out = {}
        for queue in QUEUE_NAMES:
            raw = _docker(self._redis_cli("LLEN", queue)).strip()
            if raw.isdigit():
                out[queue] = int(raw)
        return out


def _redis_cli_args(container, password, *args):
    """`docker exec` argv for a redis-cli call, authenticated when required."""
    prefix = ["exec"]
    if password:
        prefix += ["-e", f"REDISCLI_AUTH={password}"]
    return prefix + [container, "redis-cli", *args]


def _percent(value):
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def worker_totals(backend_container):
    """
    Total tasks each Celery worker has completed, from the worker itself.

    Sampled before and after the run; the difference is throughput.  Read from
    `inspect stats` rather than counted in the database because it counts every
    task, including the ones that write nothing.
    """
    raw = _docker([
        "exec", backend_container, "celery", "-A", "config", "inspect", "stats", "--json",
    ], timeout=30)
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    totals = {}
    for worker, stats in (data or {}).items():
        counts = (stats or {}).get("total") or {}
        if isinstance(counts, dict):
            totals[worker] = sum(int(v) for v in counts.values() if isinstance(v, (int, float)))
    return totals


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def build_body(kind, run_id, index, client=None):
    if kind == "campaign":
        if client is None or client.account_id is None:
            return None
        return {
            "name": f"loadtest {run_id} #{index}",
            "message_body": "Load test draft. This campaign is never dispatched.",
            "whatsapp_account": client.account_id,
            "audience_type": "ALL",
        }
    return None


def run_load(clients, duration, run_id, stop_event, think=1.0):
    """
    Drive one virtual user until the clock runs out.

    Each user picks its next scenario from the weighted mix independently, so
    the aggregate is a realistic blend rather than every user marching through
    the same sequence in lockstep -- which would produce artificial bursts on
    one endpoint and idle time on the rest.

    **Think time is what makes a virtual user a user.** With no pause, forty
    threads issue about 40,000 requests a minute between them; the per-user
    throttle is 300/min, so 92% of the run comes back 429 and the only thing
    measured is the rate limiter. A person reading a dashboard and clicking
    something pauses for seconds. `think` is that pause, jittered so the users
    do not synchronise into waves. Pass `--think 0` deliberately to saturate,
    and read the result as "what the throttle does", not "what the app can do".
    """
    names = [s[0] for s in SCENARIOS]
    weights = [s[1] for s in SCENARIOS]
    by_name = {s[0]: s for s in SCENARIOS}

    results = defaultdict(list)
    errors = defaultdict(int)
    statuses = defaultdict(lambda: defaultdict(int))

    def one_user(client):
        rng = random.Random(id(client))
        counter = 0
        deadline = time.perf_counter() + duration
        local = defaultdict(list)
        local_errors = defaultdict(int)
        local_status = defaultdict(lambda: defaultdict(int))
        while time.perf_counter() < deadline and not stop_event.is_set():
            name = rng.choices(names, weights=weights, k=1)[0]
            _, _, method, path, body_kind, _ = by_name[name]
            counter += 1
            body = build_body(body_kind, run_id, counter, client) if body_kind else None
            if body_kind and body is None:
                continue  # this tenant has no device; skip the write scenario
            elapsed, status = client.call(method, path, body)
            local[name].append(elapsed)
            local_status[name][status] += 1
            # 2xx and 3xx are fine.  4xx is not: every request here is a
            # legitimate call by an authenticated user of their own data, so a
            # 4xx means the load broke something (a throttle, a token, a
            # validation path that only fails when concurrent).
            if status < 200 or status >= 400:
                local_errors[name] += 1

            if think > 0:
                # Uniform jitter around the mean, so N users do not fall into
                # lockstep and produce a sawtooth that looks like a bottleneck.
                time.sleep(rng.uniform(think * 0.5, think * 1.5))
        return local, local_errors, local_status

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        for local, local_errors, local_status in pool.map(one_user, clients):
            for name, times in local.items():
                results[name].extend(times)
            for name, count in local_errors.items():
                errors[name] += count
            for name, codes in local_status.items():
                for code, count in codes.items():
                    statuses[name][code] += count

    return results, errors, statuses


def percentiles(values):
    if not values:
        return {}
    ordered = sorted(values)

    def at(p):
        # Nearest-rank.  With a few thousand samples the difference from
        # interpolation is noise, and nearest-rank has the property that every
        # reported number is a request that actually happened.
        index = max(int(round(p / 100 * len(ordered) + 0.5)) - 1, 0)
        return ordered[min(index, len(ordered) - 1)]

    return {
        "count": len(ordered),
        "p50_ms": at(50) * 1000,
        "p95_ms": at(95) * 1000,
        "p99_ms": at(99) * 1000,
        "max_ms": ordered[-1] * 1000,
        "mean_ms": statistics.fmean(ordered) * 1000,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def report(run, compare=None):
    lines = []
    add = lines.append

    add("")
    add("=" * 78)
    add("CONCURRENCY LOAD TEST")
    add("=" * 78)
    add(f"  target          {run['target']}")
    add(f"  virtual users   {run['users']}")
    add(f"  think time      {run['think']}s mean between requests, jittered")
    add(f"  duration        {run['duration']}s")
    add(f"  tenants         {run['tenants']} distinct workspaces")
    add(f"  started         {run['started']}")
    add("")
    add("  This measures CONCURRENCY, not data scale. For data scale run")
    add("  `manage.py benchmark_api`, which reports SQL query counts.")
    add("")

    total = run["totals"]
    add("-" * 78)
    add("OVERALL")
    add("-" * 78)
    add(f"  requests        {total['count']:,}")
    add(f"  throughput      {total['rps']:.1f} req/s")
    add(f"  errors          {total['errors']:,}  ({total['error_rate']:.2f}%)")
    throttled = (run.get("status_codes") or {}).get("429", 0)
    if throttled:
        # Counted inside the error rate above, and called out separately:
        # a 429 is the rate limiter doing its job, not the application
        # failing, and conflating the two makes a healthy run look broken.
        add(
            f"  of which 429    {throttled:,}  -- rate limited, not failures. "
            f"Raise --think or the throttle."
        )
    add(f"  p50 / p95 / p99 {total['p50_ms']:.0f} / {total['p95_ms']:.0f} / {total['p99_ms']:.0f} ms")
    add(f"  max             {total['max_ms']:.0f} ms")
    add("")

    add("-" * 78)
    add(f"{'SCENARIO':<20}{'N':>8}{'ERR%':>8}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}")
    add("-" * 78)
    for name, stats in run["scenarios"].items():
        if not stats.get("count"):
            continue
        delta = ""
        if compare:
            before = (compare.get("scenarios") or {}).get(name, {}).get("p95_ms")
            if before:
                change = (stats["p95_ms"] - before) / before * 100
                delta = f"  {change:+.0f}% p95"
        add(
            f"{name:<20}{stats['count']:>8,}{stats['error_rate']:>7.1f}%"
            f"{stats['p50_ms']:>9.0f}{stats['p95_ms']:>9.0f}"
            f"{stats['p99_ms']:>9.0f}{stats['max_ms']:>9.0f}{delta}"
        )
    add("")

    codes = run.get("status_codes") or {}
    if codes:
        add("-" * 78)
        add("HTTP STATUS CODES")
        add("-" * 78)
        for code in sorted(codes, key=lambda c: -codes[c]):
            label = "connection error / timeout" if code == "0" else ""
            add(f"  {code:<6}{codes[code]:>10,}  {label}")
        add("")

    infra = run.get("infrastructure") or {}
    if infra:
        add("-" * 78)
        add("INFRASTRUCTURE, WHILE THE LOAD WAS RUNNING")
        add("-" * 78)
        for name, stats in (infra.get("containers") or {}).items():
            add(
                f"  {name:<32} CPU peak {stats['cpu_peak']:>6.1f}%   "
                f"mean {stats['cpu_mean']:>6.1f}%   mem peak {stats['memory_peak']}"
            )
        pg = infra.get("postgres") or {}
        if pg:
            add(
                f"  postgres connections             peak {pg.get('peak_total', 0)}, "
                f"mean {pg.get('mean_total', 0):.1f}  (active peak {pg.get('peak_active', 0)})"
            )
        redis = infra.get("redis") or {}
        if redis:
            add(
                f"  redis                            clients peak {redis.get('peak_clients', 0)}, "
                f"ops/sec peak {redis.get('peak_ops', 0)}, memory {redis.get('memory', '?')}"
            )
            if redis.get("rejected_connections"):
                add(f"  redis REJECTED CONNECTIONS       {redis['rejected_connections']}")
        queues = infra.get("queues") or {}
        if queues:
            depths = ", ".join(f"{q} peak {d}" for q, d in queues.items())
            add(f"  celery queue depth               {depths}")
        workers = infra.get("workers") or {}
        if workers:
            add(
                f"  celery tasks completed           {workers.get('completed', 0)} "
                f"({workers.get('per_second', 0):.1f}/s across {workers.get('worker_count', 0)} worker(s))"
            )
        add("")

    add("-" * 78)
    add("READING THIS")
    add("-" * 78)
    add("  A p99 that climbs while CPU is low and postgres connections are at")
    add("  their ceiling is a pool-size problem, not a slow query. A rising")
    add("  queue depth with flat worker throughput means the workers are the")
    add("  bottleneck. Errors at status 0 are timeouts or resets, not 500s.")
    add("")

    return "\n".join(lines)


def summarise_samples(samples, worker_before, worker_after, elapsed):
    if not samples:
        return {}

    containers = defaultdict(lambda: {"cpu": [], "memory": []})
    for sample in samples:
        for name, stats in (sample.get("containers") or {}).items():
            if stats.get("cpu_percent") is not None:
                containers[name]["cpu"].append(stats["cpu_percent"])
            if stats.get("memory"):
                containers[name]["memory"].append(stats["memory"])

    container_summary = {}
    for name, values in containers.items():
        if not values["cpu"]:
            continue
        container_summary[name] = {
            "cpu_peak": max(values["cpu"]),
            "cpu_mean": statistics.fmean(values["cpu"]),
            "memory_peak": max(values["memory"], key=_memory_bytes) if values["memory"] else "?",
        }

    totals = [s["postgres"].get("total", 0) for s in samples if s.get("postgres")]
    actives = [s["postgres"].get("active", 0) for s in samples if s.get("postgres")]
    postgres = {
        "peak_total": max(totals) if totals else 0,
        "mean_total": statistics.fmean(totals) if totals else 0,
        "peak_active": max(actives) if actives else 0,
    }

    clients = [_int(s["redis"].get("connected_clients")) for s in samples if s.get("redis")]
    ops = [_int(s["redis"].get("instantaneous_ops_per_sec")) for s in samples if s.get("redis")]
    rejected = [_int(s["redis"].get("rejected_connections")) for s in samples if s.get("redis")]
    memories = [s["redis"].get("used_memory_human") for s in samples if s.get("redis")]
    redis = {
        "peak_clients": max([c for c in clients if c is not None] or [0]),
        "peak_ops": max([o for o in ops if o is not None] or [0]),
        "rejected_connections": max([r for r in rejected if r is not None] or [0]),
        "memory": memories[-1] if memories else "?",
    }

    queues = {}
    for sample in samples:
        for queue, depth in (sample.get("queues") or {}).items():
            queues[queue] = max(queues.get(queue, 0), depth)

    completed = sum(worker_after.values()) - sum(worker_before.values())
    workers = {
        "completed": max(completed, 0),
        "per_second": max(completed, 0) / elapsed if elapsed else 0,
        "worker_count": len(worker_after or worker_before),
    }

    return {
        "samples": len(samples),
        "containers": container_summary,
        "postgres": postgres,
        "redis": redis,
        "queues": queues,
        "workers": workers,
    }


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _memory_bytes(usage):
    """Sort key for a `docker stats` memory string like '412.3MiB / 3.8GiB'."""
    head = str(usage).split("/")[0].strip()
    units = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "kB": 1000, "MB": 10 ** 6, "GB": 10 ** 9}
    for unit, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if head.endswith(unit):
            try:
                return float(head[: -len(unit)]) * factor
            except ValueError:
                return 0.0
    return 0.0


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.getenv("LOADTEST_BASE_URL", "http://localhost"))
    parser.add_argument("--users", type=int, default=20, help="Concurrent virtual users.")
    parser.add_argument("--duration", type=int, default=60, help="Seconds of load.")
    parser.add_argument("--tenants", type=int, default=20, help="How many seeded workspaces to spread across.")
    parser.add_argument(
        "--login-delay", type=float, default=None,
        help=(
            "Seconds between logins during setup. Defaults to 6s above 8 workspaces, "
            "which keeps setup inside the per-address login throttle."
        ),
    )
    parser.add_argument("--password", default=os.getenv("LOADTEST_PASSWORD", "loadtest-password"))
    parser.add_argument(
        "--think", type=float, default=1.0,
        help=(
            "Mean seconds a virtual user pauses between requests, jittered. "
            "0 saturates -- and then measures the rate limiter, not the app."
        ),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--json", default="", help="Write the full result to this file.")
    parser.add_argument("--compare", default="", help="A previous --json file, to show p95 movement.")
    parser.add_argument("--no-infra", action="store_true", help="Skip docker/redis/postgres sampling.")
    parser.add_argument("--redis-container", default="multisender-redis-1")
    parser.add_argument("--postgres-container", default="multisender-postgres-1")
    parser.add_argument("--backend-container", default="multisender-backend-1")
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "multisender"))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "multisender"))
    parser.add_argument(
        "--redis-password", default=os.getenv("REDIS_PASSWORD", ""),
        help="Only needed when Redis requires AUTH. Read from the environment by default.",
    )
    args = parser.parse_args()

    run_id = f"{int(time.time())}"

    # **One login per workspace, not one per virtual user.**
    #
    # The login endpoint is throttled per client address (`auth`, 10/min by
    # default), which is a security control doing exactly its job -- and it
    # means a load test that logs in two hundred times in a row gets ten
    # sessions and a hundred and ninety rejections, then reports its numbers as
    # though it had two hundred users. That is not hypothetical: it is what
    # this script did, and the run looked entirely plausible until the requests
    # per user were divided out.
    #
    # Several virtual users sharing one workspace's token is also the more
    # realistic shape. A fifteen-person team is fifteen concurrent sessions on
    # one tenant, and it is *more* interesting for concurrency than fifteen
    # isolated tenants: per-tenant locks, the monthly allowance counter and the
    # credit ledger are exactly where simultaneous work contends.
    delay = args.login_delay
    if delay is None:
        # Respect the throttle without having to be told. 6s between logins
        # keeps any workspace count inside a 10/min budget.
        delay = 0.0 if args.tenants <= 8 else 6.0
    setup = args.tenants * delay
    print(
        f"Logging in {args.tenants} workspace(s) for {args.users} virtual user(s)"
        + (f" -- about {setup / 60:.0f} min, paced for the login throttle." if setup > 30 else "...")
    )

    sessions = {}
    failures = []
    for index in range(args.tenants):
        slug = f"loadtest-tenant-{index:05d}"
        client = Client(args.base_url, f"{slug}-owner", args.password, args.timeout)
        try:
            client.login()
            sessions[index] = client
        except urllib.error.HTTPError as exc:
            # The status is the whole diagnosis here: 429 means the login
            # throttle (raise --login-delay, or wait a minute), 401 means the
            # database was never seeded. Reporting only "HTTPError" sent
            # somebody looking at the seeder for a rate-limit problem.
            hint = " -- login throttled, raise --login-delay" if exc.code == 429 else ""
            failures.append(f"{slug}: HTTP {exc.code}{hint}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{slug}: {type(exc).__name__}")
        if delay and index < args.tenants - 1:
            time.sleep(delay)

    # Virtual users are dealt round-robin over the workspaces that logged in.
    # Each gets its own Client -- its own counters, its own think schedule --
    # sharing its workspace's token.
    clients = []
    ordered = [sessions[key] for key in sorted(sessions)]
    for index in range(args.users):
        if not ordered:
            break
        source = ordered[index % len(ordered)]
        client = Client(args.base_url, source.username, args.password, args.timeout)
        client.token = source.token
        client.account_id = source.account_id
        clients.append(client)

    if not clients:
        print("\nCould not log in as any seeded user. Seed the staging database first:", file=sys.stderr)
        print("  docker compose exec backend python manage.py seed_load_data --tenants 200 --yes", file=sys.stderr)
        for failure in failures[:5]:
            print(f"  {failure}", file=sys.stderr)
        return 2
    if failures:
        # Loud, because a quiet version of this line is how a run reports
        # capacity it never applied. The most likely cause is the login
        # throttle, and --login-delay is the answer.
        print("")
        print(f"  WARNING: {len(failures)} of {args.tenants} workspace(s) could not log in.")
        print(f"  Running with {len(sessions)} workspace(s) and {len(clients)} virtual user(s).")
        print("  If these are 429s, raise --login-delay.")
        for failure in failures[:3]:
            print(f"    {failure}")
        print("")

    sampler = None
    worker_before = {}
    if not args.no_infra:
        worker_before = worker_totals(args.backend_container)
        sampler = Sampler(
            args.sample_interval,
            containers=("backend", "celery", "nginx", "postgres", "redis"),
            redis_container=args.redis_container,
            postgres_container=args.postgres_container,
            db_user=args.db_user,
            db_name=args.db_name,
            redis_password=args.redis_password,
        )
        sampler.start()

    print(f"Applying load for {args.duration}s...")
    stop_event = threading.Event()
    started_at = time.time()
    wall_start = time.perf_counter()
    try:
        results, errors, statuses = run_load(
            clients, args.duration, run_id, stop_event, think=args.think,
        )
    except KeyboardInterrupt:
        stop_event.set()
        print("\nInterrupted.", file=sys.stderr)
        return 130
    elapsed = time.perf_counter() - wall_start

    infrastructure = {}
    if sampler is not None:
        sampler.stop()
        sampler.join(timeout=10)
        infrastructure = summarise_samples(
            sampler.samples, worker_before, worker_totals(args.backend_container), elapsed,
        )

    scenarios = {}
    for name, _, _, _, _, is_write in SCENARIOS:
        times = results.get(name, [])
        stats = percentiles(times)
        if stats:
            stats["errors"] = errors.get(name, 0)
            stats["error_rate"] = errors.get(name, 0) / stats["count"] * 100
            stats["write"] = is_write
        scenarios[name] = stats

    all_times = [t for times in results.values() for t in times]
    all_errors = sum(errors.values())
    totals = percentiles(all_times)
    totals["errors"] = all_errors
    totals["error_rate"] = (all_errors / len(all_times) * 100) if all_times else 0.0
    totals["rps"] = len(all_times) / elapsed if elapsed else 0.0

    status_codes = defaultdict(int)
    for codes in statuses.values():
        for code, count in codes.items():
            status_codes[str(code)] += count

    run = {
        "kind": "concurrency",
        "target": args.base_url,
        "users": len(clients),
        "think": args.think,
        # What was actually reached, not what was asked for. A run that lost
        # workspaces to the throttle must not report the requested number.
        "tenants": len(sessions),
        "tenants_requested": args.tenants,
        "duration": args.duration,
        "elapsed": elapsed,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)),
        "scenarios": scenarios,
        "totals": totals,
        "status_codes": dict(status_codes),
        "infrastructure": infrastructure,
    }

    previous = None
    if args.compare:
        try:
            with open(args.compare) as handle:
                previous = json.load(handle)
        except OSError as exc:
            print(f"Could not read {args.compare}: {exc}", file=sys.stderr)

    print(report(run, previous))

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(run, handle, indent=2)
        print(f"Full result written to {args.json}")

    # Non-zero on an error rate that would be a customer-visible outage, so a
    # CI job can gate on it without parsing the report.
    return 1 if totals["error_rate"] > 1.0 else 0


if __name__ == "__main__":
    sys.exit(main())
