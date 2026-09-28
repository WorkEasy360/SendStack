#!/usr/bin/env bash
#
# The off-host backup drill: prove the copy that is NOT on this host restores.
#
#     ./scripts/verify_offhost_backup.sh
#     ./scripts/verify_offhost_backup.sh --use-latest      # skip taking a fresh backup
#     ./scripts/verify_offhost_backup.sh --keep-download   # leave the fetched archive on disk
#
# backup_postgres.sh already copies an archive off the host and checks that
# the object arrived with the right size. restore_postgres.sh already restores
# an archive into a scratch database and counts the rows. Between the two of
# them there was still a gap, and it was the one that matters:
#
#   **nothing had ever restored the remote copy.**
#
# Everything the deployment believed about its off-site backups rested on the
# local file being good and the upload reporting success. That is not the same
# claim. A bucket that silently truncates, an rclone remote pointed at a stale
# config, a passphrase rotated since the last local test, a server-side
# encryption key revoked, an object moved by a lifecycle rule to a tier that
# cannot be streamed -- every one of them passes an upload check and fails on
# the day it is needed.
#
# So this script does the round trip, in the order a disaster would:
#
#   1. Take a backup (or use the most recent one) and copy it off-host.
#   2. Move the local archive aside, so nothing downstream can quietly use it.
#   3. **Fetch the object back from the remote**, by its name, as a restore
#      would have to on a machine that has just been rebuilt.
#   4. Compare the fetched bytes against the original: same size, same SHA-256.
#   5. Restore the FETCHED copy -- never the local one -- into a scratch
#      database and run the verification queries.
#   6. Record the outcome, so check_production can report it as a fact.
#
# Step 2 is not ceremony. Without it the drill would happily "pass" while
# restoring the local file, which is exactly the reassuring lie it exists to
# prevent. The original is put back at the end, whatever happens.
#
# Environment: everything backup_postgres.sh reads, plus
#   BACKUP_DOWNLOAD_COMMAND   the counterpart to BACKUP_UPLOAD_COMMAND, with
#                             {name} and {dest} substituted. Required when the
#                             upload side uses BACKUP_UPLOAD_COMMAND, because
#                             a destination this script has never heard of
#                             cannot be read back by guesswork.

set -euo pipefail

# See the same block in backup_postgres.sh: MSYS rewrites container paths.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENV_FILE="$REPO_ROOT/.env"

# The same non-evaluating .env reader as backup_postgres.sh. A .env is not a
# shell script and is never sourced -- see the long comment there.
env_get() {
    local key="$1" line value
    [ -f "$ENV_FILE" ] || return 0
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1)" || true
    [ -n "$line" ] || return 0
    value="${line#*=}"
    value="${value%$'\r'}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s' "$value"
}

env_or() {
    local key="$1" fallback="$2" current
    current="$(printenv "$key" 2>/dev/null || true)"
    if [ -n "$current" ]; then printf '%s' "$current"; return 0; fi
    current="$(env_get "$key")"
    if [ -n "$current" ]; then printf '%s' "$current"; return 0; fi
    printf '%s' "$fallback"
}

BACKUP_DIR="$(env_or BACKUP_DIR "$REPO_ROOT/backups")"
BACKUP_REMOTE_TARGET="$(env_or BACKUP_REMOTE_TARGET "")"
BACKUP_UPLOAD_COMMAND="$(env_or BACKUP_UPLOAD_COMMAND "")"
BACKUP_DOWNLOAD_COMMAND="$(env_or BACKUP_DOWNLOAD_COMMAND "")"
BACKEND_CONTAINER="$(env_or BACKEND_CONTAINER multisender-backend-1)"

TAKE_BACKUP=1
KEEP_DOWNLOAD=0
while [ $# -gt 0 ]; do
    case "$1" in
        --use-latest)    TAKE_BACKUP=0 ;;
        --keep-download) KEEP_DOWNLOAD=1 ;;
        -h|--help)       sed -n '2,44p' "$0" | cut -c3- ; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

REMOTE_KIND="none"
REMOTE_NAME=""
FETCHED_BYTES=0
QUARANTINE=""
ORIGINAL=""
DOWNLOAD_DIR=""

