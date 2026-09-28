from django.conf import settings
from django.db import connections
from django.db.utils import OperationalError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

import redis

from apps.audit.services import log_event


def _check_database():
    try:
        connections["default"].cursor()
        return True
    except OperationalError:
        return False


def _check_redis():
    try:
        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        return bool(client.ping())
    except redis.RedisError:
        return False


def _version() -> dict:
    """
    Which build is running.

    Safe to serve unauthenticated: a version string is not a secret, and the
    alternative -- nobody being able to tell which build is live during an
    incident -- costs far more than it saves. Deliberately no branch name, no
    repository URL and no dependency list.
    """
    return {
        "version": getattr(settings, "APP_VERSION", ""),
        "build": getattr(settings, "APP_BUILD", ""),
        "environment": getattr(settings, "APP_ENVIRONMENT", ""),
    }


class LivenessView(APIView):
    """
    The process is up and able to serve requests. Never checks dependencies.

    That distinction is the whole point of having two probes. If liveness
    checked PostgreSQL, a database blip would make the orchestrator kill and
    restart every healthy application container at exactly the moment the
    database was struggling -- turning a recoverable dependency outage into a
    self-inflicted restart storm. Liveness answers "is this process wedged",
    and only a wedged process should be killed.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    # Emptied deliberately, and this is not a micro-optimisation.
    #
    # DEFAULT_THROTTLE_CLASSES puts AnonRateThrottle on every view that does
    # not say otherwise, and AnonRateThrottle reads and writes the cache --
    # which is Redis. So the probe that exists to answer "is this process
    # alive without consulting anything" was consulting Redis on every call:
    # during a Redis outage it paid a connect timeout on each request, and a
    # liveness probe that takes longer than its own timeout is a liveness
    # probe that kills healthy containers precisely when a dependency is
    # already struggling.
    #
    # It would also have been rate-limited. Probes poll every 30 seconds from
    # more than one source, and a 429 reads to an orchestrator as "unhealthy".
    throttle_classes = []

    def get(self, request):
        return Response({"status": "alive", **_version()})


class ReadinessView(APIView):
    """
    The process and its critical dependencies (database, Redis) are usable.

    Deliberately cheap: a connection and a PING, no queries. This is polled
    every 30 seconds by Docker and by whatever sits in front of it, and a
    readiness probe that runs a real query is a readiness probe that falls over
    first under the load it is supposed to be reporting on.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    # Same reasoning as liveness: a probe must not be rate-limited, because a
    # 429 reads to an orchestrator as "unhealthy" and takes the container out.
    # This one does check Redis -- that is its job -- but it does so explicitly,
    # with a two-second timeout, rather than incidentally through a throttle.
    throttle_classes = []

    def get(self, request):
        checks = {
            "database": _check_database(),
            "redis": _check_redis(),
        }
        healthy = all(checks.values())
        return Response(
            {"status": "ready" if healthy else "not_ready", "checks": checks, **_version()},
            status=200 if healthy else 503,
        )


class HealthView(ReadinessView):
    """Alias of the readiness check, kept for /health/ per the deployment spec."""


class DiagnosticsView(APIView):
    """
    What a support agent needs to answer "is it us or is it them", and nothing more.

    Authenticated, and scoped to the caller's own workspace. It reports *state*,
    never configuration: connectivity as booleans, the caller's own plan and
    usage, the status of their own WhatsApp numbers. There is deliberately no
    way to reach another tenant's row and no environment variable, credential,
    URL or key anywhere in the response -- a diagnostics endpoint is exactly
    where that kind of thing gets added "just for debugging" and then stays.

    The production-readiness checks are included only for a superadmin, and even
    then as PASS/WARN/FAIL with no values (see apps.health.checks).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        tenant = user.tenant

        payload = {
            **_version(),
            "connectivity": {
                "database": _check_database(),
                "redis": _check_redis(),
                "celery": self._celery_state(),
            },
            "account": {
                "username": user.get_username(),
                "role": user.role,
                "workspace": tenant.name if tenant else None,
                "workspace_active": tenant.is_active if tenant else None,
            },
        }

        if tenant is not None:
            payload["subscription"] = self._subscription(tenant)
            payload["whatsapp"] = self._whatsapp(tenant)
            payload["legal"] = self._legal(user)

        if user.role == user.Role.SUPERADMIN:
            from apps.health.checks import run_all, summarise

            results = run_all()
            payload["production_readiness"] = {
                "summary": summarise(results),
                "checks": results,
            }

        return Response(payload)

    def _celery_state(self) -> dict:
        """
        Whether any worker is listening.

        A short timeout and a caught exception: this is a support screen, and a
        broker that is down must render as "down" rather than hanging the page
        for the length of a socket timeout.
        """
        try:
            from config.celery import app

            replies = app.control.ping(timeout=1.0) or []
            return {"workers": len(replies), "reachable": bool(replies)}
        except Exception:  # noqa: BLE001 - see the docstring
            return {"workers": 0, "reachable": False}

    def _subscription(self, tenant) -> dict:
        try:
            from apps.subscriptions.services import get_active_subscription, get_current_plan, usage

            subscription = get_active_subscription(tenant)
            plan = subscription.plan if subscription is not None else get_current_plan(tenant)
            return {
                "plan": plan.name if plan else None,
                "status": subscription.status if subscription else None,
                "access_state": subscription.access_state() if subscription else None,
                "usage": usage(tenant, subscription),
            }
        except Exception:  # noqa: BLE001 - one broken panel must not blank the whole screen
            return {"error": "Subscription information is unavailable."}

    def _whatsapp(self, tenant) -> list:
        try:
            from apps.whatsapp.models import WhatsAppAccount

            # Name, provider and status. Never the token, the session or the
            # phone number's credentials.
            return list(
                WhatsAppAccount.objects.filter(tenant=tenant).values("id", "name", "provider", "status")
            )
        except Exception:  # noqa: BLE001
            return []

    def _legal(self, user) -> dict:
        try:
            from apps.legal.services import outstanding_for

            return {"pending": outstanding_for(user)}
        except Exception:  # noqa: BLE001
            return {"pending": []}


class BackupStatusView(APIView):
    """
    Whether this deployment is actually protected. Operators only.

    Reports **outcomes, never locations**: when a backup last succeeded, how
    old it is, whether it was encrypted, whether it left the host, and when a
    restore was last verified. No path, no bucket, no destination URL and no
    credential -- a monitoring endpoint is exactly where a backup's location
    gets added "so we can see it" and then sits in a screenshot in a ticket.

    Superadmin only, and 404 rather than 403 for everybody else: whether this
    endpoint exists is not a customer's business.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.health.models import BackupRun

        last_backup = BackupRun.last_success()
        last_failed = BackupRun.last_failure()
        last_restore = BackupRun.last_success(kind=BackupRun.Kind.RESTORE_TEST)

        return Response({
            "last_successful_backup": self._run(last_backup),
            "last_failed_backup": self._run(last_failed),
            "last_restore_verification": self._run(last_restore),
            "backup_age_hours": (
                round(last_backup.age_seconds / 3600, 1) if last_backup else None
            ),
            "restore_age_days": (
                round(last_restore.age_seconds / 86400, 1) if last_restore else None
            ),
            # The two checks that judge the above, so an operator gets the
            # verdict and not just the numbers.
            "assessment": self._assessment(),
            "recent": [
                self._run(run) for run in BackupRun.objects.all()[:20]
            ],
        })

    @staticmethod
    def _run(run):
        if run is None:
            return None
        return {
            "kind": run.kind,
            "status": run.status,
            "finished_at": run.finished_at,
            # The basename only. record_backup strips any path before storing,
            # and this is the second place that is true.
            "artifact": run.artifact,
            "size_bytes": run.size_bytes,
            "encrypted": run.encrypted,
            "offsite": run.offsite,
            # A label ("s3", "rclone", "local"), never a URL.
            "destination": run.destination,
            "detail": run.detail[:500],
        }

    @staticmethod
    def _assessment():
        from apps.health.checks import check_backups, check_restore_verification

        return [check_backups(), check_restore_verification()]


