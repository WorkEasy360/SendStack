# Release runbook

What to do, in order, to put a new version of SendStack in front of
customers — and what to do when it goes wrong.

Written to be followed by somebody who did not write the change, at a time
when something is already broken. Every step says what "good" looks like, so
there is never a judgement call about whether to continue.

**The one rule that overrides everything below:** if a step's check does not
pass, stop and roll back. A half-finished deploy that is left running because
"it is probably fine" is how a five-minute rollback becomes a two-hour
incident.

---

## 0. Before you start

- [ ] The change is merged and CI is green on that commit.
- [ ] You know which commit is currently deployed (`docker compose exec backend
      python manage.py shell -c "from apps.health.checks import check_version;
      print(check_version())"`), and you have written it down. **This is your
      rollback target.** Finding it out afterwards, under pressure, is the
      thing this line exists to prevent.
- [ ] You have at least 45 minutes. A deploy started ten minutes before you
      have to leave is a deploy nobody can roll back.
- [ ] Nobody else is deploying.

---

## 1. Pre-release checks

Run against the **staging** stack, or against production before touching it.

```bash
# Configuration: fails the release on anything unsafe for production.
docker compose exec backend python manage.py check_production
```

**Good:** `No blocking failures.`
**Stop if:** any `FAIL`. Warnings are judgement; failures are not.

```bash
# Migrations: no model change has been left without one.
docker compose exec backend python manage.py makemigrations --check --dry-run
```

**Good:** `No changes detected.`
**Stop if:** it wants to write a migration. Deploying with an unmade migration
means the next person's `makemigrations` produces a file that depends on
something that was never applied.

```bash
# The full suite, serially. Never two runs at once -- they share one test
# database name and the second drops the schema underneath the first.
docker compose exec backend python manage.py test apps tests --noinput
```

**Good:** `OK`, with a test count **not lower** than the last known-good run.
A lower count is itself evidence the run was poisoned — modules that fail at
import are counted as one error each and their tests never run.

---

## 2. Backup

**Always. Before every deploy, including the ones that "only change the
frontend".** A migration that turns out to be wrong is unrecoverable without
one, and the cost of taking it is two minutes.

```bash
./scripts/backup_postgres.sh
```

**Good:** exits 0, prints `Off-host copy verified.`, and the archive appears
in `backups/`.
**Stop if:** it exits non-zero. A configured off-host destination that fails
to accept the upload fails the whole run by design — do not deploy behind a
backup that did not leave the host.

Confirm the application agrees:

```bash
curl -s localhost/health/backups/ -H "Authorization: Bearer $TOKEN" | jq .
```

**Good:** `last_success` is minutes old and `offsite: true`.

---

## 3. Build

The repository lives under OneDrive on the development machine, where
Files-On-Demand placeholders make BuildKit refuse the context. Build from a
copy outside it:

```bash
robocopy . /tmp/ms-build /E /XD __pycache__ .venv media staticfiles node_modules \
    /XF db.sqlite3 *.pyc
cd /tmp/ms-build
docker build -t multisender-backend:latest \
             -t multisender-celery-worker:latest \
             -t multisender-celery-beat:latest .
```

`robocopy` exits **1** on success — do not treat that as a failure.

**All three backend images must be built together.** They share a build
context but Compose tags each separately, so rebuilding only `backend` leaves
the worker running the old code and the fix appears not to work.

Any frontend change also needs nginx, which bakes the built SPA into its
image:

```bash
docker compose build nginx
```

**Good:** three backend images with the same image id
(`docker compose images`), and an nginx image newer than the frontend change.

---

## 4. Migrate

```bash
docker compose exec backend python manage.py migrate
```

**Good:** every migration applies, or `No migrations to apply.`
**Stop if:** a migration fails partway. Restore from the archive taken in
step 2 — do not attempt to hand-repair a partially migrated schema.

Migrations run **before** the new containers serve traffic, which means the
old code runs briefly against the new schema. Additive migrations are safe;
a migration that drops or renames a column the running code still reads is
not, and must be split across two releases.

---

## 5. Deploy

```bash
docker compose up -d --no-build
docker compose restart nginx
```

The nginx restart is not optional. Nginx resolves the `backend` upstream once
at startup, so a recreated backend container gets a new IP and every `/api/`
call returns 502 while the SPA still loads — which reads as a frontend bug and
sends people looking in the wrong place.

