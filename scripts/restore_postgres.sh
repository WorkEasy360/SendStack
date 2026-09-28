#!/usr/bin/env bash
#
# Restore a backup, and verify it actually came back.
#
#     ./scripts/restore_postgres.sh backups/multisender-20260830T101500Z.dump.gpg
#
# A backup nobody has restored is not a backup, it is a file. This script is
# the other half of backup_postgres.sh and is meant to be run on a schedule
# against a throwaway database, not only during a disaster.
#
# **It refuses to touch the live database unless explicitly told to.** By
# default it restores into a scratch database named after the archive, runs
# verification queries against it, prints the row counts, and drops it. That is
# the restore *test*. Overwriting production is a different, louder operation
# behind --target and --i-understand-this-overwrites.
#
# Usage:
#   restore_postgres.sh <archive>                     test restore, then drop
#   restore_postgres.sh <archive> --keep              test restore, keep the scratch db
#   restore_postgres.sh <archive> --target <db> --i-understand-this-overwrites
#
# Environment: BACKUP_PASSPHRASE (required for .gpg archives),
#              POSTGRES_CONTAINER, DB_USER, DB_NAME -- as in backup_postgres.sh.

set -euo pipefail

# Git Bash / MSYS on Windows rewrites any argument that looks like a Unix path
# into a Windows one before the process sees it. That is right for host paths
# and catastrophic for container paths: "/tmp/x.dump" passed to `docker exec`
# arrives as "C:/Users/.../Temp/x.dump", which does not exist inside the
# container, and the dump fails with a confusing "No such file or directory".
# A no-op on Linux and macOS, where the variable is simply unread.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- Reading .env --------------------------------------------------------
#
# Deliberately NOT `set -a; . ./.env`. A .env is not a shell script: a value
# containing parentheses, a backtick, a "&" or an unbalanced quote -- all
# perfectly legal in a generated password or a comment -- makes the shell exit
# with a syntax error, and the first thing that happens is the backup does not
# run. It also means every line of that file is *executed*, which is a command
# injection waiting for somebody to paste something odd into a password field.
#
# So: read the one key that is asked for, take everything after the first "=",
# strip one layer of matching quotes, and never evaluate any of it.
env_get() {
    local key="$1" line value
    [ -f "$ENV_FILE" ] || return 0
    # Last occurrence wins, matching how a shell would have sourced it.
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1)" || true
    [ -n "$line" ] || return 0
    value="${line#*=}"
    # Strip a trailing CR, for a file last edited on Windows.
    value="${value%$'\r'}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

# Environment wins over .env, so a one-off override on the command line works.
env_or() {
    local key="$1" fallback="$2" current
    current="$(printenv "$key" 2>/dev/null || true)"
    if [ -n "$current" ]; then printf '%s' "$current"; return 0; fi
    current="$(env_get "$key")"
    if [ -n "$current" ]; then printf '%s' "$current"; return 0; fi
    printf '%s' "$fallback"
}

ENV_FILE="$REPO_ROOT/.env"

POSTGRES_CONTAINER="$(env_or POSTGRES_CONTAINER multisender-postgres-1)"
DB_USER="$(env_or DB_USER multisender)"
DB_NAME="$(env_or DB_NAME multisender)"
BACKUP_PASSPHRASE="$(env_or BACKUP_PASSPHRASE "")"
[ "$BACKUP_PASSPHRASE" = "CHANGE_ME" ] && BACKUP_PASSPHRASE=""

ARCHIVE="${1:-}"
shift || true

TARGET=""
KEEP=0
OVERWRITE_ACK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        --i-understand-this-overwrites) OVERWRITE_ACK=1; shift ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
    echo "Usage: $0 <archive> [--target <db> --i-understand-this-overwrites] [--keep]" >&2
    exit 2
fi

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# --- Refuse to overwrite production by accident -----------------------------
SCRATCH=0
if [ -z "$TARGET" ]; then
    TARGET="restore_test_$(date -u +%Y%m%d%H%M%S)"
    SCRATCH=1
    log "No --target given: restoring into scratch database ${TARGET}."
elif [ "$TARGET" = "$DB_NAME" ] && [ "$OVERWRITE_ACK" -ne 1 ]; then
    cat >&2 <<MSG
REFUSING.

--target names the live database (${DB_NAME}). Restoring over it destroys
every row written since the backup was taken.

If that is genuinely what you want -- you are recovering from a disaster and
have accepted the data loss between the backup and now -- add:

    --i-understand-this-overwrites

If you are running a restore *test*, do not pass --target at all. The default
restores into a throwaway database and leaves production untouched.
MSG
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$POSTGRES_CONTAINER"; then
    echo "ERROR: container '$POSTGRES_CONTAINER' is not running." >&2
    exit 1
fi

psql_run() {
    docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$1" -v ON_ERROR_STOP=1 -tAc "$2"
}

# --- Decrypt if needed ------------------------------------------------------
PLAIN="$ARCHIVE"
TEMP=""
case "$ARCHIVE" in
    *.gpg)
        if [ -z "$BACKUP_PASSPHRASE" ]; then
            echo "ERROR: this archive is encrypted and BACKUP_PASSPHRASE is unset." >&2
            exit 1
        fi
        TEMP="$(mktemp)"
        chmod 600 "$TEMP"
        log "Decrypting..."
        printf '%s' "$BACKUP_PASSPHRASE" | gpg --batch --quiet --passphrase-fd 0 \
            --decrypt --output "$TEMP" "$ARCHIVE"
        PLAIN="$TEMP"
        ;;
