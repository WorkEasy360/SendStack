"""
Worker count: the standard sync-worker formula (2 * CPU cores + 1), computed
from whatever CPU allocation the container actually has at startup instead
of a number guessed in advance. This adapts automatically if the production
host's core count ever changes, and works correctly for the CPU shares
Docker actually gives the container.

To pin an exact value instead (e.g. you know the target box and want more
control), set GUNICORN_WORKERS in the environment -- it always wins.
"""
import os


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


workers = int(os.environ.get("GUNICORN_WORKERS") or (_available_cpus() * 2 + 1))

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