---

## 6. Health check

```bash
curl -sf localhost/health/ready/    # dependencies reachable
curl -sf localhost/health/live/     # process up
```

**Good:** both return 200.
**Stop if:** `ready` fails for more than a minute. It checks the database and
Redis; a failure here means the new containers cannot reach something they
need.

```bash
docker compose exec backend python manage.py shell -c \
    "from apps.health.checks import check_version; print(check_version())"
```

**Good:** the version reported is the commit you intended to deploy. If it is
the old one, the image did not rebuild — see step 3.

---

## 7. Billing verification

The subsystem where a silent break costs money rather than uptime.

- [ ] `GET /api/v1/billing/pricing/` returns the expected plans and prices.
- [ ] `GET /api/v1/billing/overview/` renders for a real workspace.
- [ ] The reconciliation report is clean:
      `GET /api/v1/reports/business/reconciliation/` → `clean: true`.
      A non-empty `captured_without_active_subscription` means somebody paid
      and did not get what they paid for. That is a stop-and-investigate.
- [ ] Webhook endpoint reachable from outside:
      `curl -i https://<host>/api/v1/billing/webhook/razorpay/` returns 400 or
      401 (a signature failure), **not** 404 or 502. A 404 here means every
      payment confirmation is being dropped, and nothing else will tell you.

---

## 8. Celery verification

```bash
docker compose exec backend celery -A config inspect ping
docker compose exec backend celery -A config inspect active_queues
```

**Good:** every worker replies, and the queue list covers all of
`celery, messages, campaigns, bulk, maintenance`.
**Stop if:** a queue is missing from the workers' list. A queue nobody
consumes is one where tasks vanish silently — messages queue and never send,
with no error anywhere.

```bash
curl -s localhost/health/operations/ -H "Authorization: Bearer $TOKEN" | jq .
```

**Read `queues_unconsumed` first.** It is the field that catches the failure
mode above, and it is why the operations dashboard exists.

Also check the scheduler is not backing up: `scheduler.overdue` should be near
zero. A beat container that did not come back shows here within minutes, and
scheduled campaigns silently stop going out.

---

## 9. Post-deploy monitoring

For **30 minutes** after the deploy:

- [ ] Error rate in the error monitor — a new exception type is the signal,
      not the absolute count.
- [ ] `GET /health/dead-letters/` — jobs that gave up. A burst right after a
      deploy usually means a task signature changed and in-flight jobs cannot
      be deserialised.
- [ ] Queue depths not growing monotonically.
- [ ] One real campaign completes end to end, if any are scheduled.

---

## 10. Rollback

Do this **as soon as** a check fails. Rolling back is cheap; deciding whether
to roll back while a queue backs up is not.

```bash
# 1. Put the previous images back.
docker compose down
git checkout <previous-commit>
# rebuild per step 3, or retag the previous images if still present
docker compose up -d --no-build
docker compose restart nginx
```

**If migrations ran and must be undone**, prefer restoring the backup from
step 2 over reversing migrations by hand:

```bash
./scripts/restore_postgres.sh backups/<archive> \
    --target multisender --i-understand-this-overwrites
```

Reverse migrations are frequently lossy — a dropped column does not come back
because the migration is reversible in Django's opinion. The archive is the
only thing that restores what was actually there.

**Rolling back application code alone is safe when the migrations were
additive**, which is the normal case. Additive means: new tables, new nullable
columns, new indexes. Nothing removed and nothing renamed.

---

## Appendix: the drills, and when to run them

These are not part of a release. They are the periodic exercises that make a
release safe, and each has its own script.

| Drill | Command | Cadence |
|---|---|---|
| Off-host backup restore | `./scripts/verify_offhost_backup.sh` | Monthly |
| Local restore test | `./scripts/restore_postgres.sh <archive>` | Weekly |
| Concurrent load test | `python scripts/loadtest.py --users 40 --duration 180` | Before a capacity change |
| Data-scale benchmark | `manage.py benchmark_api --runs 20 --compare baseline.json` | Before and after a query change |
| Redis loss | `docker compose stop redis` → wait → `start` | Quarterly |
| Worker crash | `docker compose kill celery-worker` → `up -d` | Quarterly |

The load test needs a seeded staging database and **must never run against
production**: `seed_load_data` refuses to, and `loadtest.py` never sends a
WhatsApp message, but the requests themselves are real load.