# Declared before `fail` uses it, because `fail` records the failure through it.
record() {
    local status="$1" detail="$2"
    docker exec -i "$BACKEND_CONTAINER" python manage.py record_backup \
        --kind OFFSITE_RESTORE_TEST \
        --status "$status" \
        --artifact "${REMOTE_NAME:-unknown}" \
        --bytes "$FETCHED_BYTES" \
        --destination "$REMOTE_KIND" \
        --offsite \
        --detail "$detail" > /dev/null 2>&1 \
        || echo "WARNING: the drill result could not be recorded in the application." >&2
}

fail() {
    echo "FAIL: $*" >&2
    # A failed drill is a fact the deployment needs to know about, and the
    # most important one it can record. Reported before exiting, not after.
    record FAILED "$*"
    exit 1
}

# Put the world back however this exits. A drill that leaves the only local
# copy of a backup in a temp directory it then deletes is worse than no drill.
cleanup() {
    local code=$?
    if [ -n "$QUARANTINE" ] && [ -f "$QUARANTINE" ] && [ -n "$ORIGINAL" ] && [ ! -f "$ORIGINAL" ]; then
        mv "$QUARANTINE" "$ORIGINAL" && log "Local archive restored to ${ORIGINAL}."
    fi
    if [ "$KEEP_DOWNLOAD" -eq 0 ] && [ -n "$DOWNLOAD_DIR" ] && [ -d "$DOWNLOAD_DIR" ]; then
        # The fetched archive holds every phone number and message in the
        # deployment. It does not get to linger in a temp directory.
        rm -rf "$DOWNLOAD_DIR"
    fi
    return $code
}
trap cleanup EXIT

# --- Is there anywhere to fetch from at all? --------------------------------

if [ -z "$BACKUP_REMOTE_TARGET" ] && [ -z "$BACKUP_UPLOAD_COMMAND" ]; then
    echo "FAIL: no off-host destination is configured." >&2
    echo "      Set BACKUP_REMOTE_TARGET (s3://bucket/prefix or rclone:remote:path)," >&2
    echo "      or BACKUP_UPLOAD_COMMAND together with BACKUP_DOWNLOAD_COMMAND." >&2
    echo "      There is nothing off-host to verify, which is itself the finding." >&2
    exit 1
fi

if [ -n "$BACKUP_UPLOAD_COMMAND" ] && [ -z "$BACKUP_DOWNLOAD_COMMAND" ]; then
    fail "BACKUP_UPLOAD_COMMAND is set but BACKUP_DOWNLOAD_COMMAND is not. A destination this script cannot read back is a destination nobody can restore from."
fi

# --- 1. A fresh backup, copied off-host -------------------------------------

if [ "$TAKE_BACKUP" -eq 1 ]; then
    log "Taking a backup and copying it off-host..."
    "$REPO_ROOT/scripts/backup_postgres.sh" || fail "the backup itself failed; there is nothing to verify."
else
    log "Using the most recent existing archive (--use-latest)."
fi

# Newest by the ISO-8601 timestamp in the name, which sorts chronologically.
ORIGINAL="$(ls -1 "$BACKUP_DIR"/*.dump "$BACKUP_DIR"/*.dump.gpg 2>/dev/null | sort | tail -n 1 || true)"
if [ -z "$ORIGINAL" ] || [ ! -f "$ORIGINAL" ]; then
    fail "no archive found in ${BACKUP_DIR}."
fi

REMOTE_NAME="$(basename "$ORIGINAL")"
ORIGINAL_BYTES="$(wc -c < "$ORIGINAL" | tr -d ' ')"
ORIGINAL_SHA="$(sha256sum "$ORIGINAL" | cut -d' ' -f1)"
log "Archive under test: ${REMOTE_NAME} (${ORIGINAL_BYTES} bytes)"

# --- 2. Take the local copy out of reach ------------------------------------

QUARANTINE="${TMPDIR:-/tmp}/offhost-drill-original-$$-${REMOTE_NAME}"
mv "$ORIGINAL" "$QUARANTINE"
log "Local copy moved aside. Anything restored from here on came from the remote."

# --- 3. Fetch it back -------------------------------------------------------

DOWNLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/offhost-drill-XXXXXX")"
FETCHED="${DOWNLOAD_DIR}/${REMOTE_NAME}"

