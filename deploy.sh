#!/usr/bin/env bash
#
# Deploy SendStack on the Oracle Cloud VM.
#
# This is docs/RELEASE_RUNBOOK.md with the judgement calls removed: the same
# order, the same checks, and the same rule that a failed check stops the
# deploy rather than being weighed up. What it does not do is decide anything
# the runbook leaves to a person -- it will not roll back for you, because
# rolling back correctly depends on why it failed, and it prints the exact
# commands to do so instead.
#
#   ./deploy.sh                 full gated deploy
#   ./deploy.sh --skip-backup   for a frontend-only redeploy you have judged safe
#   ./deploy.sh --force-qr      rebuild qr-bridge too (see WHATSAPP SESSIONS)
#   ./deploy.sh --no-pull       deploy the working tree as-is, no git pull
#
# WHATSAPP SESSIONS
#
# qr-bridge holds live WhatsApp device sessions. It is deliberately NOT
# rebuilt unless its own sources changed, because a new image id makes Compose
# recreate the container, and every linked phone has to re-establish its
# session when that happens. The auth state survives in the qr_bridge_data
# volume, so this is a reconnect rather than a re-pair -- but it is still a
# gap in sending, and there is no reason to take it when only Django changed.
#
# Never run two qr-bridge containers against the same volume. Overlapping
# sockets corrupt the Signal session store, and the symptom is permanent
# "Waiting for this message" on the customer's phone with no error anywhere on
# this side. That is why the deploy below is stop-then-start for that service
# and never a scale-up.
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

SKIP_BACKUP=0
FORCE_QR=0
DO_PULL=1
for arg in "$@"; do
    case "$arg" in
        --skip-backup) SKIP_BACKUP=1 ;;
        --force-qr)    FORCE_QR=1 ;;
        --no-pull)     DO_PULL=0 ;;
        -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

step()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()    { printf '    \033[32mok\033[0m  %s\n' "$*"; }
die()   { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
step "Preconditions"

[ -f .env ] || die ".env not found. Copy .env.example and fill it in."

# The one file that must not be in play. It forces development settings, which
# disables the production guards -- and on a public host that means DEBUG
# behaviour and permissive CORS. Naming both -f files above already prevents
# Compose loading it, but its presence means somebody is confused about which
# stack this is, and that is worth stopping for.
if [ -f docker-compose.override.yml ]; then
    die "docker-compose.override.yml exists. It forces config.settings.development and must not be on a production host. Delete or rename it."
fi

command -v docker >/dev/null || die "docker not installed"
$COMPOSE version >/dev/null 2>&1 || die "docker compose v2 not available"

# Rollback target. Captured before anything changes, because finding it out
# afterwards, under pressure, is the thing the runbook exists to prevent.
PREVIOUS_COMMIT="$(git rev-parse HEAD)"
ok "current commit ${PREVIOUS_COMMIT:0:12} -- this is your rollback target"

# ---------------------------------------------------------------------------
if [ "$DO_PULL" -eq 1 ]; then
    step "Pull"
    git diff --quiet || die "working tree is dirty. Commit, stash, or use --no-pull."
    # --ff-only: a merge commit created unattended on a production host is a
    # state that exists nowhere else and that nobody can review.
    git pull --ff-only || die "git pull failed (not a fast-forward?)"
    ok "now at $(git rev-parse --short HEAD)"
else
    step "Pull (skipped)"
fi

NEW_COMMIT="$(git rev-parse HEAD)"
export APP_BUILD="$(git rev-parse --short HEAD)"

# ---------------------------------------------------------------------------
step "Backup"

if [ "$SKIP_BACKUP" -eq 1 ]; then
    printf '    \033[33mskipped by --skip-backup\033[0m\n'
elif [ -x scripts/backup_postgres.sh ]; then
    # A migration that turns out to be wrong is unrecoverable without this,
    # and taking it costs two minutes.
    ./scripts/backup_postgres.sh || die "backup failed -- do not deploy behind a backup that did not complete"
    ok "database backed up"
else
    die "scripts/backup_postgres.sh not found or not executable"
fi

# ---------------------------------------------------------------------------
step "Build"

# All three backend images share a build context but Compose tags them
# separately. Building only 'backend' leaves the worker and beat running the
# previous code, and the symptom is a fix that appears not to have worked --
# so they are always built together.
$COMPOSE build backend celery-worker celery-beat || die "backend image build failed"
ok "backend, celery-worker, celery-beat"

# nginx bakes the built SPA into its own image, so any frontend change needs
# it -- and it is cheap enough not to bother detecting.
$COMPOSE build nginx || die "nginx image build failed"
ok "nginx (includes the Vite build)"

QR_CHANGED=0
if [ "$FORCE_QR" -eq 1 ]; then
    QR_CHANGED=1
elif [ "$PREVIOUS_COMMIT" != "$NEW_COMMIT" ] \
     && ! git diff --quiet "$PREVIOUS_COMMIT" "$NEW_COMMIT" -- qr-bridge/; then
    QR_CHANGED=1
fi

if [ "$QR_CHANGED" -eq 1 ]; then
    $COMPOSE build qr-bridge || die "qr-bridge image build failed"
    ok "qr-bridge (sources changed -- linked phones will reconnect)"
else
    ok "qr-bridge unchanged, not rebuilt (WhatsApp sessions stay up)"
fi

# ---------------------------------------------------------------------------
step "Pre-flight checks"

# Runs in a throwaway container on the NEW image, before anything serving
# traffic is touched. The entrypoint only auto-migrates when the command is
# gunicorn, so these are read-only.
$COMPOSE run --rm --no-deps -T backend python manage.py check_production \
    || die "check_production found a blocking failure -- fix .env before deploying"
ok "check_production clean"

$COMPOSE run --rm --no-deps -T backend python manage.py makemigrations --check --dry-run \
    || die "a model change has no migration. Generate it, commit it, and redeploy."
ok "no missing migrations"

# ---------------------------------------------------------------------------
step "Migrate"

# Before the new containers serve traffic, so the schema is ready when they
# start. This means the OLD code runs briefly against the NEW schema: additive
# migrations are safe, and one that drops or renames a column the running code
# still reads must be split across two releases.
$COMPOSE run --rm -T backend python manage.py migrate --noinput \
    || die "migration failed. Restore from the backup taken above -- do not hand-repair a half-migrated schema."
ok "migrations applied"

# ---------------------------------------------------------------------------
step "Deploy"

$COMPOSE up -d --no-build || die "compose up failed"

# Not optional. nginx resolves the 'backend' upstream once at startup, so a
# recreated backend container gets a new IP and every /api/ call returns 502
# while the SPA still loads -- which reads as a frontend bug and sends people
# looking in entirely the wrong place.
$COMPOSE restart nginx
ok "containers up, nginx re-resolved the backend upstream"

# ---------------------------------------------------------------------------
step "Health"

# /health/ is served over plain :80 under both layouts precisely so this works
# without dragging certificate verification into a deploy gate.
for i in $(seq 1 30); do
    if curl -sf -o /dev/null "http://localhost:${HTTP_PORT:-80}/health/live/"; then break; fi
    [ "$i" -eq 30 ] && die "/health/live/ never came up. Check: $COMPOSE logs backend"
    sleep 2
done
ok "/health/live/"

for i in $(seq 1 30); do
    if curl -sf -o /dev/null "http://localhost:${HTTP_PORT:-80}/health/ready/"; then break; fi
    [ "$i" -eq 30 ] && die "/health/ready/ failing -- the app cannot reach Postgres or Redis. Check: $COMPOSE logs backend"
    sleep 2
done
ok "/health/ready/ (database and Redis reachable)"

DEPLOYED_VERSION="$($COMPOSE exec -T backend python manage.py shell -c \
    'from apps.health.checks import check_version; print(check_version())' 2>/dev/null || true)"
echo "    version reported: ${DEPLOYED_VERSION:-<unavailable>}"
case "$DEPLOYED_VERSION" in
    *"${APP_BUILD}"*) ok "running the commit we just built" ;;
    *) printf '    \033[33mwarning\033[0m  version does not mention %s -- the image may not have rebuilt\n' "$APP_BUILD" ;;
