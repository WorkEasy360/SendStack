# Security incident response

For a suspected or confirmed security incident: credentials leaked, unauthorised
access, data exposed, or a compromised dependency. For an outage that is not a
security event, use `docs/DISASTER_RECOVERY.md` instead.

**The first ten minutes matter more than the next ten hours.** Contain first,
understand second — but do not destroy the evidence while containing.

---

## 0. Before anything

Open a document and start a timeline. Every entry gets a UTC timestamp:

```
2026-08-30T14:02Z  Reported by <who>: <what they saw>
2026-08-30T14:05Z  Confirmed: <what you verified, and how>
2026-08-30T14:09Z  Contained: rotated JWT_SECRET, all sessions ended
```

You will need this for the post-incident review, and possibly for a regulator.
Reconstructing it afterwards from memory does not work.

---

## 1. Detect

Signals worth treating as an incident until proved otherwise:

- Unexpected `auth.login` audit events — unfamiliar times, unfamiliar addresses.
- A spike in 401s or 403s, or a spike in failed-login lockouts.
- A customer reporting messages they did not send.
- An unexplained drop in a credit balance.
- A secret found in a commit, a log, a screenshot or a support ticket.
- A dependency advisory affecting something in `requirements/`.
- Error monitoring showing exceptions from paths nobody uses.

```bash
# Recent authentication and administrative activity
docker compose exec backend python manage.py shell -c "
from apps.audit.models import AuditEvent
for e in AuditEvent.objects.order_by('-created_at')[:50]:
    print(e.created_at, e.action, e.actor_id, e.tenant_id, e.ip_address)"
```

---

## 2. Contain

Pick the smallest action that stops the bleeding. In rough order of severity:

**A single account is compromised**
```bash
docker compose exec backend python manage.py shell -c "
from django.contrib.auth import get_user_model
from apps.accounts.sessions import revoke_all_refresh_tokens
u = get_user_model().objects.get(username='<username>')
u.is_active = False; u.save(update_fields=['is_active'])
print(revoke_all_refresh_tokens(u), 'sessions revoked')"
```

**A whole workspace is compromised or abusive**
```bash
docker compose exec backend python manage.py shell -c "
from apps.tenants.models import Tenant
t = Tenant.objects.get(slug='<slug>'); t.is_active = False; t.save(update_fields=['is_active'])"
```
This takes effect on the next request: sign-in is refused, existing tokens are
rejected by the authentication layer, and the send gate blocks every queued and
scheduled message for that workspace. **No data is deleted.**

**Tokens may be stolen across the board** — rotate `JWT_SECRET` (section 4).
Every user is signed out immediately. That is the intended effect.

**The application itself is compromised** — stop serving:
```bash
docker compose stop nginx     # keeps the database and containers for evidence
```

---

## 3. Preserve evidence

Do this *before* rotating, restarting or rebuilding. Restarting a container
discards its logs.

```bash
mkdir -p incident/$(date -u +%Y%m%dT%H%M%SZ) && cd $_
docker compose logs --no-color --timestamps > containers.log
docker compose ps > containers.txt
docker compose exec -T backend python manage.py shell -c "
import json
from apps.audit.models import AuditEvent
print(json.dumps(list(AuditEvent.objects.order_by('-created_at')[:5000].values()), default=str))
" > audit.json
cd - && ./scripts/backup_postgres.sh      # a snapshot of the state as found
```

Store these somewhere the compromised system cannot reach or overwrite.

---

## 4. Rotate credentials

Order matters — two of these are destructive, and one must never be done by hand.

