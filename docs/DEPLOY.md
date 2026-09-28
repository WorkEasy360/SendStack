# Deploying to an Oracle Cloud Always Free VM

Getting SendStack from nothing to a working HTTPS host. Once it is running,
[RELEASE_RUNBOOK.md](RELEASE_RUNBOOK.md) covers putting new versions out and
`./deploy.sh` automates it.

---

## 1. Pick the instance

**Shape: `VM.Standard.A1.Flex`, 2 OCPU, 12 GB RAM, Ubuntu 24.04 (aarch64).**

That is the whole Always Free Ampere allowance in one instance (verified
against Oracle's documentation as of 29 June 2026 — the allowance has been
reduced over time, so confirm the console's own "Always Free-eligible" label
rather than trusting any figure written down here).

**It fits comfortably.** Measured against this stack's own numbers
(`OPERATIONS.md`): idle total ~880 MiB, and the sum of every container's
memory *cap* is ~4.2 GiB. On 12 GB that leaves ~7.8 GB spare even if every
service simultaneously hit its ceiling. No limits need raising or lowering for
this shape, and `gunicorn.conf.py` derives its worker count from
`MEM_LIMIT_BACKEND` automatically — on 2 OCPU it settles at 5 workers × 8
threads (40 concurrent requests), which is well past what this box will see.

The other free shape, `VM.Standard.E2.1.Micro`, is 1 GB of RAM. It cannot run
this — the Vite build alone will OOM. Do not start there.

**Boot volume:** 50 GB is plenty (the free allowance is 200 GB total across
all volumes). Postgres, uploaded media and the backup archives all live on it.

**On "Out of host capacity":** A1 capacity in free tier is genuinely scarce and
the error is not about your account. Try a different availability domain, or a
different home region when you create the tenancy. Retrying on a timer is the
normal way people get one.

### ARM: what I checked, and what to watch

Ampere is **aarch64**, and every native dependency in this repo has a prebuilt
arm64 artifact. I verified rather than assumed:

| Dependency | Status on arm64 |
|---|---|
| `psycopg2-binary` 2.9.12 | ✅ `manylinux_2_28_aarch64` wheel published |
| `cryptography` 50.0.0 | ✅ `manylinux2014/2_28/2_34_aarch64` wheels |
| `argon2-cffi-bindings` | ✅ `manylinux_2_17_aarch64` wheel |
| `sharp` / libvips (Baileys) | ✅ `@img/sharp-linux-arm64` + libvips arm64 |
| `lightningcss`, `@tailwindcss/oxide`, `oxlint`, `rollup` | ✅ arm64 gnu **and** musl builds |
| `pypdf`, `python-docx`, `openpyxl`, `segno` | ✅ pure Python |

**Nothing here is x86-only.** Two things to keep in mind anyway:

1. **`backend/Dockerfile` installs no compiler** — only `libpq5` and `curl`. That
   is deliberate and keeps the image small, but it means a Python dependency
   without an aarch64 wheel does not fall back to building from source; the
   image build fails outright. If you ever add one and hit that, the fix is to
   add `build-essential` and `libpq-dev` to a build stage rather than to the
   runtime image.
2. **The two Node images need different arm64 variants.** `nginx/Dockerfile`
   builds the SPA on `node:20-alpine` (musl → `*-linux-arm64-musl`) and
   `qr-bridge` runs on `node:20-slim` (glibc → `*-linux-arm64-gnu`). Both are
   in the lockfiles. Keep using `npm ci` so the lockfile is what is installed.

> **Build on the VM, not on your laptop.** Your development machine is Windows
> on x86. Images built there will not run on Ampere. `deploy.sh` builds on the
> host it deploys to, which is the whole reason it does. If you ever need to
> build elsewhere, it must be `docker buildx --platform linux/arm64`.

---

## 2. Open the ports — **both** layers

This is the single most common way an Oracle VM appears broken. There are two
independent firewalls and opening one does nothing on its own.

