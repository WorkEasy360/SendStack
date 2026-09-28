# Durable scheduled work

Why scheduled campaigns survive losing Redis, what the scheduling precision
actually is, and how a stuck job recovers.

---

## The problem this fixes

Before this, "send this campaign at 9am on Friday" was:

```
dispatch_campaign.apply_async(eta=campaign.scheduled_at)
```

and nothing else. That instruction existed in exactly one place — a Redis list.
Redis in this deployment is a **cache**: `IGNORE_EXCEPTIONS` is on, it has no
persistence guarantee, and losing it is an ordinary operational event. A
customer's Friday campaign disappearing because a cache was restarted on
Wednesday is not a failure mode a system that charges money may have.

---

## The design

```
ScheduledJob (PostgreSQL — the truth)
      │
      ├── the poller finds it due          ← every SCHEDULER_POLL_INTERVAL_SECONDS
      ├── Celery carries it to a worker    ← replaceable, re-runnable
      └── the handler runs                 ← re-reads everything
```

Redis goes back to what it is good at: carrying a job that is due *right now*
to whichever worker is free. **Losing Redis entirely costs at most one polling
interval of latency**, because the row in PostgreSQL is untouched and the next
tick finds it.

The model is deliberately *not* a copy of `Campaign`. It stores when something
should happen and whether it has; `kind` + `object_id` names a row in the table
that already holds everything else. A scheduled job that duplicated the
audience or the message body would be a second version of the truth, and the
two would disagree the first time somebody edited the campaign.

---

## Scheduling precision

**A job fires between its scheduled time and one polling interval later.
Late-biased, never early.**

`SCHEDULER_POLL_INTERVAL_SECONDS` defaults to **60**, so the honest statement
is "within a minute". Say that to a customer, not "at 9:00:00".

Tightening it is a one-variable change. The cost is one indexed query per tick:
a range scan over the composite `(status, scheduled_for)` index that returns
nothing almost every time. A second would still be affordable and pointless for
a product whose unit of scheduling is "9am".

The poller **never** scans the campaign table.

---

## Concurrency

Claiming is a database operation, not a decision:

```sql
SELECT ... WHERE status = 'PENDING' AND scheduled_for <= now()
ORDER BY scheduled_for, id LIMIT n
FOR UPDATE SKIP LOCKED
```

Two pollers firing the same second each take what nobody else holds, and
neither waits for the other. Without `SKIP LOCKED` the second would block on
the first's row locks and then wake to find everything claimed — a wasted
connection to learn nothing.

On top of that, a **partial unique constraint** allows only one live
(`PENDING` or `PROCESSING`) job per `(kind, object_id)`. Two pending jobs for
one campaign would be two dispatches of that campaign, and no amount of care in
the service layer survives two requests arriving at once.

---

## Failure recovery

`locked_at` is a **lease**, not a lock. A worker that dies holding a job would
otherwise leave it `PROCESSING` for ever — and the way that happens is not
exotic, it is an ordinary redeploy.

```
PROCESSING, locked_at older than SCHEDULER_LEASE_SECONDS (default 900)
      ↓
attempts < max_attempts  →  back to PENDING, picked up next tick
attempts >= max_attempts →  FAILED, with the reason on the row
```

**The attempt is spent when the job is claimed, not when it fails.** A worker
killed mid-job reports nothing, so a retry budget that counted only reported
failures would never run out and the job would retry for ever.

---

## Running twice

The scheduler guarantees **at least once**. Nothing can guarantee exactly once
across a process boundary and a network, and this document will not claim it.

What makes at-least-once safe is one layer down, and it predates this work:

```python
# apps/messaging/services.attempt_send
claimed = Message.objects.filter(pk=..., status=QUEUED).update(status=SENDING)
if not claimed:
    return None   # another worker already has it
```

A conditional `UPDATE` is a compare-and-swap. Re-running a whole campaign
dispatch re-enqueues every message still marked `QUEUED` — and that is fine,
because only one execution can ever claim each message. Nobody is charged
twice (credits are consumed per message, against that same row) and nobody is
messaged twice.

**Not claimed:** exactly-once *delivery*. If the provider accepts a message and
the acknowledgement is lost, the recipient has the message and this system does
not know it. That is the provider's boundary, not one this code can close.

---

## Authorization at execution time

A campaign scheduled on Monday carries a decision made on Monday. By Friday the
workspace may be suspended, the subscription lapsed, or the campaign cancelled.

`handlers.dispatch_campaign_job` re-reads, at the moment of use:

* the campaign's own status — this is what makes cancelling un-raceable
* whether the tenant is active
* whether the subscription is live (`access_state() != EXPIRED`)

The per-recipient rules — consent, opt-out, quiet hours, the number's send
limits, the monthly allowance claimed one message at a time — are **not**
re-checked here and must not be. They live in `apps.messaging.gating`, which
runs immediately before each individual send, which is strictly better than a
campaign-level snapshot taken minutes earlier.

---

## Monitoring

`apps.health.checks.check_scheduler` **FAILs** when any job is more than five
minutes past due and unclaimed. That is the one number that says whether the
beat container is alive; without it, a stopped scheduler is invisible until a
customer asks why their campaign never went out.

It is a FAIL rather than a warning because a scheduled campaign that did not
send is a customer-visible failure of the product's core promise.

---

## Verifying it by hand

```bash
# 1. Schedule a campaign for a few minutes out, and launch it.
# 2. Confirm the durable row exists:
docker compose exec backend python manage.py shell -c \
  "from apps.scheduling.models import ScheduledJob; print(list(ScheduledJob.objects.values()))"

# 3. Destroy Redis completely -- not a restart, a data loss:
docker compose stop redis
docker compose rm -f redis
docker volume rm multisender_redis_data 2>/dev/null || true
docker compose up -d redis

# 4. Wait one polling interval. The campaign sends.
docker compose logs -f celery-worker | grep -i "scheduled campaign"
```

The row survives because it was never in Redis.