| # | Secret | Where | Effect |
| --- | --- | --- | --- |
| 1 | `DB_PASSWORD` | `ALTER ROLE` in Postgres, then `.env` | Restart all backend services |
| 2 | `REDIS_PASSWORD` | `.env` | Queue and cache lost — treat as DR Scenario 2 |
| 3 | `JWT_SECRET` | `.env` | **Every user signed out immediately** |
| 4 | `SECRET_KEY` | `.env` | Sessions and **all outstanding password-reset links** invalidated |
| 5 | WhatsApp / AI / telephony keys | At the provider, then `.env` | Provider calls fail until updated |
| 6 | `QR_PROVIDER_API_KEY`, `QR_PROVIDER_WEBHOOK_SECRET` | `.env` | Backend and bridge must restart together |
| 7 | `BACKUP_PASSPHRASE` | `.env` | **Only affects new backups.** Old archives still need the old passphrase — keep it |

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # generate
# edit .env, then:
docker compose up -d --force-recreate backend celery-worker celery-beat
docker compose exec backend python manage.py check_production
```

> ### `WHATSAPP_TOKEN_ENCRYPTION_KEY` — never rotate by hand
>
> Every stored WhatsApp provider credential is encrypted with it. Changing it
> makes them permanently unreadable and every connected number stops working,
> with no way back. If it genuinely must be rotated: take a backup, then
> decrypt with the old key and re-encrypt with the new one in a single
> transaction. There is no other safe procedure.

---

## 5. Identify scope

Answer these four, in writing:

1. **What was reachable?** Which accounts, which workspaces, which tables.
2. **What was actually accessed?** Audit events, request logs, provider logs.
   "We cannot tell" is an acceptable answer and must be recorded as one.
3. **Whose data?** List the affected tenants by id.
4. **Over what window?** First and last evidence, and note if the start is
   uncertain.

```sql
-- Cross-tenant access attempts, if any reached the audit trail
SELECT tenant_id, action, count(*) FROM audit_auditevent
WHERE created_at > now() - interval '7 days'
GROUP BY tenant_id, action ORDER BY count DESC LIMIT 50;
```

---

## 6. Restore service

Only once containment holds and the cause is understood well enough that
restoring will not simply reopen the hole.

1. Patch the cause. A rotation is not a fix.
2. `docker compose up -d`
3. `manage.py check_production`
4. Run `docker/SMOKE_TEST.md`.
5. Reactivate any accounts or workspaces suspended in step 2 — and record who
   authorised it.

---

## 7. Notify

**This deployment has not decided its notification obligations.** They depend on
the legal entity, where customers are, and where data is hosted — all still
`[TO BE COMPLETED]` in the legal documents.

`[TO BE COMPLETED — the notification deadline the operator commits to (many
regimes use 72 hours for a supervisory authority), who signs off, and the
template used.]`

What is already true regardless:

- Affected **customers** are the controllers for their own contacts' data. If
  their contacts' data was exposed, **they** may have notification obligations
  and cannot meet them unless told. Tell them what was exposed, when, what has
  been done, and what they should do.
- Say what is known and what is not. A confident wrong statement in a breach
  notice is worse than "we are still determining the scope".
- Never include credentials, tokens or extracts of the exposed data in a notice.

---

## 8. Post-incident review

Within a week, while it is still remembered. No blame — the question is what
in the system allowed it, not who typed what.

- Timeline: first exposure → detection → containment → resolution.
- **How was it detected?** If a customer told you, that is the finding.
- What made containment slow?
- What would have prevented it? Add it as work, with an owner.
- What would have detected it sooner? Usually a monitor or an alert.
- Update this document with what was actually missing from it.

---

## Quick reference

```bash
# Suspend a workspace (no data loss)
docker compose exec backend python manage.py shell -c "
from apps.tenants.models import Tenant
t=Tenant.objects.get(slug='SLUG'); t.is_active=False; t.save(update_fields=['is_active'])"

# End every session for one user
docker compose exec backend python manage.py shell -c "
from django.contrib.auth import get_user_model
from apps.accounts.sessions import revoke_all_refresh_tokens
revoke_all_refresh_tokens(get_user_model().objects.get(username='USER'))"

# Configuration audit (never prints values)
docker compose exec backend python manage.py check_production

# Emergency snapshot
./scripts/backup_postgres.sh

# Stop serving, keep everything
docker compose stop nginx
```
