# Operations

Running this deployment day to day: what to watch, what the numbers mean, when
to act, and what capacity has actually been tested.

Recovery from an outage is a different document — see
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md). This one is about the hours when
nothing is on fire, and about noticing when something is about to be.

---

## The operations dashboard

```
GET /health/operations/      Authorization: Bearer <superadmin token>
```

Superadmin only, and it answers **404** to everybody else — whether it exists
is not a customer's business.

One request, every panel. It reports volumes and states only: never message
content, never a customer's data, and never a credential, hostname or broker
URL. A panel that cannot be computed renders as `{"error": "unavailable"}`
beside the ones that can, because this screen is read *during* an outage and
one that returned a 500 because Redis was down would be useless exactly when it
is needed.

| Panel | What it answers |
| --- | --- |
| `queues` | Depth of each Celery queue, read straight from Redis |
| `workers` | How many workers answer, and **which queues they consume** |
| `dead_letters` | Background jobs that gave up, and which task it keeps happening to |
| `scheduler` | Pending, processing and **overdue** scheduled jobs |
| `messaging` | Queued now, sent/failed/skipped in the last hour, and failures **by reason** |
| `providers` | WhatsApp numbers by state, and how many are auto-paused |
| `tenants` | Workspaces, suspensions, subscriptions by status |
| `campaigns` | Campaigns queued, running or scheduled right now |
| `backups` | Last successful backup, its age, whether it went off-host, last restore test |

### The one field to look at first

`workers.queues_unconsumed`. If it is not `[]`, **tasks are being lost right
now** — routed to a queue no worker reads, sitting in Redis for ever. It looks
exactly like an idle system, which is why it is called out rather than left to
be inferred from a queue depth. `manage.py check_production` fails on it too.

---

## Queues

Five, and only five. Every queue is something an operator has to remember to
consume, and a forgotten one is silent, so they are split by workload shape
rather than by app.

| Queue | Carries | Why it is separate |
| --- | --- | --- |
| `messages` | One provider call per task, thousands of them | Latency matters; must never queue behind anything slow |
| `campaigns` | Campaign dispatch — one task enqueues thousands of the above | Bursty |
| `bulk` | Contact imports | Minutes long, customer-facing |
| `maintenance` | Retention sweeps, stale-record sweeps | Nobody is waiting |
| `celery` | Everything else — automation, AI replies, CRM, voice, **and the scheduler tick** | The default |

The scheduler tick is deliberately **not** on `maintenance`. It is what turns a
scheduled campaign into work at all, and putting it behind an hour-long
retention delete is how a customer's Friday campaign goes out on Saturday.

### Separating them for real

The shipped worker consumes all five, so a default install behaves exactly as
it did before queues existed. To stop a large import delaying sends, run a
second worker and take that queue off the first:

```yaml
  celery-messages:
    <<: *backend
    command: ["celery", "-A", "config", "worker", "-Q", "messages", "-c", "8", "--loglevel=info"]
```

Then narrow the main worker's `-Q` to the remaining four. **Check
`/health/operations/` afterwards**: `queues_unconsumed` must still be `[]`.

---

## Alerts

Only these, and each one is actionable. An alert nobody can act on is training
people to ignore the ones they can.

| Severity | Condition | Why | First action |
| --- | --- | --- | --- |
| **CRITICAL** | `workers.queues_unconsumed` not empty | Tasks are being lost | Fix the worker's `-Q`; restart it |
| **CRITICAL** | `workers.reachable` false for > 5 min | Nothing is sending | `docker compose restart celery-worker` |
| **CRITICAL** | `scheduler.overdue` > 0 for > 5 min | Scheduled campaigns are not going out | Check `celery-beat`; see DR Scenario 4 |
| **CRITICAL** | Readiness (`/health/ready/`) 503 for > 2 min | The app cannot serve | See DR Scenario 1 or 2 |
| **HIGH** | `backups.age_hours` > 36 | Recovery point is slipping | Run `scripts/backup_postgres.sh`; check its cron |
| **HIGH** | `messaging.failure_rate` > 25% over an hour | Something systemic | Read `messaging.failure_reasons` — it names the cause |
| **HIGH** | `queues.total` rising for > 15 min with workers alive | Consumption is slower than production | Add a dedicated worker for the deepest queue |
| **HIGH** | 5xx rate spike | — | Correlation id from the response; see Support below |
| **MEDIUM** | `dead_letters.open` > 0 | A job gave up | Read `by_task`; act, then resolve |
| **MEDIUM** | `providers.paused` rising | Numbers are being auto-paused | A number is failing enough to be protected |
| **MEDIUM** | `backups.restore_age_days` > 35 | Untested backups | Run the restore drill below |
| **INFO** | Trial expiries, plan changes | — | None |

