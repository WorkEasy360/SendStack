# Production smoke test

Run after every deployment, and after any recovery. Sections 0-11 take about
twenty minutes; 12-15 are only for a deployment that has opened self-service
signup or payments, and add about fifteen.

**Use a test workspace with a test WhatsApp number and your own phone as the
recipient.** Never a real customer's account, and never a real contact list.

Record the result: date, build, who ran it, and anything that failed.

---

## 0. Before touching the UI

```bash
curl -f https://<host>/health/live/                    # 200, reports version + build
curl -f https://<host>/health/ready/                   # 200, database + redis true
docker compose exec backend python manage.py check_production
docker compose exec backend celery -A config inspect ping
docker compose ps                                      # every service healthy
```

- [ ] Liveness 200
- [ ] Readiness 200 with both dependencies true
- [ ] `check_production` reports no FAIL
- [ ] The build reported by `/health/live/` is the build you deployed
- [ ] At least one Celery worker answers

---

## 1. Authentication

- [ ] Sign in with a correct password → dashboard loads
- [ ] Sign in with a wrong password → refused, no stack trace, no "user not found"
- [ ] Sign out → returns to sign-in; going back in the browser does not restore the session
- [ ] **Forgot password** → `/forgot-password`, submit your address
- [ ] The email arrives, is branded, states the one-hour expiry, and carries a warning
- [ ] **The link in the plain-text part works when pasted** (this is a real regression class — check `&token=`, not `&amp;token=`)
- [ ] The link opens `/reset-password` and sets a new password
- [ ] The old password no longer signs in; the new one does
- [ ] **Opening the same link again is refused** ("invalid or has expired")
- [ ] A "your password was changed" email arrives, containing no password
- [ ] Any other device that was signed in is signed out
- [ ] Submitting a reset for an address with no account gives the *same* response
- [ ] Settings → Change password works, and requires the current password

---

## 2. Legal and support

- [ ] `/terms` opens **while signed out**
- [ ] `/privacy` opens while signed out
- [ ] `/dpa` opens while signed out
- [ ] Each shows a version number
- [ ] If business details are unset, each shows the "Draft — not legally reviewed" banner
- [ ] Settings shows a support contact, or an honest "not configured"
- [ ] A dashboard URL while signed out redirects to sign-in and shows no data

---

## 3. Billing and entitlements

- [ ] The plan and usage load on the billing screen
- [ ] Usage numbers look right for the workspace
- [ ] Exceed a quota (or use a tiny test plan) → refused with a sentence naming the plan
- [ ] A feature the plan excludes is refused **in the UI**
- [ ] The same feature is refused **by direct API call** — not just hidden in the UI
- [ ] The refusal carries a `code` and a `request_id`, not a stack trace

---

## 4. WhatsApp

- [ ] Connect a device (QR) or confirm a Cloud API number is `CONNECTED`
- [ ] Send a test message to your own phone → arrives
- [ ] The message shows `SENT`, then `DELIVERED`
- [ ] Reply from your phone → appears in the inbox within seconds
- [ ] The inbox shows the correct contact and thread

---

## 5. Contacts and consent

- [ ] Create a contact by hand
- [ ] Its marketing consent is **"Not recorded"** — not granted
- [ ] Import a small CSV without declaring a consent source
- [ ] The imported contacts are also **"Not recorded"**
- [ ] Import with a declared source → contacts show that source and a timestamp
- [ ] Contact detail shows consent history
- [ ] Export contacts → the CSV carries the consent columns

---

## 6. Campaigns

- [ ] Create a campaign to a small test audience
- [ ] Launch it → messages send with the expected gaps
- [ ] Results show sent / delivered / failed
- [ ] Schedule a campaign for two minutes ahead → it fires
- [ ] Pause a running campaign → sending stops
- [ ] Resume it → sending continues

---

## 7. Safety gates — the important section

- [ ] Reply **STOP** from your phone → the contact shows unsubscribed
- [ ] A confirmation is sent (if the policy has it on)
- [ ] Send that contact a marketing message → **blocked**, marked SKIPPED with a reason
- [ ] Try to re-subscribe them by editing the contact through the API
      (`PATCH {"subscribed": true}`) → **the opt-out survives**
- [ ] Re-import that number in a CSV with a declared consent source →
      **still unsubscribed**
- [ ] Turn on "require marketing consent" → contacts with no recorded consent
      are skipped, not messaged
- [ ] A service reply to somebody who just messaged in still works with that
      setting on
- [ ] Turn on quiet hours covering now → a marketing message is held, not sent
- [ ] Suspend the test workspace → sign-in refused, existing session refused on
      next request, queued sends blocked
- [ ] Reactivate it → access returns and nothing was deleted

---

## 8. Data

- [ ] Settings → Download export produces a JSON file
- [ ] It contains your contacts, consent records and campaigns
- [ ] It contains **no** password hash, token, API key or WhatsApp credential
- [ ] It contains **no** other workspace's data
- [ ] `manage.py delete_tenant <slug> --dry-run` on a scrap workspace prints
      what it would delete and what it would keep, and changes nothing

---

## 9. Recovery

Do this on staging, or during a maintenance window.