class OperationsView(APIView):
    """
    The platform's operational state on one screen. Superadmin only.

    404 rather than 403 for everybody else, matching BackupStatusView above:
    whether this endpoint exists is not a customer's business, and a 403 tells
    somebody probing that it does.

    Every number comes from apps.health.operations, which is explicit about
    what it will and will not report -- volumes and states, never message
    content, never a customer's data, never a credential, a hostname or a
    broker URL.
    """

    permission_classes = [IsAuthenticated]
    # Polled by a dashboard that refreshes while an operator watches an
    # incident, and the queries behind it are counts. The ordinary user rate
    # would let that page rate-limit the person trying to diagnose an outage.
    throttle_classes = []

    def get(self, request):
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.health import operations

        return Response({**_version(), **operations.snapshot()})


class DeadLetterView(APIView):
    """
    Background jobs that gave up, and marking one dealt with. Superadmin only.

    GET lists the open ones, newest first. POST with ``{"ids": [...]}`` marks
    them resolved -- which records that a person has looked, and nothing else.

    **Resolving does not re-run anything.** Re-running a failed job is a
    decision that needs to know why it failed: re-running an import that died
    half way could double a customer's contacts, and re-running a send could
    deliver a message twice. So this endpoint deliberately offers no retry
    button, and the operator uses the specific, guarded path for whatever
    failed -- the campaign's own retry action, or a re-upload.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.health.models import DeadLetter

        rows = DeadLetter.objects.all()
        if request.query_params.get("open") != "false":
            rows = rows.filter(resolved_at__isnull=True)

        return Response({
            "results": [
                {
                    "id": row.pk,
                    "task": row.task_name,
                    "task_id": row.task_id,
                    "queue": row.queue,
                    "tenant_id": row.tenant_id,
                    "arguments": row.arguments,
                    "exception": row.exception_type,
                    # Trimmed here as well as at write time: this is rendered
                    # in a browser, and a 4,000-character cell is not a
                    # dashboard.
                    "detail": row.detail[:1000],
                    "attempts": row.attempts,
                    # The thread back to the HTTP request that started it,
                    # however many queue hops ago. See config.celery.
                    "request_id": row.request_id,
                    "failed_at": row.failed_at,
                    "resolved_at": row.resolved_at,
                }
                for row in rows[:200]
            ],
        })

    def post(self, request):
        user = request.user
        if user.role != user.Role.SUPERADMIN:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        from apps.health.models import DeadLetter

        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(value, int) for value in ids):
            return Response({"detail": "Send a list of dead-letter ids."}, status=400)

        note = str(request.data.get("note") or "")[:255]
        updated = DeadLetter.objects.filter(pk__in=ids, resolved_at__isnull=True).update(
            resolved_at=timezone.now(), resolved_note=note,
        )

        # Audited: marking a platform-wide failure record as handled is an
        # administrative action on the operational record, and Phase 3's rule
        # is that those are written down.
        log_event(
            request=request, action="operations.dead_letters_resolved", tenant=user.tenant,
            object_type="dead_letter", metadata={"count": updated},
        )
        return Response({"resolved": updated})