**Deliberately not alerts:** queue depth during a large campaign (that is the
system working), individual message failures (a wrong number is normal), and
CPU (this workload is IO-bound; CPU is a symptom, never the diagnosis).

---

## Failures, by reason

`Message.failure_reason` is a stable code beside the human sentence in
`error_message`. The sentence is what a customer reads and it changes whenever
the wording improves; the code is what reports group by and what alerts fire
on.

| Code | Means | Who fixes it |
| --- | --- | --- |
| `opted_out` | The contact unsubscribed | Nobody — working as intended |
| `consent_missing` | No marketing consent on file | The customer's list hygiene |
| `quota_exceeded` | Plan allowance exhausted | The customer, by upgrading |
| `subscription_expired` | Subscription not active | Billing |
| `insufficient_credits` | Out of credits | The customer or their reseller |
| `tenant_suspended` | Workspace suspended | An operator suspended it |
| `device_disconnected` | No connected number available | The customer rescans a QR |
| `device_paused` | Auto-paused after a run of failures | Investigate the number |
| `rate_limited` | Hit a sending limit | Pacing; usually self-clearing |
| `held_too_long` | Waited past `MESSAGE_MAX_HOLD_SECONDS` | Usually quiet hours plus a limit |
| `invalid_recipient` | The provider rejected the number | The customer's data |
| `provider_unavailable` | Could not reach the provider | **The provider, or us** |
| `provider_error` | Provider said no, uncharacterised | Read `error_message` |
| `delivery_unconfirmed` | Reached, never answered — **not retried** | May or may not have been delivered |

A spike in `provider_unavailable` or `device_disconnected` is a platform
problem. A spike in `opted_out` or `consent_missing` is a customer's list.
Blank means the row predates the codes; it is never backfilled with a guess.

---

## Dead-lettered jobs

A Celery task that spends its retries used to leave a traceback in a log stream
and nothing else. Now it writes a `DeadLetter` row.

```
GET  /health/dead-letters/                    open ones, newest first
POST /health/dead-letters/  {"ids": [1,2]}    mark them handled
```

**Resolving does not re-run anything, on purpose.** Re-running a failed job
needs to know why it failed — re-running a half-finished import could double a
customer's contacts, and re-running a send could deliver a message twice. Use
the specific guarded path instead (the campaign's own retry action, or a
re-upload), then resolve the row.

Rows carry the correlation id, so a support ticket that starts with a request
id from a browser can be followed to the job that failed three hops later.

---

## Data retention

A daily sweep (`apps.health.tasks.run_retention_sweep`, on `maintenance`)
deletes operational exhaust. See [DATA_RETENTION.md](DATA_RETENTION.md) for the
full policy.

```bash
python manage.py prune_expired_data --dry-run     # counts, deletes nothing
python manage.py prune_expired_data
```

Run `--dry-run` first on a deployment that has been accumulating for a year —
the numbers are usually larger than expected.

**It never touches** customer business data, consent evidence, audit events,
the credit ledger, payments or legal acceptances. There is no rule for them,
which is stronger than a switch defaulting to off.

