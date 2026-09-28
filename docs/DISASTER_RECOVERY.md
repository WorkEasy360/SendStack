# Disaster recovery runbook

What to do when part of the deployment is down. Each scenario has the same five
steps, in the same order, so that under pressure there is nothing to work out.

**Before you start anything below:** note the time, and write down what you
observe before you change it. A recovery that destroys the evidence of the
cause guarantees the same outage again next month.

---

## Recovery objectives

These are the **actual current capability**, not a target somebody would like.
They are honest, and two of them are worse than a customer would want.

| | Value | Why it is this and not better |
| --- | --- | --- |
| **RPO** (how much data a disaster can lose) | **Up to 24 hours** with the default daily backup | The only recovery point is the last `pg_dump`. There is no WAL archiving and no streaming replica, so anything written after the last backup is gone. Hourly backups reduce this to an hour; continuous archiving (`pgBackRest`, or a managed Postgres with PITR) reduces it to minutes and is the right answer before onboarding customers who cannot lose a day. |
| **RTO** (how long recovery takes) | **1–4 hours** for a full rebuild on a new host | Provision a host, install Docker, clone the repository, restore the environment file, restore the database, start the stack. Most of that is provisioning. A restore into an already-running stack is 5–20 minutes depending on database size. |
| **RTO for a single container** | **Under 5 minutes** | `docker compose restart <service>`; `restart: unless-stopped` handles most of these without a human. |

**Not claimed:** high availability, automatic failover, zero data loss, or
zero-downtime deployment. This is a single-host Docker Compose deployment. A
host failure is an outage measured in hours, and a deployment is a brief
maintenance interruption. Say that to customers rather than something better.

---

## Automated off-host backups

The single most important thing in this document: **a backup on the same disk
as the database is not a backup.** It survives a dropped table and nothing
else — not the disk, not the host, not the account, not ransomware.

### Setting it up

`scripts/backup_postgres.sh` dumps, verifies, encrypts, uploads and reports.
Point it at a destination and schedule it:

```bash
# In .env -- credentials never live in this repository.
BACKUP_PASSPHRASE=<a long random passphrase, stored in your password manager>
BACKUP_REMOTE_TARGET=s3://my-bucket/sendstack      # or rclone:remote:path
BACKUP_RETENTION_DAYS=14
BACKUP_MIN_KEEP=3
BACKUP_ALERT_COMMAND='mail -s "SendStack backup failed" ops@example.com'
```

```bash
# Daily at 02:15, on the Docker host.
15 2 * * *  cd /srv/sendstack && ./scripts/backup_postgres.sh >> /var/log/sendstack-backup.log 2>&1
```

Credentials for the destination come from the environment, an instance role,
or `~/.aws` / `rclone.conf` — never from this repository, and never on a
command line where `ps` can read them.

### Destinations

| `BACKUP_REMOTE_TARGET` | Uses | Verified by |
|---|---|---|
| `s3://bucket/prefix` | AWS CLI | `head-object`, byte count compared |
| `rclone:remote:path` | rclone | `rclone check --size-only` |
| anything else | set `BACKUP_UPLOAD_COMMAND` instead | your command's exit code |

`BACKUP_UPLOAD_COMMAND` substitutes `{src}` and `{name}` and runs verbatim.
That is the escape hatch for a destination this script has never heard of.

### What the script guarantees

* **A configured destination that fails to upload fails the whole run**, with a
  non-zero exit. There is no silent fallback to "well, it is on the local
  disk" — that is how a deployment believes it has off-site backups for a year
  and finds out it does not on the day the host dies.
* **The last known-good backups are never pruned.** Retention is age-based
  *with a floor* (`BACKUP_MIN_KEEP`, default 3). Under pure age-based pruning,
  a deployment whose backups had been failing for three weeks would delete its
  last good one on the day somebody needed it.
* **The encryption key is not stored beside the backup.** `BACKUP_PASSPHRASE`
  lives in the environment; the archive goes to object storage. Losing the
  passphrase means losing the backups, so keep it in a password manager and
  not only in `.env`.
* **Failures are loud.** Every non-clean exit runs `BACKUP_ALERT_COMMAND` and
  records a `FAILED` row. A backup system whose failures are silent is not a
  backup system, it is a cron job that used to work.

### Monitoring

The script reports every run — success or failure — into the application:

```bash
# Operators only. Ages and outcomes; never a path, a bucket or a credential.
curl -H "Authorization: Bearer $TOKEN" https://your-host/health/backups/

# Or as part of the release gate:
docker compose exec backend python manage.py check_production
```

`check_production` FAILs when:

* no backup has ever succeeded, or
* the last one never left the host (unless `BACKUP_OFFSITE_ACKNOWLEDGED=1`), or
* the last successful one is older than `BACKUP_MAX_AGE_HOURS` (default 36).

It WARNs when backups run but are unencrypted, or when the most recent attempt
failed after an older success.

### Restore verification

**A backup nobody has restored is a file with a promising name.**

```bash
# Restores into a throwaway database, checks the tables, prints row counts,
# drops it. It never touches production without --i-understand-this-overwrites.
./scripts/restore_postgres.sh backups/multisender-<stamp>.dump.gpg
```

Run it monthly. `check_restore_verification` WARNs when the last recorded
restore test is older than `RESTORE_TEST_MAX_AGE_DAYS` (default 30), and warns
that nothing has ever been restored when that is the case.

### What this does and does not change

| | Before Phase 4 | After |
|---|---|---|
| Backup exists | manual script run | scheduled, automated |
| Survives host loss | **no** | yes, once a destination is configured |
| Failure visible | no | alert command + `FAILED` row + failing production check |
| "Are we protected?" | an operator's memory (`BACKUP_VERIFIED_AT`) | a query over recorded runs |
| RPO | ~24h | ~24h, unchanged — see the objectives table above |

RPO is **unchanged** by this work. Backups now leave the host and are watched;
they are still point-in-time dumps on a daily schedule. Continuous archiving
(`pgBackRest`, or managed Postgres with PITR) is what moves RPO to minutes, and
it remains the right answer before onboarding customers who cannot lose a day.

---

## Scenario 1 — PostgreSQL failure

### Detection
- `/health/ready/` returns 503 with `"database": false`.
- The backend container is unhealthy; `docker compose ps` shows postgres as
  unhealthy, restarting, or exited.
- Application logs fill with `OperationalError`.

### Immediate action
```bash
docker compose ps
docker compose logs --tail=200 postgres
df -h                      # a full disk is the most common cause
```
Do **not** run `docker compose down -v`. The `-v` deletes the volume, which is
the database.

### Recovery
Container stopped but the volume is intact — the ordinary case:
```bash
docker compose up -d postgres
docker compose logs -f postgres     # wait for "database system is ready"
docker compose restart backend celery-worker celery-beat
```

Disk full:
```bash
docker system prune -f              # reclaim images and build cache, never volumes
# then free space outside Docker, and only then restart postgres
```

Data corrupted, or the volume is gone — restore from backup:
```bash
ls -lt backups/
./scripts/restore_postgres.sh backups/<newest>.dump.gpg --target multisender \
    --i-understand-this-overwrites
docker compose up -d
docker compose exec backend python manage.py migrate --noinput
```

### Verification
```bash
curl -f http://localhost/health/ready/
docker compose exec backend python manage.py showmigrations | grep -c '\[X\]'
docker compose exec backend python manage.py shell -c \
  "from apps.tenants.models import Tenant; from apps.contacts.models import Contact; \
   print(Tenant.objects.count(), Contact.objects.count())"
```

### Post-recovery check
- How much data was lost? (Backup timestamp → failure time. Tell customers.)
- Are campaigns stuck in `RUNNING` with unsent messages? Resume them; the send
  gate re-checks consent, quota and credits per message, so resuming is safe.
- Why did it fail? Disk, memory, or corruption each need a different fix.

---

## Scenario 2 — Redis failure

Redis holds the cache, rate-limit counters, failed-login counters and the
**Celery queue**. It holds no customer data — that is all in PostgreSQL.

### Detection
- `/health/ready/` returns 503 with `"redis": false`.
- Nothing is sending; messages sit in `QUEUED`.

### Immediate action
```bash
docker compose logs --tail=100 redis
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" ping
```

### Recovery
```bash
docker compose up -d redis
docker compose restart celery-worker celery-beat backend
```

### Verification
```bash
curl -f http://localhost/health/ready/
docker compose exec backend celery -A config inspect ping
```

### Post-recovery check — read this part, it is the one that loses messages

**Queued Celery tasks do not survive losing Redis.** Redis persistence is
best-effort (AOF/RDB), so tasks accepted since the last flush are gone. What
that means in practice:

| Work | Survives? | Recovery |
| --- | --- | --- |
| Messages already `QUEUED` in PostgreSQL | The **row** survives; the task that would send it may not | Resume or retry the campaign — it re-dispatches from the rows |
| A campaign scheduled for the future (`apply_async(eta=...)`) | **No.** The ETA lives only in Redis | Re-launch it, or move `scheduled_at` and launch again |
| Messages being held for quiet hours | **No** — a hold is a task with a countdown | Resume the campaign |
| Workflow runs mid-step | Yes — `sweep_stuck_workflow_runs` picks them up within 5 minutes |
| Contact imports in progress | **No** | Re-upload |

```sql
-- What is stranded
SELECT c.id, c.name, c.status, count(m.id) FILTER (WHERE m.status='QUEUED') AS queued
FROM campaigns_campaign c LEFT JOIN messaging_message m ON m.campaign_id = c.id
WHERE c.status IN ('QUEUED','RUNNING') GROUP BY c.id ORDER BY c.id;
```

Then, per campaign, POST to its `resume` action. This is safe: the send gate
re-evaluates suspension, consent, opt-out, quiet hours, plan quota, credits and
sender health at the moment of each send, so a message re-dispatched after an
outage is authorised against *now*, not against when it was scheduled.

**Known limitation, deliberately not fixed in Phase 3:** there is no reconciler
that finds stranded scheduled campaigns automatically. Recovery is the manual
query above. Fixing it properly means a durable scheduled-job table rather than
trusting the broker, which is a redesign of the campaign scheduler.

---

## Scenario 3 — Backend (Django/gunicorn) failure

### Detection
- nginx returns 502.
- `/health/live/` does not answer.

### Immediate action
```bash
docker compose ps backend
docker compose logs --tail=200 backend
```
A container that exits immediately on start is almost always a configuration
error — production settings `raise RuntimeError` for a missing `SECRET_KEY`,
`ALLOWED_HOSTS`, email configuration or `FRONTEND_URL`. The log line names the
variable and never its value.

### Recovery
```bash
docker compose up -d backend
# configuration error:
docker compose exec backend python manage.py check_production
```

### Verification
```bash
curl -f http://localhost/health/live/
curl -f http://localhost/health/ready/
curl -s http://localhost/health/ | python -m json.tool   # confirm version/build
```

### Post-recovery check
- Did it OOM? `docker inspect multisender-backend-1 | grep OOMKilled`.
- Were requests lost? gunicorn had no graceful drain, so in-flight requests
  failed. Customers may need to retry; nothing was half-charged, because credit
  reservation and message-allowance claims are transactional.

---

## Scenario 4 — Celery failure

### Detection
- Messages stay `QUEUED`, nothing sends.
- `celery -A config inspect ping` returns nothing.

### Immediate action
```bash
docker compose logs --tail=200 celery-worker
docker compose exec backend celery -A config inspect active
docker compose exec backend celery -A config inspect reserved
```

### Recovery
```bash
docker compose restart celery-worker celery-beat
```

Tasks are `acks_late=True`, so a task interrupted mid-execution is redelivered
rather than lost. That is why idempotency matters and why it is implemented:

- `claim_message_allowance` stamps `plan_counted_at` on the message row, so a
  redelivered task does not spend a second message from the plan.
- Credit reservation is keyed on a per-campaign reference.
- A message already `SENT` is not re-sent — `send_message_task` returns early.
- A provider failure that is *ambiguous* (the message may have been delivered)
  is terminal rather than retried, precisely so a retry cannot double-send.

### Verification
```bash
docker compose exec backend celery -A config inspect ping
```
Then watch a `QUEUED` message move to `SENT`.

### Post-recovery check
- Beat missed its schedule while down. `reap_stale_calls`,
  `sweep_stuck_workflow_runs` and `refresh_crm_signals` all catch up on the
  next tick; none of them accumulate a backlog.
- Check for messages held far longer than intended: the gate blocks anything
  held past `MESSAGE_MAX_HOLD_SECONDS` (48h default) rather than sending it
  late, which is the correct outcome but worth telling the customer about.

---

## Scenario 5 — QR bridge failure

The bridge holds live device-linked WhatsApp sessions. Cloud API accounts are
unaffected.

### Detection
- Device-linked numbers show `DISCONNECTED`.
- `docker compose ps qr-bridge` unhealthy.