download_s3() {
    local bucket_path="${BACKUP_REMOTE_TARGET#s3://}"
    bucket_path="${bucket_path%/}"
    command -v aws > /dev/null 2>&1 || {
        echo "ERROR: BACKUP_REMOTE_TARGET is an s3:// URL but the AWS CLI is not installed." >&2
        return 1
    }
    log "Fetching s3://${bucket_path}/${REMOTE_NAME} ..."
    # Credentials come from the environment, an instance role or ~/.aws --
    # never from this repository and never on this command line.
    aws s3 cp "s3://${bucket_path}/${REMOTE_NAME}" "$FETCHED" --only-show-errors
}

download_rclone() {
    local remote="${BACKUP_REMOTE_TARGET#rclone:}"
    remote="${remote%/}"
    command -v rclone > /dev/null 2>&1 || {
        echo "ERROR: BACKUP_REMOTE_TARGET is an rclone: target but rclone is not installed." >&2
        return 1
    }
    log "Fetching ${remote}/${REMOTE_NAME} via rclone ..."
    rclone copyto "${remote}/${REMOTE_NAME}" "$FETCHED"
}

download_custom() {
    # {name} and {dest} substituted, then run verbatim -- the mirror image of
    # BACKUP_UPLOAD_COMMAND's {src} and {name}.
    local cmd="${BACKUP_DOWNLOAD_COMMAND//\{name\}/$REMOTE_NAME}"
    cmd="${cmd//\{dest\}/$FETCHED}"
    log "Fetching with the configured command..."
    sh -c "$cmd"
}

if [ -n "$BACKUP_DOWNLOAD_COMMAND" ]; then
    REMOTE_KIND="custom"
    download_custom || fail "the off-host fetch failed. The remote copy cannot be read back."
else
    case "$BACKUP_REMOTE_TARGET" in
        s3://*)   REMOTE_KIND="s3";     download_s3     || fail "the off-host fetch from S3 failed." ;;
        rclone:*) REMOTE_KIND="rclone"; download_rclone || fail "the off-host fetch via rclone failed." ;;
        *)        fail "BACKUP_REMOTE_TARGET '${BACKUP_REMOTE_TARGET}' is not an s3:// or rclone: target. Use BACKUP_UPLOAD_COMMAND and BACKUP_DOWNLOAD_COMMAND for anything else." ;;
    esac
fi

[ -f "$FETCHED" ] || fail "the fetch reported success but wrote no file."

# --- 4. Is it the same archive? ---------------------------------------------

FETCHED_BYTES="$(wc -c < "$FETCHED" | tr -d ' ')"
FETCHED_SHA="$(sha256sum "$FETCHED" | cut -d' ' -f1)"

if [ "$FETCHED_BYTES" != "$ORIGINAL_BYTES" ]; then
    fail "the remote copy is ${FETCHED_BYTES} bytes; the archive sent was ${ORIGINAL_BYTES}."
fi
# Size alone would miss a corrupted byte in the middle, which is precisely the
# failure that survives an upload check and then kills a restore.
if [ "$FETCHED_SHA" != "$ORIGINAL_SHA" ]; then
    fail "the remote copy's SHA-256 does not match the archive that was sent."
fi
log "Integrity verified: ${FETCHED_BYTES} bytes, SHA-256 matches."

# --- 5. Restore the fetched copy --------------------------------------------
#
# restore_postgres.sh defaults to a scratch database that it drops afterwards,
# and refuses the live one without an explicit --target. Handing it the
# FETCHED path rather than the local archive is the whole point of the drill.

log "Restoring the fetched copy into a scratch database..."
if ! "$REPO_ROOT/scripts/restore_postgres.sh" "$FETCHED"; then
    fail "the remote copy downloaded and matched, but would not restore."
fi

# --- 6. Write it down -------------------------------------------------------

record SUCCEEDED "Fetched from ${REMOTE_KIND} and restored into a scratch database."

echo
log "PASS: the off-host copy was fetched, matched byte for byte, and restored."
echo "      destination kind: ${REMOTE_KIND}"
echo "      archive:          ${REMOTE_NAME}"
echo "      bytes:            ${FETCHED_BYTES}"
echo "      sha256:           ${FETCHED_SHA}"