| Setting | Default | What it removes |
| --- | --- | --- |
| `RETENTION_WEBHOOK_EVENT_DAYS` | 30 | Raw provider webhook bodies |
| `RETENTION_SCHEDULED_JOB_DAYS` | 30 | Completed/cancelled scheduled jobs (never FAILED) |
| `RETENTION_BUTTON_PROMPT_DAYS` | 7 | Expired numbered-list prompts |
| `BILLING_WEBHOOK_PAYLOAD_RETENTION_DAYS` | 90 | Payment webhook *payloads* — the row survives |

`0` disables a rule entirely, matching the "0 means unlimited" convention used
throughout this codebase.

---

## Capacity — what has actually been tested

Measured, not estimated. Anything not in this table is **unknown**, not
"probably fine".

### Tested dataset

| | |
| --- | --- |
| Tenants | 200 |
| Contacts | 57,925 |
| Messages | 1,096,283 |
| Campaigns | 900 |
| Largest single tenant | ~189,000 messages, ~11,000 contacts |

Reproduce with:

```bash
python manage.py seed_load_data --tenants 200 --contacts 50000 --messages 1000000 --yes
python manage.py benchmark_api --runs 20
```

`seed_load_data` refuses to run unless the database name is on a scratch
allowlist, `APP_ENVIRONMENT` is not `production`, and `--yes` is passed.

### Measured API latency at that size

Application time only — no nginx, no TLS, no network. Ratios and query counts
are the useful part; absolute latency over a real socket is higher.

| Endpoint | Queries | p50 | p95 |
| --- | --- | --- | --- |
| `me` | 2 | 1.8 ms | 2.1 ms |
| dashboard (cached) | 2 | 1.8 ms | 2.2 ms |
| dashboard (cold) | 9 | — | 37 ms |
| daily report | 2 | 1.9 ms | 1.9 ms |
| daily report (cold) | 3 | — | 53 ms |
| contacts list | 6 | 13 ms | 15 ms |
| contacts deep page (90) | 6 | 16 ms | 20 ms |
| contacts search | 6 | 20 ms | 24 ms |
| campaign report | 6 | 44 ms | 49 ms |
| messages list | 4 | 13 ms | 14 ms |
| billing usage | 7 | 12 ms | 13 ms |

No endpoint runs more than 9 queries. There is no N+1 anywhere in the
benchmarked set.

### Measured resource footprint (idle, 24-core host)

| Service | Memory | Limit |
| --- | --- | --- |
| backend (7 gunicorn workers × 8 threads) | 410 MiB | 1 GiB |
| celery-worker (concurrency 4) | 270 MiB | 1 GiB |
| celery-beat | 86 MiB | 256 MiB |
| postgres | 32 MiB | 1 GiB |
| redis | 9 MiB | 256 MiB |
| qr-bridge | 52 MiB | 512 MiB |
| nginx | 20 MiB | 128 MiB |
| **Total** | **~880 MiB** | ~4.1 GiB |

Worker counts derive from the memory limits, not from CPU count — see
`backend/gunicorn.conf.py`. Raising `MEM_LIMIT_BACKEND` raises the worker count
automatically; the two cannot drift apart.

### Measured concurrency (Phase 6)

**A different test from the table above, and the distinction matters.**
`benchmark_api` measures *data scale*: one request at a time, through the test
client, against a large dataset. It never contends for a gunicorn worker, a
database connection or a Redis socket, because only one request is ever in
flight. `scripts/loadtest.py` measures *concurrency*: many real HTTP clients
at once, over the network, through nginx and gunicorn, as many workspaces.
Neither substitutes for the other.

Measured on a 24-core development host against 886,310 messages / 46,330
contacts / 60 workspaces, mixed read and write traffic (10 scenarios, 5% of
them campaign creation), 0.5 s mean think time per virtual user:

| Virtual users | Throughput | Errors | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| 40 (1.0 s think) | 9.7 req/s | 0% | 25 ms | 46 ms | 63 ms | 91 ms |
| 200 | **345 req/s** | **0%** | 44 ms | 280 ms | 730 ms | 1,719 ms |
| 400 | 329 req/s | 0% | 60 ms | 3,551 ms | 4,364 ms | 5,289 ms |