esac

# ---------------------------------------------------------------------------
step "Celery"

# A queue nobody consumes does not error. Tasks sit in Redis for ever, which
# looks exactly like a worker that is merely idle -- messages queue and never
# send, with nothing in any log. This is the check that catches it.
$COMPOSE exec -T backend celery -A config inspect ping >/dev/null 2>&1 \
    || die "no Celery worker replied to ping. Check: $COMPOSE logs celery-worker"
ok "worker responding"

ACTIVE_QUEUES="$($COMPOSE exec -T backend celery -A config inspect active_queues 2>/dev/null || true)"
MISSING=""
for q in celery messages campaigns bulk maintenance; do
    case "$ACTIVE_QUEUES" in *"$q"*) ;; *) MISSING="$MISSING $q" ;; esac
done
if [ -n "$MISSING" ]; then
    printf '    \033[33mwarning\033[0m  queues not being consumed:%s\n' "$MISSING"
    printf '              Tasks routed there will vanish silently. Check celery-worker -Q.\n'
else
    ok "all five queues consumed"
fi

# ---------------------------------------------------------------------------
printf '\n\033[32mDeployed.\033[0m %s -> %s\n' "${PREVIOUS_COMMIT:0:12}" "${NEW_COMMIT:0:12}"
cat <<EOF

Watch for 30 minutes (docs/RELEASE_RUNBOOK.md step 9):
  - new exception types in the error monitor, not the absolute count
  - GET /health/dead-letters/     jobs that gave up
  - GET /health/operations/       read queues_unconsumed first
  - queue depths not growing monotonically

To roll back:
  git reset --hard $PREVIOUS_COMMIT
  ./deploy.sh --no-pull --skip-backup
  # If a migration was applied and must also come off, restore the archive
  # taken above with scripts/restore_postgres.sh -- read docs/DISASTER_RECOVERY.md first.
EOF