### Immediate action
```bash
docker compose logs --tail=200 qr-bridge
docker volume inspect multisender_qr_bridge_data
```

### Recovery
```bash
docker compose restart qr-bridge
```

**Do not delete `qr_bridge_data`** unless you accept that every linked device
must be re-paired by scanning a QR code on the customer's phone.

**Do not start a second bridge against the same volume.** Overlapping sockets
on one session corrupt the Signal session store, and the symptom is permanent
"Waiting for this message" on the recipient's phone — unrecoverable without
re-pairing.

### Verification
```bash
curl -f http://127.0.0.1:4001/health
```
Then confirm a number returns to `CONNECTED` and send one test message.

### Post-recovery check
- Numbers that will not reconnect need re-pairing (customer action).
- Check sender health: repeated failures during the outage may have paused a
  number. Resume it from the devices page once the bridge is healthy.

---

## Scenario 6 — Credential compromise

**Act first, investigate second.** See `docs/INCIDENT_RESPONSE.md` for the full
procedure; this is the recovery half.

Rotate in this order — the order matters, because two of these are destructive:

1. **Database password.** Change in Postgres, update `.env`, restart everything.
2. **Redis password.** Update `.env`, restart. All sessions' cached state and
   the queue are lost; treat as Scenario 2.
3. **`JWT_SECRET`.** Rotating signs out every user immediately. That is the
   point, if tokens are suspected stolen.
4. **`SECRET_KEY`.** Invalidates sessions and **every outstanding
   password-reset link**.
5. **WhatsApp / AI / telephony provider keys.** Rotate at the provider, then in
   `.env`.
6. **`QR_PROVIDER_API_KEY` / `QR_PROVIDER_WEBHOOK_SECRET`.** Update in `.env`;
   both containers read them.

> ### Never rotate `WHATSAPP_TOKEN_ENCRYPTION_KEY` by hand
>
> Stored WhatsApp provider credentials are encrypted with it. Changing it makes
> every stored credential permanently unreadable, and every connected number
> stops working with no way back. If it must be rotated, decrypt with the old
> key and re-encrypt with the new one in the same transaction, and take a
> backup first.

---

## Scenario 7 — WhatsApp provider outage

The provider is unreachable or refusing. This is the outage most likely to
actually happen, and the one where the instinct to "do something" causes the
damage.

### Detection
- `messaging.failure_reasons` on `/health/operations/` is dominated by
  `provider_unavailable`.
- `messaging.failure_rate` climbs while `queued_now` climbs with it.
- `providers.paused` rising: numbers are auto-pausing themselves after runs of
  failures.

### Immediate action — mostly, do not intervene

The application already does the right thing, and the temptation is to defeat
it:

- A failed send is retried with **bounded exponential backoff and full jitter**
  (`core/backoff.py`), so a thousand messages that failed together do not all
  retry in the same second.
- After the retries are spent the message is `FAILED` with
  `failure_reason=provider_unavailable`. It is **not** marked sent.
- A send that timed out *after* transmission is `delivery_unconfirmed` and is
  deliberately **never retried** — it may already be on the recipient's phone.
- A number failing at an unusual rate is auto-paused (`WHATSAPP_AUTO_PAUSE_*`),
  which is what stops a temporary block becoming a permanent one.

**Do not** raise the retry limit, clear the pauses, or re-queue everything at
once. Continuing to push a provider that is refusing is how an outage becomes a
ban.

### Recovery

1. Confirm it is them, not us: check the provider's own status page, and check
   `providers.by_status`. If every number went `DISCONNECTED` at once it is
   more likely the qr-bridge (Scenario 5) than the provider.
2. Wait. Paused numbers resume by themselves after
   `WHATSAPP_AUTO_PAUSE_MINUTES`.
3. When it is over, retry the affected campaigns from the campaign's own retry
   action. That re-queues only messages that genuinely failed, and it goes back
   through the send gate — consent, quota and credits are re-checked, so nobody
   is charged twice and nobody who opted out in the meantime is messaged.

### Verification
- `messaging.failure_rate` back to its normal band.
- `providers.paused` back to 0.
- A test message to your own number goes out.

### Customer communication

Say that sending is delayed, not that messages were lost — they are still
`QUEUED` rows in PostgreSQL. Do not promise a delivery time you do not control.

---