**The knee is at roughly 345 req/s on this hardware.** Between 200 and 400
users throughput stops rising — it falls slightly — while p95 grows by a
factor of twelve. That is saturation: past the knee, additional load only
queues.

**The bottleneck is the application tier, not the database.** At saturation:

| | 200 users | 400 users |
| --- | --- | --- |
| backend CPU (mean) | 519% | 557% |
| postgres CPU (mean) | 134% | 133% |
| postgres connections | 49 (5 active) | 49 (6 active) |
| redis ops/sec (peak) | 881 | 997 |
| redis rejected connections | 0 | 0 |
| celery queue depth | 0 | 0 |

Backend CPU rises and pins; PostgreSQL CPU and the connection pool are flat,
with only five or six connections ever active out of forty-nine held. So the
answer to "how do we serve more" is **more application CPU or more backend
replicas**, not database tuning or a larger pool. Anybody who read only the
p99 would have gone and optimised a query.

Reproduce with:

```bash
docker compose exec backend python manage.py seed_load_data     --tenants 60 --contacts 60000 --messages 400000 --yes
python scripts/loadtest.py --users 200 --duration 150 --tenants 50     --think 0.5 --db-name ms_loadtest --json capacity.json
```

**Two runs are needed, and they answer different questions.** At production
throttle rates (`THROTTLE_RATE_USER=300/min`) a 200-user saturation run
returns ~85% `429` — that is the rate limiter working, and it is worth
confirming. To measure what the *application* can do, raise the throttles on
the staging stack first; otherwise the number you get is the limiter's, not
the app's. Both runs are reported above with the throttles raised.

### Known limits and untested scale

* **Not tested:** more than 200 tenants, more than ~1.1M messages, sustained
  send throughput against a real provider, and any multi-host deployment. No
  claim is made about any of them.
* **The concurrency numbers above are for a single 24-core host** with the
  memory limits in `docker-compose.yml`. They are not a claim about any other
  hardware, and they were measured with the payment and WhatsApp providers
  never contacted — no message was sent during any load run.
* **The next bottleneck is reporting.** `daily_message_breakdown` aggregates
  every matching message row for the range. At 189,000 rows that is ~53 ms
  cold and cached for 45 s; it grows linearly with a tenant's message history.
  A tenant with several million messages will feel it. The fix when that
  arrives is a pre-aggregated daily rollup table, not another index — the scan
  is inherent to the question.
* **Sending throughput is bounded by the provider, not by this system**, and by
  the deliberate per-message pacing (`CAMPAIGN_MIN/MAX_DELAY_SECONDS`) and the
  per-number daily and hourly limits. Adding workers does not raise it.
* **Single host.** Web containers are stateless and can scale horizontally, but
  uploaded media lives on a local volume — see the file-storage note in
  DISASTER_RECOVERY.md before running more than one host.

---

## Support: from a customer's error to the cause

Every error carries a correlation id, and it survives the hop into Celery.

1. The customer quotes the reference from the error screen.
2. `grep` that id in the application logs: `request_id=<id>`.
3. The same id is on every log line the resulting background job produced, and
   on any `DeadLetter` row it wrote.
4. `/health/diagnostics/` (authenticated, tenant-scoped) shows that workspace's
   own connectivity, plan, usage and number states.

Logs never contain passwords, tokens, message bodies or full phone numbers —
numbers are masked to `+91******3210` in production.

---

## Routine

| When | What |
| --- | --- |
| Daily (automatic) | Backup, retention sweep |
| Weekly | Skim `/health/operations/`; clear resolved dead letters |
| Monthly | Restore drill (below); review `dead_letters.by_task` for patterns |
| Before every release | `manage.py check_production`; full test suite; backup |

### Restore drill

```bash
scripts/backup_postgres.sh
scripts/restore_postgres.sh backups/<archive>
```

Restores into a throwaway database, verifies row counts, drops it, and records
the run so `check_production` and `/health/backups/` report it. It refuses to
touch the live database without `--target` **and**
`--i-understand-this-overwrites`.

Last verified: see `restore_age_days` on `/health/operations/`.
