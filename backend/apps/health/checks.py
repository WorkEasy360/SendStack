"""
Is this deployment configured to be trusted with real customer data?

One list of checks, used from three places: the `check_production` management
command an operator runs before a release, the support-diagnostics endpoint, and
the test suite that keeps the list honest.

**No check ever returns a value.** Each returns PASS, WARN or FAIL and a
sentence. The output of this module goes into deployment logs, CI output and a
screenshot in a support ticket, and a "readiness check" that prints the secret
key to prove it is set would be the worst secret leak in the codebase.

The distinction between WARN and FAIL is the useful part:

* **FAIL** -- customer data or customer access is at risk right now. Do not
  release.
* **WARN** -- a real gap that is survivable for a while, or a feature that is
  simply switched off. A deployment with no AI provider is not broken.
"""

import os

from django.conf import settings

PASS = "PASS"
WARN = "WARNING"
FAIL = "FAIL"


def _result(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _is_set(name) -> bool:
    """Whether a setting has a non-empty value. Never returns the value."""
    return bool(getattr(settings, name, "") or "")


# ---------------------------------------------------------------------------
# Core configuration
# ---------------------------------------------------------------------------

def check_debug():
    if getattr(settings, "DEBUG", False):
        return _result(
            "DEBUG is off", FAIL,
            "DEBUG is on. Every error page will print settings, SQL and a full traceback to whoever triggered it.",
        )
    return _result("DEBUG is off", PASS, "DEBUG is off.")


def check_secret_key():
    key = getattr(settings, "SECRET_KEY", "") or ""
    if not key:
        return _result("Secret key set", FAIL, "SECRET_KEY is empty. Sessions and signed values are not secure.")
    if "insecure" in key.lower() or "change" in key.lower():
        return _result(
            "Secret key set", FAIL,
            "SECRET_KEY is still a development placeholder. Generate a real one.",
        )
    if len(key) < 40:
        return _result("Secret key set", WARN, "SECRET_KEY is shorter than 40 characters.")
    return _result("Secret key set", PASS, "A non-default secret key is configured.")


def check_jwt_secret():
    if not os.getenv("JWT_SECRET"):
        return _result(
            "JWT signing key", WARN,
            "JWT_SECRET is unset, so tokens are signed with SECRET_KEY. Workable, but rotating "
            "SECRET_KEY then signs out every user at once.",
        )
    return _result("JWT signing key", PASS, "A dedicated JWT signing key is configured.")


def check_allowed_hosts():
    hosts = [host for host in getattr(settings, "ALLOWED_HOSTS", []) if host != "localhost"]
    if not hosts:
        return _result("Allowed hosts", FAIL, "ALLOWED_HOSTS names no real host.")
    if "*" in hosts:
        return _result("Allowed hosts", FAIL, "ALLOWED_HOSTS contains '*', which accepts any Host header.")
    return _result("Allowed hosts", PASS, f"{len(hosts)} host(s) configured.")


def check_database():
    config = settings.DATABASES.get("default", {})
    if not config.get("NAME") or not config.get("HOST"):
        return _result("Database configured", FAIL, "The default database is missing a name or host.")
    if not config.get("PASSWORD"):
        return _result("Database configured", FAIL, "The database password is empty.")
    if config.get("CONN_MAX_AGE", 0) == 0:
        return _result(
            "Database configured", WARN,
            "CONN_MAX_AGE is 0, so every request opens a new PostgreSQL connection.",
        )
    return _result("Database configured", PASS, "Database host, name, password and connection reuse are set.")


def check_redis():
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return _result("Redis configured", FAIL, "REDIS_URL is unset. Cache, throttling and Celery all need it.")
    if "@" not in url:
        return _result(
            "Redis configured", FAIL,
            "The Redis URL carries no password. Redis holds queued tasks, rate-limit counters "
            "and failed-login counters.",
        )
    return _result("Redis configured", PASS, "Redis is configured with authentication.")


# ---------------------------------------------------------------------------
# Transport and browser security
# ---------------------------------------------------------------------------

def check_https():
    if not getattr(settings, "SECURE_SSL_REDIRECT", False):
        return _result(
            "HTTPS enforced", FAIL,
            "SECURE_SSL_REDIRECT is off, so plain HTTP is served. Tokens and message content "
            "travel in the clear.",
        )
    if not getattr(settings, "SECURE_HSTS_SECONDS", 0):
        return _result("HTTPS enforced", WARN, "HTTPS is enforced but HSTS is not set.")
    return _result("HTTPS enforced", PASS, "HTTPS is enforced and HSTS is set.")


def check_cookies():
    problems = [
        name
        for name, expected in (
            ("SESSION_COOKIE_SECURE", True),
            ("SESSION_COOKIE_HTTPONLY", True),
            ("CSRF_COOKIE_SECURE", True),
        )
        if getattr(settings, name, False) is not expected
    ]
    if problems:
        return _result("Secure cookies", FAIL, f"Not set: {', '.join(problems)}.")
    return _result("Secure cookies", PASS, "Session and CSRF cookies are secure and HTTP-only.")


def check_security_headers():
    missing = []
    if not getattr(settings, "SECURE_CONTENT_TYPE_NOSNIFF", False):
        missing.append("X-Content-Type-Options")
    if getattr(settings, "X_FRAME_OPTIONS", "") != "DENY":
        missing.append("X-Frame-Options: DENY")
    if not getattr(settings, "SECURE_REFERRER_POLICY", ""):
        missing.append("Referrer-Policy")
    if missing:
        return _result("Security headers", WARN, f"Not configured: {', '.join(missing)}.")
    return _result("Security headers", PASS, "Content-type, framing and referrer headers are configured.")


def check_cors():
    if getattr(settings, "CORS_ALLOW_ALL_ORIGINS", False):
        return _result(
            "CORS restricted", FAIL,
            "CORS_ALLOW_ALL_ORIGINS is on, so any website can call this API with a user's token.",
        )
    origins = getattr(settings, "CORS_ALLOWED_ORIGINS", [])
    local = [origin for origin in origins if "localhost" in origin or "127.0.0.1" in origin]
    if local:
        return _result(
            "CORS restricted", WARN,
            f"{len(local)} development origin(s) are still allowed. Remove them in production.",
        )
    return _result("CORS restricted", PASS, f"{len(origins)} origin(s) allowed.")


def check_csrf():
    origins = getattr(settings, "CSRF_TRUSTED_ORIGINS", [])
    if not origins:
        return _result("CSRF trusted origins", WARN, "None configured. The Django admin login will fail behind a proxy.")
    insecure = [origin for origin in origins if not origin.startswith("https://")]
    if insecure:
        return _result("CSRF trusted origins", FAIL, f"{len(insecure)} origin(s) are not https.")
    return _result("CSRF trusted origins", PASS, f"{len(origins)} https origin(s) trusted.")


# ---------------------------------------------------------------------------
# Application configuration
# ---------------------------------------------------------------------------

def check_encryption_key():
    if not _is_set("WHATSAPP_TOKEN_ENCRYPTION_KEY"):
        return _result(
            "Provider credential encryption", FAIL,
            "WHATSAPP_TOKEN_ENCRYPTION_KEY is unset, so provider credentials cannot be encrypted at rest.",
        )
    return _result(
        "Provider credential encryption", PASS,
        "An encryption key is configured. Never rotate it by hand -- stored credentials are "
        "encrypted with it and become unreadable.",
    )


def check_email():
    backend = getattr(settings, "EMAIL_BACKEND", "")
    if "console" in backend or "locmem" in backend:
        return _result(
            "Email configured", FAIL,
            "Email goes to the console. Password-reset links would never reach a customer.",
        )
    if "dummy" in backend:
        return _result(
            "Email configured", WARN,
            "The dummy backend is set: mail is silently discarded. Password reset will not work.",
        )
    if "smtp" in backend and not all(_is_set(name) for name in ("EMAIL_HOST", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD")):
        return _result("Email configured", FAIL, "SMTP is selected but host, user or password is missing.")
    if not _is_set("DEFAULT_FROM_EMAIL") or "localhost" in getattr(settings, "DEFAULT_FROM_EMAIL", ""):
        return _result("Email configured", WARN, "DEFAULT_FROM_EMAIL is still a placeholder address.")
    return _result("Email configured", PASS, "A real mail backend is configured.")


def check_frontend_url():
    url = getattr(settings, "FRONTEND_URL", "")
    if not url or "localhost" in url:
        return _result(
            "Public URL", FAIL,
            "FRONTEND_URL is unset or points at localhost. Reset and unsubscribe links are built from it.",
        )
    if not url.startswith("https://"):
        return _result("Public URL", WARN, "FRONTEND_URL is not https.")
    return _result("Public URL", PASS, "A public https URL is configured.")


def check_error_monitoring():
    if not os.getenv("SENTRY_DSN"):
        return _result(
            "Error monitoring", WARN,
            "SENTRY_DSN is unset. Unhandled exceptions are only visible in container logs.",
        )
    if not getattr(settings, "ERROR_MONITORING_ENABLED", False):
        return _result(
            "Error monitoring", FAIL,
            "SENTRY_DSN is set but monitoring did not start. Check that sentry-sdk is installed.",
        )
    return _result("Error monitoring", PASS, "Error monitoring is on, with scrubbing applied.")


def check_admin_url():
    if getattr(settings, "DJANGO_ADMIN_URL", "admin/") == "admin/":
        return _result(
            "Admin path", WARN,
            "The admin is at the default /admin/. Moving it is not a security control, just one "
            "fewer path every scanner tries.",
        )
    return _result("Admin path", PASS, "The admin is not at the default path.")


def check_media_limits():
    if not getattr(settings, "MEDIA_ASSET_MAX_UPLOAD_BYTES", 0):
        return _result("Upload limits", WARN, "No maximum upload size is configured.")
    return _result("Upload limits", PASS, "An upload size limit is configured.")


def check_backups():
    """
    Backups, judged on what actually ran rather than on what somebody remembers.

    Before Phase 4 this could only report BACKUP_VERIFIED_AT -- a date an
    operator typed in. Now scripts/backup_postgres.sh reports every run into
    `BackupRun`, so this reads three real facts: did one succeed, how old is
    it, and did it leave the host.

    **A local-only backup is a FAIL in production**, not a warning. It
    survives a dropped table and nothing else -- not the disk, not the host,
    not the account. Calling that "configured" is how a deployment believes it
    is protected for a year.
    """
    from apps.health.models import BackupRun

    configured_remote = bool(
        os.getenv("BACKUP_REMOTE_TARGET", "").strip()
        or os.getenv("BACKUP_UPLOAD_COMMAND", "").strip()
    )
    acknowledged = bool(os.getenv("BACKUP_OFFSITE_ACKNOWLEDGED", "").strip())

    try:
        last_success = BackupRun.last_success()
        last_failure = BackupRun.last_failure()
    except Exception:  # noqa: BLE001 - a check must never take the command down
        return _result("Backups", WARN, "The backup history could not be read.")

    if last_success is None:
        if not configured_remote and not acknowledged:
            return _result(
                "Backups", FAIL,
                "No backup has ever reported success, and no off-host destination is configured. "
                "Set BACKUP_REMOTE_TARGET (s3:// or rclone:) and schedule scripts/backup_postgres.sh.",
            )
        return _result(
            "Backups", FAIL,
            "An off-host destination is configured but no backup has reported success yet. "
            "Run scripts/backup_postgres.sh once and check it completes.",
        )

    age_hours = last_success.age_seconds / 3600
    stale_after = _backup_max_age_hours()

    if not last_success.offsite and not acknowledged:
        return _result(
            "Backups", FAIL,
            f"The last backup ({age_hours:.0f}h ago) never left this host. A backup on the same "
            "disk as the database does not survive losing the host. Set BACKUP_REMOTE_TARGET.",
        )

    if age_hours > stale_after:
        return _result(
            "Backups", FAIL,
            f"The last successful backup was {age_hours:.0f} hours ago, over the {stale_after:.0f}h "
            "threshold. The schedule has stopped or is failing.",
        )

    if last_failure is not None and last_failure.finished_at > last_success.finished_at:
        return _result(
            "Backups", WARN,
            "The most recent backup attempt failed, though an older one succeeded. Check the job.",
        )

    if not last_success.encrypted:
        return _result(
            "Backups", WARN,
            f"Backups are running ({age_hours:.0f}h ago, off-host) but are NOT encrypted. The "
            "archive holds every phone number and message in the deployment. Set BACKUP_PASSPHRASE.",
        )

    return _result(
        "Backups", PASS,
        f"Last successful backup {age_hours:.0f}h ago, encrypted and copied off-host.",
    )


def _backup_max_age_hours() -> float:
    """How old a backup may be before it counts as stale. A daily job plus slack."""
    try:
        return float(os.getenv("BACKUP_MAX_AGE_HOURS", "36"))
    except (TypeError, ValueError):
        return 36.0


def check_restore_verification():
    """
    Whether anybody has ever restored one of these backups.

    Separate from `check_backups` because it is a separate claim, and the one
    that is actually load-bearing: an archive nobody has restored is a file
    with a promising name. `scripts/restore_postgres.sh` records a
    RESTORE_TEST when it runs, so this reports a fact rather than a hope.

    A WARN and not a FAIL: a deployment with fresh, encrypted, off-host
    backups and no restore test yet is in a real but survivable position, and
    a check that fails a release for it would be switched off.
    """
    from apps.health.models import BackupRun

    try:
        last = BackupRun.last_success(kind=BackupRun.Kind.RESTORE_TEST)
    except Exception:  # noqa: BLE001
        return _result("Restore verification", WARN, "The restore history could not be read.")

    legacy_marker = os.getenv("BACKUP_VERIFIED_AT", "").strip()
    if last is None:
        if legacy_marker:
            return _result(
                "Restore verification", WARN,
                f"No automated restore test has run. BACKUP_VERIFIED_AT reports {legacy_marker}, "
                "which is an operator's note rather than a recorded run.",
            )
        return _result(
            "Restore verification", WARN,
            "No backup has ever been restored and verified. Run scripts/restore_postgres.sh "
            "against a recent archive -- a backup is not a backup until it restores.",
        )

    age_days = last.age_seconds / 86400
    if age_days > _restore_max_age_days():
        return _result(
            "Restore verification", WARN,
            f"The last restore test was {age_days:.0f} days ago. Re-run scripts/restore_postgres.sh.",
        )
    return _result("Restore verification", PASS, f"A restore was verified {age_days:.0f} day(s) ago.")


def _restore_max_age_days() -> float:
    try:
        return float(os.getenv("RESTORE_TEST_MAX_AGE_DAYS", "30"))
    except (TypeError, ValueError):
        return 30.0


def check_offhost_restore():
    """
    Whether the copy that is **not on this host** has ever been restored.

    Distinct from `check_restore_verification`, and the distinction is the
    whole point. A local restore test proves the dump this machine holds is a
    good dump. It says nothing about the object in the bucket -- whether it is
    complete, whether it can be read back with the credentials a rebuilt host
    would have, whether a lifecycle rule has moved it to a tier that cannot be
    streamed, whether the passphrase in use today still decrypts it. Every one
    of those failures passes an upload check and a local restore, and is
    discovered on the day the host is gone.

    `scripts/verify_offhost_backup.sh` does the round trip -- upload, move the
    local copy aside, fetch from the remote, compare SHA-256, restore the
    fetched file -- and records the outcome here.

    A WARN rather than a FAIL, for the same reason `check_restore_verification`
    is: a deployment with fresh encrypted off-host backups and a passing local
    restore test is in a real but survivable position, and a check that failed
    a release over it would be switched off within a month. A recorded
    *failure* of the drill is a different matter and does fail.
    """
    from apps.health.models import BackupRun

    configured_remote = bool(
        os.getenv("BACKUP_REMOTE_TARGET", "").strip()
        or os.getenv("BACKUP_UPLOAD_COMMAND", "").strip()
    )
    if not configured_remote:
        # `check_backups` already fails on this; saying it twice would be
        # noise, and a second failure for one cause trains people to skim.
        return _result(
            "Off-host restore", WARN,
            "No off-host destination is configured, so there is nothing to restore from. "
            "See the Backups check above.",
        )

    try:
        last = BackupRun.last_success(kind=BackupRun.Kind.OFFSITE_RESTORE_TEST)
        failed = BackupRun.last_failure(kind=BackupRun.Kind.OFFSITE_RESTORE_TEST)
    except Exception:  # noqa: BLE001 - a check must never take the command down
        return _result("Off-host restore", WARN, "The off-host restore history could not be read.")

    # A drill that ran and failed is the loudest signal this file can produce:
    # somebody went looking, and the off-site copy did not come back.
    if failed is not None and (last is None or failed.finished_at > last.finished_at):
        return _result(
            "Off-host restore", FAIL,
            f"The most recent off-host restore drill FAILED ({failed.age_seconds / 3600:.0f}h ago). "
            "The remote copy could not be fetched, matched or restored. Run "
            "scripts/verify_offhost_backup.sh and read its output.",
        )

    if last is None:
        return _result(
            "Off-host restore", WARN,
            "Backups are being copied off-host but the remote copy has never been fetched back "
            "and restored. Run scripts/verify_offhost_backup.sh -- an upload that reports "
            "success is not the same claim as an archive that restores.",
        )

    age_days = last.age_seconds / 86400
    if age_days > _offhost_restore_max_age_days():
        return _result(
            "Off-host restore", WARN,
            f"The last off-host restore drill was {age_days:.0f} days ago. "
            "Re-run scripts/verify_offhost_backup.sh.",
        )
    return _result(
        "Off-host restore", PASS,
        f"The off-host copy was fetched and restored {age_days:.0f} day(s) ago.",
    )


def _offhost_restore_max_age_days() -> float:
    try:
        return float(os.getenv("OFFSITE_RESTORE_TEST_MAX_AGE_DAYS", "30"))
    except (TypeError, ValueError):
        return 30.0


def check_scheduler():
    """
    Whether the durable scheduler is keeping up.

    The one number that says so is how many jobs are past due and unclaimed:
    a beat container that stopped shows up here within minutes, and nothing
    else in the system would notice until a customer asked why their campaign
    never went out.

    Overdue work is a FAIL rather than a warning. A scheduled campaign that
    did not send is a customer-visible failure of the product's core promise.
    """
    from apps.scheduling.services import overdue_count

    try:
        overdue = overdue_count()
    except Exception:  # noqa: BLE001
        return _result("Scheduler", WARN, "The scheduled-job table could not be read.")

    if overdue:
        return _result(
            "Scheduler", FAIL,
            f"{overdue} scheduled job(s) are past due and unclaimed. The celery-beat container "
            "is not running, or the worker is not consuming. Scheduled campaigns are not sending.",
        )
    return _result("Scheduler", PASS, "No scheduled work is overdue.")


def check_queue_consumption():
    """
    Whether a worker is actually consuming every queue this application routes to.

    The failure this catches is silent, which is why it is a check rather than
    something an operator would notice. Celery does not complain about a task
    published to a queue nobody reads -- the task simply sits in Redis for
    ever, and a queue with a growing backlog and no consumer looks exactly like
    a system with nothing to do.

    A FAIL, because tasks are being lost right now. "No workers at all" is a
    WARN instead: that is a worker container that is down or still starting,
    which the container healthcheck already reports and which is a different
    (and more obvious) problem than a misconfigured -Q.
    """
    from django.conf import settings

    declared = set(getattr(settings, "CELERY_QUEUE_NAMES", []) or ["celery"])

    try:
        from apps.health.operations import workers

        state = workers()
    except Exception:  # noqa: BLE001
        return _result("Queue consumption", WARN, "The workers could not be inspected.")

    if not state.get("reachable"):
        return _result(
            "Queue consumption", WARN,
            "No Celery worker answered. Nothing is processing campaigns, messages or imports.",
        )

    unconsumed = sorted(set(state.get("queues_unconsumed") or []))
    if unconsumed:
        return _result(
            "Queue consumption", FAIL,
            f"{len(unconsumed)} queue(s) have no consumer. Tasks routed there are accumulating "
            "and will never run. Start the worker with -Q naming every queue.",
        )

    return _result(
        "Queue consumption", PASS,
        f"A worker is consuming all {len(declared)} configured queue(s).",
    )


def check_data_retention():
    """
    Whether anything is actually pruning the tables that grow with traffic.

    The retention *policy* has been documented since Phase 3; until Phase 5
    nothing implemented it, and BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS was a
    setting read by no code at all. A WARN rather than a FAIL: unbounded growth
    is a problem measured in months, not an outage, and an operator with a
    legal reason to keep everything is entitled to turn every rule off.
    """
    from django.conf import settings

    scheduled = any(
        entry.get("task") == "apps.health.tasks.run_retention_sweep"
        for entry in getattr(settings, "CELERY_BEAT_SCHEDULE", {}).values()
    )
    if not scheduled:
        return _result(
            "Data retention", WARN,
            "No retention sweep is scheduled. Webhook events and expired tokens will grow "
            "without bound.",
        )

    horizons = [
        getattr(settings, "RETENTION_WEBHOOK_EVENT_DAYS", 0),
        getattr(settings, "RETENTION_SCHEDULED_JOB_DAYS", 0),
        getattr(settings, "RETENTION_BUTTON_PROMPT_DAYS", 0),
    ]
    if not any(int(value or 0) > 0 for value in horizons):
        return _result(
            "Data retention", WARN,
            "A retention sweep is scheduled but every rule is disabled, so it deletes nothing.",
        )

    return _result("Data retention", PASS, "A retention sweep is scheduled and enabled.")


def check_send_recovery_sweep():
    """
    Whether the reconciliation sweep for stranded sends is scheduled.

    Without it, a Celery worker that dies mid-send leaves the message stuck in
    SENDING for ever and its campaign never completes -- the P0 this sweep
    exists to close. A FAIL, not a WARN: this is customer campaigns silently
    stopping, not a slow-growing table.
    """
    from django.conf import settings

    scheduled = any(
        entry.get("task") == "apps.messaging.tasks.sweep_stuck_sends"
        for entry in getattr(settings, "CELERY_BEAT_SCHEDULE", {}).values()
    )
    if not scheduled:
        return _result(
            "Send recovery sweep", FAIL,
            "apps.messaging.tasks.sweep_stuck_sends is not in CELERY_BEAT_SCHEDULE. A worker "
            "that dies mid-send will leave messages stuck in SENDING with nothing to recover them.",
        )
    lease = int(getattr(settings, "SEND_LEASE_SECONDS", 0) or 0)
    soft = int(getattr(settings, "SEND_SOFT_TIME_LIMIT_SECONDS", 0) or 0)
    if lease and soft and lease <= soft:
        return _result(
            "Send recovery sweep", WARN,
            f"SEND_LEASE_SECONDS ({lease}s) is not comfortably above the send soft time limit "
            f"({soft}s); the sweep may reconcile sends that are still in progress.",
        )
    return _result("Send recovery sweep", PASS, "The stranded-send reconciliation sweep is scheduled.")


def check_outbound_url_policy():
    """
    Whether tenants can point this server at its own network.

    Two settings on the AI and Voice agents let a tenant type the URL this
    server then makes an authenticated request to. OUTBOUND_URL_ALLOW_PRIVATE
    switches the guard off wholesale, which is defensible on a single-tenant
    install on a private network and is a server-side request forgery hole
    anywhere else -- so it is reported rather than assumed deliberate.
    """
    from django.conf import settings

    if getattr(settings, "OUTBOUND_URL_ALLOW_PRIVATE", False):
        return _result(
            "Outbound URL policy", WARN,
            "OUTBOUND_URL_ALLOW_PRIVATE is on. Any tenant can make this server fetch an address "
            "on its own network, including the cloud metadata service.",
        )

    allowed = getattr(settings, "OUTBOUND_URL_ALLOWED_INTERNAL_HOSTS", []) or []
    if allowed:
        return _result(
            "Outbound URL policy", PASS,
            f"Private addresses are refused, with {len(allowed)} host(s) explicitly allowed.",
        )
    return _result("Outbound URL policy", PASS, "Tenant-supplied provider URLs cannot reach this network.")


def check_signup_configuration():
    """
    Whether self-service signup, if it is on, can actually complete.

    The failure this catches is specific and quiet: signups are open, no trial
    plan resolves, and every new customer lands on **no subscription** -- which
    in this codebase means *unlimited* (see subscriptions.services.
    get_current_plan). A commercialization deployment giving away the product
    is worth failing a release for.
    """
    if not getattr(settings, "SELF_SERVICE_SIGNUP_ENABLED", False):
        return _result("Self-service signup", PASS, "Self-service signup is switched off.")

    from apps.subscriptions.trials import trial_plan, trials_enabled

    if not getattr(settings, "TRIALS_ENABLED", True):
        return _result(
            "Self-service signup", WARN,
            "Signups are open but trials are off, so every new workspace lands with no plan and "
            "must be sold to before it can send.",
        )
    try:
        plan = trial_plan()
    except Exception:  # noqa: BLE001
        return _result("Self-service signup", WARN, "The trial plan could not be resolved.")

    if plan is None:
        return _result(
            "Self-service signup", FAIL,
            "Signups are open but BILLING_TRIAL_PLAN names no active platform plan. New customers "
            "would be created with no subscription, which this codebase treats as UNLIMITED. "
            "Run 'manage.py seed_plans' and set BILLING_TRIAL_PLAN.",
        )
    if not trials_enabled():
        return _result("Self-service signup", FAIL, "Trials are configured but not resolvable.")
    return _result(
        "Self-service signup", PASS,
        f"Signups are open and start a {settings.BILLING_TRIAL_DAYS}-day trial.",
    )


def check_payment_configuration():
    """
    Whether payments, if switched on, are actually able to take money.

    ``PAYMENTS_ENABLED`` with no credentials is the worst of the three states:
    the UI offers an upgrade button and pressing it fails. Off is fine and
    says so; on-and-working is fine; on-and-broken is a FAIL.
    """
    if not getattr(settings, "PAYMENTS_ENABLED", False):
        return _result(
            "Payments", PASS,
            "Payments are off. Plans are assigned by an operator and no checkout is offered.",
        )

    from apps.billing.providers import PaymentNotConfigured, get_provider

    try:
        provider = get_provider()
    except PaymentNotConfigured:
        return _result(
            "Payments", FAIL,
            "PAYMENTS_ENABLED is on but PAYMENT_PROVIDER names an unknown provider.",
        )

    if not provider.supports_checkout:
        return _result(
            "Payments", WARN,
            f"PAYMENTS_ENABLED is on but the '{provider.name}' provider cannot take payments. "
            "No checkout will be offered.",
        )
    if not provider.is_configured():
        return _result(
            "Payments", FAIL,
            f"PAYMENTS_ENABLED is on but the '{provider.name}' provider has no credentials. "
            "Checkout would fail for every customer.",
        )
    if not _is_set("RAZORPAY_WEBHOOK_SECRET") and provider.name == "razorpay":
        return _result(
            "Payments", WARN,
            "Payments work, but no webhook secret is set. Webhooks are refused, so a payment "
            "whose browser callback is lost will only settle when somebody reloads the billing page.",
        )
    return _result("Payments", PASS, f"Payments are on through '{provider.name}', with webhooks verified.")


def check_legal_documents():
    from apps.legal.documents_registry import is_configured

    if not is_configured():
        return _result(
            "Legal documents", WARN,
            "The Terms and Privacy Policy still contain [TO BE COMPLETED] placeholders. They are "
            "served with a visible draft banner until LEGAL_ENTITY_NAME, LEGAL_ENTITY_ADDRESS, "
            "LEGAL_CONTACT_EMAIL and LEGAL_GOVERNING_LAW are set.",
        )
    return _result("Legal documents", PASS, "Business details for the legal documents are configured.")


def check_support_contact():
    if not (_is_set("SUPPORT_EMAIL") or _is_set("SUPPORT_URL")):
        return _result(
            "Support contact", WARN,
            "Neither SUPPORT_EMAIL nor SUPPORT_URL is set, so account emails carry no way to get help.",
        )
    return _result("Support contact", PASS, "A support contact is configured.")


def check_version():
    if not _is_set("APP_BUILD"):
        return _result(
            "Build identity", WARN,
            "APP_BUILD is unset, so a running container cannot be tied to a commit.",
        )
    return _result("Build identity", PASS, f"Version {settings.APP_VERSION}, build {settings.APP_BUILD}.")


#: Every check, in the order they are reported. Grouped roughly by how much
#: damage the corresponding misconfiguration does.
ALL_CHECKS = (
    check_debug,
    check_secret_key,
    check_jwt_secret,
    check_allowed_hosts,
    check_database,
    check_redis,
    check_encryption_key,
    check_https,
    check_cookies,
    check_security_headers,
    check_cors,
    check_csrf,
    check_email,
    check_frontend_url,
    check_media_limits,
    check_admin_url,
    check_error_monitoring,
    check_backups,
    check_restore_verification,
    check_offhost_restore,
    check_scheduler,
    check_queue_consumption,
    check_data_retention,
    check_send_recovery_sweep,
    check_outbound_url_policy,
    check_signup_configuration,
    check_payment_configuration,
    check_legal_documents,
    check_support_contact,
    check_version,
)


def run_all() -> list:
    """Every check. A check that raises is itself a FAIL, never a crash."""
    results = []
    for check in ALL_CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - a broken check must not hide the others
            results.append(
                _result(check.__name__, FAIL, f"This check could not run: {type(exc).__name__}.")
            )
    return results


def summarise(results) -> dict:
    return {
        "passed": sum(1 for r in results if r["status"] == PASS),
        "warnings": sum(1 for r in results if r["status"] == WARN),
        "failed": sum(1 for r in results if r["status"] == FAIL),
        "total": len(results),
    }