### Layer 1 — the VCN security list (Oracle's console)

Networking → Virtual Cloud Networks → your VCN → Security Lists → default →
**Add Ingress Rules**:

| Source CIDR | Protocol | Dest. port | Why |
|---|---|---|---|
| `0.0.0.0/0` | TCP | **80** | ACME http-01 validation and renewal; redirects to 443 |
| `0.0.0.0/0` | TCP | **443** | The application |
| *your IP*`/32` | TCP | **22** | SSH. Narrow this to your own address |

Nothing else. Postgres (5432), Redis (6379) and qr-bridge (4001) are bound to
`127.0.0.1` in `docker-compose.yml` and must never appear in this table.

### Layer 2 — the instance's own iptables

**Oracle's Ubuntu images ship with a REJECT rule that blocks everything except
SSH, regardless of what the security list says.** Traffic reaches the VM and is
dropped by the host. Insert the rules above that REJECT:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save     # survives reboot; without this you lose it
```

Check it took: `sudo iptables -L INPUT --line-numbers | head` — the ACCEPTs
must appear **above** the `REJECT all` line.

---

## 3. Host setup

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER && newgrp docker
```

### Swap

On the 2 OCPU / 12 GB A1, swap is still not *required* — the Vite build peaks
well under 12 GB, and the whole stack's caps come to ~4.2 GiB. Add it anyway:
at 12 GB the margin is real but no longer generous, and the build runs on the
same box as the live containers, so a spike during `npm ci` competing with a
large CSV import is the case worth insuring against. 2 GB is enough, and costs
2 GB of a 50 GB volume.

Expect the SPA build to take a few minutes on 2 cores. That is the build being
slow, not stuck.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab   # persist across reboot
sudo sysctl -w vm.swappiness=10                              # prefer RAM; swap is insurance
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

Verify with `free -h`. If you ever do fall back to a 1 GB instance, swap stops
being insurance and becomes mandatory — use 4 GB there, and expect the build
to be slow rather than fast.

---

## 4. Point the domain at the VM

Copy the instance's **public IPv4** from the console, then at your DNS provider:

| Type | Name | Value |
|---|---|---|
| `A` | `@` | `<public IP>` |
| `A` | `www` | `<public IP>` |

Reserve the IP (Networking → IP Management → make the ephemeral IP **reserved**)
or a stop/start will hand you a different address and the certificate will stop
matching.

Wait for propagation before going near certbot — an issuance attempt against
DNS that has not caught up is a failed validation, and five of those in an hour
per hostname locks you out of retrying:

```bash
dig +short your-domain.com     # must return the VM's IP, from a machine that is not the VM
```

---

## 5. First deploy

```bash
git clone <your-repo-url> sendstack && cd sendstack
cp .env.example .env
```

Fill in `.env`. At minimum:

```bash
# Generate each of these separately -- never reuse one value for two settings.
python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # DB_PASSWORD, REDIS_PASSWORD, JWT_SECRET
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # WHATSAPP_TOKEN_ENCRYPTION_KEY
```

> `WHATSAPP_TOKEN_ENCRYPTION_KEY` encrypts stored provider credentials. Rotating
> it by hand makes every stored token undecryptable. Set it once and back it up
> somewhere that is not this VM.

Then the deployment-specific values:

```ini
DOMAIN=your-domain.com
CERTBOT_EMAIL=you@your-domain.com
ALLOWED_HOSTS=your-domain.com,www.your-domain.com
FRONTEND_URL=https://your-domain.com
MEDIA_PUBLIC_BASE_URL=https://your-domain.com

# TEMPORARY. No certificate exists yet, so redirecting to https would make the
# site unreachable -- and unreachable on :80 means certbot can never validate.
# You will flip this to true in step 6.
SECURE_SSL_REDIRECT=false
```