## Scenario 8 — Payment provider outage

### Detection
- Checkout fails; billing errors in the logs.
- Payment webhooks stop arriving.

### The thing that must *not* happen

**No existing customer loses access because a billing API is down.**
Entitlements are computed from `Subscription` rows in PostgreSQL — the local,
authoritative state — and never by asking the gateway. A gateway outage means
*new* checkouts fail; it does not touch anybody already paid up.

### Immediate action

1. Confirm with the provider's status page.
2. Leave `PAYMENTS_ENABLED` alone. Turning it off mid-outage removes the
   checkout button from customers who are mid-purchase and changes nothing
   else.
3. Webhooks are idempotent and deduplicated on `(provider, event_id)`, so a
   backlog delivered late is safe. Do not replay them by hand.

### Recovery

When the provider returns, replayed webhooks reconcile themselves. Check for
rows stuck in `RECEIVED`:

```bash
docker compose exec postgres psql -U "$DB_USER" -d "$DB_NAME" \
  -c "SELECT provider, status, count(*) FROM billing_webhookevent GROUP BY 1,2;"
```

### Customer communication

"Payments are temporarily unavailable; your account is unaffected." That is
true, and it is the whole point of holding subscription state locally.

---

## Scenario 9 — Email provider outage

### Detection

Password-reset emails are not arriving; SMTP errors in the logs.

### The thing that must *not* happen

The password-reset endpoint **must keep answering identically** whether or not
the address exists and whether or not the mail was sent. That is what stops the
endpoint being an account-enumeration oracle, and an outage is exactly when
somebody is tempted to add a helpful "we could not send your email" message.
Do not.

### Immediate action

1. Check SMTP credentials and the provider's status.
2. Affected users cannot reset passwords. If one needs urgent access, an
   operator changes it directly (`manage.py changepassword`) — audited, and
   never by sending a token over any other channel.

### Recovery

Restore SMTP, then confirm with a real reset to an address you control.
`manage.py check_production` fails on incomplete SMTP configuration.

---

## Scenario 10 — Queue backlog, or a queue nobody is consuming

New in Phase 5, and the failure is **silent** — which is why it has a scenario.

### Detection
- `workers.queues_unconsumed` on `/health/operations/` is not empty.
- `manage.py check_production` FAILs on "Queue consumption".
- Without the dashboard: a queue's depth grows and nothing happens. Campaigns
  stay `QUEUED`, imports never finish. It looks exactly like an idle system.

### Cause

Tasks are routed by `CELERY_TASK_ROUTES` to five named queues. A worker started
without `-Q` naming all of them consumes only the default, and everything
routed elsewhere waits in Redis for ever. Celery does not report this.

### Recovery

```bash
docker compose exec celery-worker celery -A config inspect active_queues
docker compose up -d celery-worker
```

Nothing is lost — the tasks are still in Redis, and are consumed as soon as a
worker subscribes to that queue.

### Verification

`/health/operations/` shows `workers.queues_unconsumed` empty, and queue depths
falling.

### Prevention

`tests/test_queue_routing.py` asserts that the worker command in
docker-compose.yml covers every queue in `CELERY_QUEUE_NAMES`, so adding a
queue and forgetting the worker fails the test suite rather than production.

---

## Scenario 11 — Accidental or broken deployment

### Detection
- Errors spike immediately after a deploy.
- `/health/ready/` fails, or the app loads but a core screen is broken.

### Immediate action
Note the currently deployed build:
```bash
curl -s http://localhost/health/ | python -m json.tool   # version + build
```

### Recovery — rollback
```bash
git log --oneline -10
git checkout <previous-good-sha>
docker compose build backend nginx        # all three backend services share the image
docker compose up -d
```

**The migration question, before you roll back:** rolling code back does not
roll the database back.

| Situation | Action |
| --- | --- |
| No migrations in the bad release | Roll back freely |
| Migrations that only **add** columns/tables (all of Phase 3) | Roll back freely — old code ignores new columns |
| Migrations that **remove or rename** | Do **not** roll back code alone. Reverse the migration first (`migrate <app> <previous>`) and only if it is reversible |
| An irreversible migration | Restore from the backup taken before the deploy |

```bash
docker compose exec backend python manage.py showmigrations
docker compose exec backend python manage.py migrate <app> <number>   # if reversing
```

Frontend rollback is part of the nginx image rebuild above; there is no
separate step.

