"""
How many worker processes to run, decided by whichever resource runs out first.

The worker count used to be the textbook ``2 * CPU + 1``, computed from the
container's real CPU allocation. That is the right formula and the wrong
input, because **memory is what actually runs out**. Measured on a 24-core
development host: the formula asked for 49 workers, each Django worker
resident at roughly 80 MB, and the web tier alone settled at 2.9 GB -- on a
box that also has to run PostgreSQL, Redis, two Celery containers and nginx in
7.6 GB. A 32-core host would simply not have started.

So the count is now the smaller of two budgets:

    CPU     2 * cores + 1        -- how many can usefully run at once
    memory  limit / per-worker   -- how many will actually fit

with a hard ceiling on top, because past a certain point more gthread workers
buy nothing: each already serves ``threads`` requests concurrently, so twelve
workers is 96 concurrent requests, and the bottleneck by then is PostgreSQL
connections rather than Python processes.

The memory limit is read from the cgroup the container is actually running
under, so a deployment that raises ``MEM_LIMIT_BACKEND`` gets more workers
without touching this file, and one that lowers it gets fewer instead of an
OOM-kill loop at boot.

``GUNICORN_WORKERS`` still overrides everything, for an operator who has
measured their own box and wants to say so.
"""
import os

#: Resident memory to budget per worker, in bytes. Measured at ~80 MB for a
#: booted Django worker on this codebase; 128 MB leaves room for the
#: allocations a request makes on top of that without being so generous that
#: the formula starves a machine of workers it could afford.
BYTES_PER_WORKER = int(os.environ.get("GUNICORN_BYTES_PER_WORKER") or 128 * 1024 * 1024)

#: Never fewer than this, whatever the arithmetic says. Two workers means a
#: worker being recycled does not leave the site unserved.
MIN_WORKERS = 2

#: Never more than this. Each already handles `threads` concurrent requests,
#: so this is 96+ in flight -- well past the point where PostgreSQL's
#: connection limit, not this number, is the constraint.
MAX_WORKERS = 12

#: Of the container's memory, how much the web workers may claim. The rest is
#: the interpreter's shared pages, the OS page cache and headroom for a
#: request that allocates unusually.
MEMORY_FRACTION = 0.75


def _available_cpus() -> int:
    # os.sched_getaffinity respects Docker's --cpuset-cpus / cgroup cpuset
    # (which cores this container may use), unlike os.cpu_count() which
    # reports the host's full core count even inside a constrained
    # container. Not available on Windows/macOS, hence the fallback --
    # irrelevant in prod since this only runs inside the Linux container.
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def _memory_limit_bytes():
    """
    The container's memory limit, or None if it has none.

    Both cgroup versions are read because which one is in force depends on the
    host's kernel and Docker configuration, not on anything this image
    controls. cgroup v2 writes the literal string "max" when unlimited; v1
    writes a number so large it is unlimited in practice, which is why an
    implausible value is treated as absent rather than believed.
    """
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # Anything above a terabyte is the "no limit" sentinel, not a limit.
        if 0 < value < (1 << 40):
            return value
    return None


def _worker_count() -> int:
    explicit = os.environ.get("GUNICORN_WORKERS")
    if explicit:
        return max(1, int(explicit))

    by_cpu = _available_cpus() * 2 + 1

    limit = _memory_limit_bytes()
    if limit is None:
        # No limit to reason about: fall back to the CPU formula, still capped.
        return max(MIN_WORKERS, min(by_cpu, MAX_WORKERS))

    by_memory = int((limit * MEMORY_FRACTION) // BYTES_PER_WORKER)
    return max(MIN_WORKERS, min(by_cpu, by_memory, MAX_WORKERS))


workers = _worker_count()

# Threaded rather than sync workers, because the inbox's live updates are
# Server-Sent Events: each connected browser holds one HTTP connection open
# for minutes at a time. On sync workers that would occupy a whole worker
# process per open tab and starve ordinary requests within a handful of
# users; on gthread it costs a thread. Ordinary requests are unaffected --
# they still run one per thread, and Django is thread-safe per request.
#
# Concurrency is therefore workers * threads. Raise GUNICORN_THREADS if
# agents report the inbox falling back to polling under load.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("GUNICORN_THREADS") or 8)

# A stream is capped at ten minutes server-side (apps/realtime/views.py) and
# the browser reconnects, so this only has to outlast one stream's lifetime.
timeout = int(os.environ.get("GUNICORN_TIMEOUT") or 660)

bind = "0.0.0.0:8000"