Bring it up on plain HTTP:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
curl -sf http://localhost/health/ready/ && echo OK
```

> **Use `docker compose` directly here, not `./deploy.sh`.** With
> `SECURE_SSL_REDIRECT=false`, `manage.py check_production` reports a FAIL
> (`HTTPS enforced`), and `deploy.sh` stops on any failure — by design, so that
> nobody ships a production stack serving plain HTTP. That gate is *correct* and
> you should not work around it; you simply have not reached the point where it
> can pass. `./deploy.sh` becomes the right tool from step 7, once step 6 has
> turned the redirect on.

`http://your-domain.com` should now serve the app. If it does not, it is
almost always step 2 layer 2.

---

## 6. Issue the certificate

Issuance is manual and one-off, on purpose: it fails for reasons only a person
can fix, and a container retrying those in a loop burns the rate limit.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
  --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot \
  -d your-domain.com -d www.your-domain.com \
  --email you@your-domain.com --agree-tos --no-eff-email
```

> Add `--dry-run` first. It exercises the whole path against Let's Encrypt's
> staging environment and does not count against the rate limit. Only drop the
> flag once the dry run succeeds.

Then switch nginx over and turn the redirect on:

```bash
# nginx installs the TLS layout at start, once the certificate exists on disk.
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart nginx

sed -i 's/^SECURE_SSL_REDIRECT=false/SECURE_SSL_REDIRECT=true/' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d backend celery-worker celery-beat
```

Verify:

```bash
curl -sI http://your-domain.com/  | head -1   # 301
curl -sI https://your-domain.com/ | head -1   # 200
curl -sI https://your-domain.com/ | grep -i strict-transport   # HSTS present
```

**Renewal is automatic.** The `certbot` container attempts a renewal every 12
hours (a no-op until 30 days before expiry) and nginx reloads every 12 hours to
pick up a renewed certificate. Neither needs a cron job on the host.

---

## 7. Subsequent deploys

```bash
./deploy.sh
```

It backs up the database, builds all three backend images together, runs
`check_production` and the migration check against the new image, migrates,
restarts, and verifies health and Celery queues — stopping at the first failure
and printing the rollback commands. See `./deploy.sh --help`.

---

## 8. Keeping the WhatsApp sessions alive

`qr-bridge` holds a live Signal/Baileys session per linked phone. Three things
keep it alive, and they are already configured:

1. **`restart: unless-stopped`** — it comes back after a crash, a Docker daemon
   restart, and a VM reboot. Nothing systemd-level is needed; Docker's own unit
   is enabled by default on Ubuntu and restores the containers.
2. **The `qr_bridge_data` named volume** holds the auth state, so a container
   that is recreated *reconnects* rather than asking anyone to re-scan a QR.
3. **`deploy.sh` does not rebuild it unless `qr-bridge/` actually changed.** A
   Django-only deploy leaves the container — and every live session — untouched.
   Use `--force-qr` to override, and expect a brief reconnect when you do.

**Never run two qr-bridge containers against that volume.** Overlapping sockets
corrupt the session store, and the symptom is permanent "Waiting for this
message" on the customer's handset with nothing wrong on this side. This is why
the deploy is stop-then-start for that service and never a scale-up, and why
you should not "temporarily" start a second one to test something.

To check on it: `docker compose ... logs -f qr-bridge`, or
`curl -sf http://127.0.0.1:4001/health` on the VM.

---

## 9. Before you call it done

- [ ] `docker compose ... exec backend python manage.py check_production` → no failures
- [ ] `docker compose ... exec backend python manage.py createsuperuser`
- [ ] Billing webhook reachable: `curl -i https://your-domain.com/api/v1/billing/webhook/razorpay/`
      returns 400/401, **not** 404 or 502
- [ ] `./scripts/backup_postgres.sh` succeeds, and `BACKUP_REMOTE_TARGET` sends a
      copy **off this VM** — a backup on the machine it protects is not a backup
- [ ] Restore drill scheduled (`docs/DISASTER_RECOVERY.md`)
- [ ] `docs/SMOKE_TEST.md` passed against the live domain