### Verification
Run the smoke test in `docs/SMOKE_TEST.md`.

### Post-recovery check
- Take a backup before every deploy. Then this scenario is 15 minutes, not a
  bad afternoon.
- `manage.py check_production` in the pipeline catches the configuration half
  of these before they ship.

---

## Restore test — run this monthly

A backup nobody has restored is a file, not a backup.

```bash
./scripts/backup_postgres.sh
ls -lt backups/ | head -3
./scripts/restore_postgres.sh backups/<newest>
```

The restore script creates a throwaway database, restores into it, runs
verification queries, prints the row counts and drops it. It never touches
production unless given `--target` **and**
`--i-understand-this-overwrites`.

The script records its own result, so `manage.py check_production` and
`/health/backups/` report it without anything further being typed in.

> **Fixed in Phase 6:** this script used to exit **1 even when the restore
> passed.** Its `EXIT` trap was a bare `[ -n "$TEMP" ] && { ... }`, and when
> `TEMP` was empty — which is every restore of an unencrypted archive, the
> common case — the failed test became the script's exit status. A drill that
> printed `RESTORE TEST PASSED` returned non-zero, so any cron or CI job gating
> on the exit code saw every successful run as a failure. If you have a job
> that has been red since Phase 3, this is why.

| Date | Archive | Restored rows (tenants / contacts / messages) | Result | By |
| --- | --- | --- | --- | --- |
| 2026-08-30 | `multisender-20260830T080043Z.dump` | 1 tenant / 24 contacts / 148 messages (77 tables, 104 migrations, 26 consent events) | PASS | Phase 3 |
| 2026-08-31 | `multisender-20260831T051839Z.dump` | 2 tenants / 24 contacts / 163 messages (88 tables, 121 migrations, 26 consent events) | PASS | Phase 6 |
| _(record each test)_ | | | | |

---

## Off-host restore drill — run this monthly

**A different and stronger claim than the restore test above, and the one that
actually matters on the day the host is gone.**

A local restore test proves the archive *on this disk* is a good archive. It
says nothing about the copy in the bucket: whether it is complete, whether it
can be read back with the credentials a rebuilt host would have, whether the
passphrase in use today still decrypts it, whether a lifecycle rule has moved
it to a storage class that cannot be streamed. Every one of those failures
passes an upload check **and** a local restore, and is discovered at the worst
possible moment.

```bash
./scripts/verify_offhost_backup.sh
```

What it does, in the order a disaster would:

1. Takes a backup and copies it off-host.
2. **Moves the local archive aside**, so nothing downstream can quietly use
   it. Without this the drill would happily pass while restoring the local
   file — which is exactly the reassuring lie it exists to prevent.
3. Fetches the object back **from the remote**, by name.
4. Compares it against what was sent: same size, same SHA-256. Size alone
   would miss a corrupted byte in the middle, which is the failure that
   survives an upload check and then kills a restore.
5. Restores **the fetched copy** into a scratch database and verifies it.
6. Records the outcome as an `OFFSITE_RESTORE_TEST`, which
   `check_production` reports under "Off-host restore".

The original archive is put back whatever happens, and the fetched copy is
shredded on exit — it holds every phone number and message in the deployment
and does not get to linger in a temp directory.

`s3://` and `rclone:` destinations are read back automatically. For anything
driven by `BACKUP_UPLOAD_COMMAND` you must also set `BACKUP_DOWNLOAD_COMMAND`;
the drill refuses to run without it rather than reporting a pass it did not
earn. A destination this deployment can write to but cannot read back is a
destination nobody can restore from.

| Date | Destination | Archive | Bytes | SHA-256 match | Restored | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-08-31 | `custom` (separate filesystem path, single host) | `multisender-20260831T051839Z.dump` | 537,823 | yes | 2 tenants / 24 contacts / 163 messages | PASS |
| _(record each drill)_ | | | | | | |

> **What that row does and does not prove.** It proves the **mechanism** end to
> end: upload, quarantine, fetch, byte-for-byte verification, restore, and the
> recording of the result. It does **not** prove this deployment has off-site
> backups, because the destination used was a separate filesystem path on the
> same machine — there is no S3 bucket or rclone remote configured here. Before
> production, set `BACKUP_REMOTE_TARGET` to a genuinely off-host destination
> and re-run this drill. `check_production` fails the release until a real one
> is configured.