esac
# The decrypted dump is a plaintext copy of the whole customer database sitting
# in a temp directory. It goes when this script does, however it exits.
# The `local code=$?` first line is load-bearing, and its absence was a real
# bug: this handler used to be a bare `[ -n "$TEMP" ] && { ... }`, and when
# TEMP was empty -- which is every restore of an unencrypted archive, the
# common case -- the test itself returned 1 and that became the script's exit
# status. A restore that printed "RESTORE TEST PASSED" exited non-zero, so any
# cron or CI job gating on the exit code saw every successful drill as a
# failure. Capturing the incoming status and returning it explicitly keeps a
# genuine failure's code intact while stopping the cleanup from inventing one.
cleanup() {
    local code=$?
    [ -n "$TEMP" ] && { shred -u "$TEMP" 2> /dev/null || rm -f "$TEMP"; }
    return $code
}
trap cleanup EXIT

# --- Restore ----------------------------------------------------------------
log "Creating database ${TARGET}..."
docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${TARGET}\";" > /dev/null
docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE \"${TARGET}\";" > /dev/null

log "Restoring..."
# --exit-on-error so a restore that half-worked is reported as a failure and
# not as a success with missing tables. --no-owner because the roles in a
# scratch database are not the roles the dump was taken under.
docker exec -i "$POSTGRES_CONTAINER" \
    pg_restore -U "$DB_USER" -d "$TARGET" --no-owner --exit-on-error < "$PLAIN"

# --- Verify -----------------------------------------------------------------
#
# The point of the whole exercise. "pg_restore exited 0" only says the file
# parsed; these queries say the customer data is actually there.
log ""
log "Verification:"
FAILED=0
verify() {
    local label="$1" query="$2" minimum="${3:-0}"
    local value
    value="$(psql_run "$TARGET" "$query" 2> /dev/null || echo "ERROR")"
    if [ "$value" = "ERROR" ]; then
        printf '  %-28s %s\n' "$label" "QUERY FAILED"
        FAILED=1
        return
    fi
    if [ "$value" -lt "$minimum" ] 2> /dev/null; then
        printf '  %-28s %s (expected at least %s)\n' "$label" "$value" "$minimum"
        FAILED=1
        return
    fi
    printf '  %-28s %s\n' "$label" "$value"
}

verify "tables"          "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 20
verify "django_migrations" "SELECT count(*) FROM django_migrations;" 1
verify "tenants"         "SELECT count(*) FROM tenants_tenant;" 1
verify "users"           "SELECT count(*) FROM accounts_user;" 1
verify "contacts"        "SELECT count(*) FROM contacts_contact;"
verify "campaigns"       "SELECT count(*) FROM campaigns_campaign;"
verify "messages"        "SELECT count(*) FROM messaging_message;"
verify "consent events"  "SELECT count(*) FROM compliance_consentevent;"
verify "legal acceptances" "SELECT count(*) FROM legal_legalacceptance;"

log ""
if [ "$FAILED" -ne 0 ]; then
    log "RESTORE TEST FAILED. This backup is not trustworthy."
    report_restore FAILED "One or more verification queries failed against ${TARGET}."
    [ "$SCRATCH" -eq 1 ] && [ "$KEEP" -eq 0 ] && docker exec -i "$POSTGRES_CONTAINER" \
        psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS \"${TARGET}\";" > /dev/null
    exit 1
fi

# --- Reporting the outcome --------------------------------------------------
#
# A restore test that only prints to a terminal is a restore test nobody can
# prove happened three weeks later. This writes a RESTORE_TEST row so
# `manage.py check_production` can report a recorded run rather than an
# operator's memory (which is all BACKUP_VERIFIED_AT ever was).
#
# Best-effort: a successful restore that could not be recorded is still a
# successful restore, and failing here would teach people to ignore the exit
# code of the thing that actually matters.
report_restore() {
    local status="$1" detail="$2"
    local backend
    backend="$(printenv BACKEND_CONTAINER 2>/dev/null || echo multisender-backend-1)"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$backend" || return 0
    docker exec "$backend" python manage.py record_backup \
        --kind RESTORE_TEST \
        --status "$status" \
        --artifact "$(basename "$ARCHIVE")" \
        --detail "$detail" > /dev/null 2>&1 || true
}

log "RESTORE TEST PASSED."
log "Archive:     $ARCHIVE"
log "Restored to: $TARGET"
report_restore SUCCEEDED "Restored and verified into ${TARGET}."

if [ "$SCRATCH" -eq 1 ] && [ "$KEEP" -eq 0 ]; then
    log "Dropping scratch database ${TARGET}..."
    docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS \"${TARGET}\";" > /dev/null
    log "Done."
else
    log "Left ${TARGET} in place. Drop it when finished:"
    log "  docker exec -i ${POSTGRES_CONTAINER} psql -U ${DB_USER} -d postgres -c 'DROP DATABASE \"${TARGET}\";'"
fi

log ""
log "This run has been recorded, so 'manage.py check_production' and"
log "/health/backups/ will report it without anything further from you."