```bash
docker compose restart redis
# wait ~15s
curl -f https://<host>/health/ready/
docker compose exec backend celery -A config inspect ping
```
- [ ] Readiness returns
- [ ] Workers reconnect
- [ ] A new campaign sends normally afterwards
- [ ] Any campaign that was mid-flight is checked against
      `docs/DISASTER_RECOVERY.md` Scenario 2 (scheduled ETAs do not survive)

```bash
docker compose restart celery-worker
docker compose restart backend
```
- [ ] Both come back healthy
- [ ] Queued messages resume sending

---

## 10. Errors and observability

- [ ] Trigger a 404 through the API → a clean JSON error, no stack trace
- [ ] A 500 (if one can be provoked safely) returns a generic message **and a
      `request_id`**, never an exception string
- [ ] That `request_id` finds the request in the logs
- [ ] A Celery task's log line carries the same id as the request that queued it
- [ ] Phone numbers in production logs are masked (`+91******3210`)
- [ ] If error monitoring is on, the event arrived — and carries no token,
      message body or full phone number

---

## 11. Backup

```bash
./scripts/backup_postgres.sh
./scripts/restore_postgres.sh backups/<newest>

# Both runs now report themselves in. Operators only:
curl -H "Authorization: Bearer $TOKEN" https://<host>/health/backups/
```
- [ ] Backup completes and reports a size
- [ ] It is encrypted (or the warning that it is not is understood and accepted)
- [ ] It says **"Off-host copy verified"** — not just "Done"
- [ ] The restore test passes and prints plausible row counts
- [ ] `/health/backups/` shows both runs, and `assessment` is all PASS
- [ ] Recorded in the table at the end of `docs/DISASTER_RECOVERY.md`

### 11b. The one that proves it

Break the destination on purpose once, and confirm the failure is loud:

```bash
BACKUP_REMOTE_TARGET=s3://a-bucket-that-does-not-exist ./scripts/backup_postgres.sh
echo "exit: $?"
```
- [ ] Exits non-zero
- [ ] `BACKUP_ALERT_COMMAND` fired
- [ ] `/health/backups/` shows a FAILED run
- [ ] It did **not** report success and quietly keep a local-only copy

---

## 12. Self-service signup (only where `SELF_SERVICE_SIGNUP_ENABLED=true`)

Use a real mailbox you control, and a workspace name you will recognise.

1. Open `/pricing` **signed out**. Prices, limits and features render.
2. Press a plan's button → `/signup?plan=<name>`.
3. Create an account. Do **not** tick the marketing box.

- [ ] `/pricing` loads with no session and shows the plans from the database
- [ ] The trial length on `/signup` matches `BILLING_TRIAL_DAYS`
- [ ] The Terms box is **unticked** on arrival, and the form will not submit without it
- [ ] The marketing box is separate and **unticked**
- [ ] Signing up lands on `/onboarding`, already signed in
- [ ] A second signup on the same address is refused with a message that does
      **not** confirm the address exists
- [ ] In the admin: the new tenant is `CUSTOMER`, its parent is the platform
      (or the configured reseller), and the user's role is `USER`

## 13. Onboarding and activation

Work through `/onboarding` as the new customer.

- [ ] Connect WhatsApp — the step ticks only after the backend says CONNECTED
- [ ] Close the browser mid-way, sign back in: progress is **still there**
- [ ] Import a CSV; the consent question is asked explicitly
- [ ] Send one test message to your own phone
- [ ] The checklist ticks "Send a test message" without anything being clicked
- [ ] An existing, long-standing workspace is **not** shown the guided flow

## 14. Scheduled campaign survives Redis loss

**The Phase 3 HIGH risk. Do not skip this one.**

```bash
# Schedule a small campaign a few minutes out and launch it, then:
docker compose exec backend python manage.py shell -c \
  "from apps.scheduling.models import ScheduledJob; print(list(ScheduledJob.objects.values('id','status','scheduled_for')))"

# Destroy Redis -- a restart is not enough, the data has to go:
docker compose stop redis && docker compose rm -f redis
docker volume rm multisender_redis_data 2>/dev/null || true
docker compose up -d redis
```

- [ ] A `PENDING` ScheduledJob row exists after launching
- [ ] Redis comes back empty
- [ ] The campaign **still sends**, within about a polling interval of its time
- [ ] Each recipient received it exactly **once**
- [ ] Cancelling a scheduled campaign stops it (`ScheduledJob` → `CANCELLED`)

## 15. Billing (only where `PAYMENTS_ENABLED=true`)

Provider **test** keys. Never a live key, never a real card.

- [ ] `/billing` shows the current plan, period, usage meters and the plans
- [ ] Pay with a test card → plan activates
- [ ] Reload `/billing`: still active (it was the server, not the browser)
- [ ] Redeliver the same webhook from the provider dashboard → **nothing changes**
- [ ] Cancel: the message names the exact date access ends
- [ ] With `PAYMENTS_ENABLED=false`, no checkout button is offered anywhere

---

## Result

```
Date:
Build:
Run by:
Failed steps:
Follow-up:
```

**Blocking failures:**

* **Section 7** — the gates that stop the product messaging somebody who asked
  not to be messaged.
* **Section 14** — a scheduled campaign that does not survive Redis loss is the
  Phase 3 risk still open.
* **Section 11b** — a backup failure that is not loud is a backup system nobody
  can rely on.

Everything else is a bug to file. Those three are a release to stop.
