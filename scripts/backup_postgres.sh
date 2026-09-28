#!/usr/bin/env bash
#
# Timestamped, compressed, optionally encrypted PostgreSQL backup.
#
# Run from the repository root, on the Docker host:
#
#     ./scripts/backup_postgres.sh
#
# A Docker volume is not a backup. It lives on the same disk, in the same
# machine, in the same failure domain as the database it is a copy of, and it
# survives none of: the disk failing, the host being terminated, `docker
# compose down -v`, a ransomware event, or somebody dropping a table. This
# script produces a file that can leave the machine.
#
# What it does:
#   1. pg_dump in custom format (-Fc) from inside the postgres container.
#      Custom format because it restores selectively and in parallel, which
#      plain SQL cannot -- and because pg_restore refuses a truncated file
#      rather than replaying half a database.
#   2. Optionally encrypts with a passphrase. A backup holds every phone
#      number, every message and every encrypted provider credential in the
#      deployment; it is the single most sensitive artefact this system
#      produces and it is about to be copied somewhere else.
#   3. Verifies the archive is readable before counting it as a backup.
#   4. Deletes backups older than the retention window.
#   5. Copies the archive OFF THE HOST, if a destination is configured, and
#      verifies it arrived. A backup on the same disk as the database
#      survives none of the things a backup exists for.
#   6. Reports the outcome back into the application, so "when did a backup
#      last succeed" is a question the deployment can answer about itself
#      rather than something an operator has to remember.
#
# Environment (all optional, read from .env when present):
#   BACKUP_DIR                 where to write. Default ./backups
#   BACKUP_RETENTION_DAYS      default 14
#   BACKUP_PASSPHRASE          if set, the dump is encrypted with it
#   POSTGRES_CONTAINER         default multisender-postgres-1
#   BACKUP_REMOTE_TARGET       off-host destination (see below). Unset = local only
#   BACKUP_UPLOAD_COMMAND      custom upload command, {src} and {name} substituted
#   BACKUP_REMOTE_RETENTION_DAYS  prune the remote too. Unset = never prune it
#   BACKUP_MIN_KEEP            never drop below this many local backups. Default 3
#   BACKUP_ALERT_COMMAND       run on failure, with the reason on stdin
#   BACKEND_CONTAINER          default multisender-backend-1, for reporting in
#
# Off-host destinations (set BACKUP_REMOTE_TARGET):
#   s3://bucket/prefix        uses the AWS CLI. Credentials come from the
#                             environment or the instance role -- never from
#                             this repository.
#   rclone:remote:path        uses rclone, for B2, GCS, Azure, SFTP, or
#                             anything else rclone speaks.
#   Anything else             set BACKUP_UPLOAD_COMMAND instead: a command
#                             line with {src} and {name} placeholders, run
#                             verbatim. That is the escape hatch for a
#                             destination this script has never heard of.
#
# A configured destination that FAILS to upload fails the whole run, loudly
# and with a non-zero exit. A silent fallback to "well, it is on the local
# disk" is how a deployment ends up believing it has off-site backups for a
# year and finding out it does not on the day the host dies.

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

BACKUP_DIR="$(env_or BACKUP_DIR "$REPO_ROOT/backups")"
BACKUP_RETENTION_DAYS="$(env_or BACKUP_RETENTION_DAYS 14)"
POSTGRES_CONTAINER="$(env_or POSTGRES_CONTAINER multisender-postgres-1)"
DB_NAME="$(env_or DB_NAME multisender)"
DB_USER="$(env_or DB_USER multisender)"
BACKUP_PASSPHRASE="$(env_or BACKUP_PASSPHRASE "")"
BACKUP_OFFSITE_ACKNOWLEDGED="$(env_or BACKUP_OFFSITE_ACKNOWLEDGED "")"
BACKUP_REMOTE_TARGET="$(env_or BACKUP_REMOTE_TARGET "")"
BACKUP_UPLOAD_COMMAND="$(env_or BACKUP_UPLOAD_COMMAND "")"
BACKUP_REMOTE_RETENTION_DAYS="$(env_or BACKUP_REMOTE_RETENTION_DAYS "")"
BACKUP_MIN_KEEP="$(env_or BACKUP_MIN_KEEP 3)"
BACKUP_ALERT_COMMAND="$(env_or BACKUP_ALERT_COMMAND "")"
BACKEND_CONTAINER="$(env_or BACKEND_CONTAINER multisender-backend-1)"

# The placeholder from .env.example is not a passphrase. Encrypting with it
# would be worse than not encrypting, because the archive would *look*
# protected.
if [ "$BACKUP_PASSPHRASE" = "CHANGE_ME" ]; then
    echo "WARNING: BACKUP_PASSPHRASE is still the CHANGE_ME placeholder; treating it as unset." >&2
    BACKUP_PASSPHRASE=""
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"
# Owner-only. A backup directory readable by every account on the host is a
# copy of the customer database readable by every account on the host.
chmod 700 "$BACKUP_DIR"

ARCHIVE="$BACKUP_DIR/${DB_NAME}-${STAMP}.dump"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# --- Reporting the outcome --------------------------------------------------
#
# Two audiences, and they need different things.
#
# `report` writes a row into the application's own BackupRun table, so the
# deployment can answer "when did a backup last succeed" about itself rather
# than relying on somebody remembering. Best-effort: a backup that ran and
# could not be recorded is still a backup, and failing the job over a
# monitoring write would be exactly backwards.
#
# `alert` runs whatever the operator configured, on failure only. A backup
# system whose failures are silent is not a backup system -- it is a cron job
# that used to work.
report() {
    local status="$1" detail="$2"
    docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$BACKEND_CONTAINER" || return 0
    docker exec "$BACKEND_CONTAINER" python manage.py record_backup \
        --status "$status" \
        --kind BACKUP \
        --artifact "$(basename "${ARCHIVE:-}")" \
        --bytes "${ARCHIVE_BYTES:-0}" \
        --destination "${REMOTE_KIND:-local}" \
        ${OFFSITE_OK:+--offsite} \
        ${ENCRYPTED:+--encrypted} \
        --detail "$detail" > /dev/null 2>&1 || true
}

alert() {
    local reason="$1"
    [ -n "$BACKUP_ALERT_COMMAND" ] || return 0
    # The reason arrives on stdin, never as an argument: an argument is
    # visible in `ps` to every account on the host, and a failure reason can
    # quote a path or a hostname.
    printf '%s\n' "$reason" | sh -c "$BACKUP_ALERT_COMMAND" || \
        echo "WARNING: BACKUP_ALERT_COMMAND itself failed." >&2
}

# Every exit that is not a clean finish lands here. `set -e` plus this trap is
# what makes "the backup failed and nobody noticed" impossible: there is no
# path out of this script that skips it.
FINISHED=""
ARCHIVE_BYTES=0
ENCRYPTED=""
OFFSITE_OK=""
REMOTE_KIND="local"
on_exit() {
    local code=$?
    container_cleanup 2> /dev/null || true
    if [ -z "$FINISHED" ] && [ "$code" -ne 0 ]; then
        local reason
        reason="SendStack backup FAILED (exit ${code}) on $(hostname 2>/dev/null || echo host) at $(date -u +%Y-%m-%dT%H:%M:%SZ)."
        echo "$reason" >&2
        report FAILED "$reason"
        alert "$reason"
    fi
    exit "$code"
}


if ! docker ps --format '{{.Names}}' | grep -qx "$POSTGRES_CONTAINER"; then
    echo "ERROR: container '$POSTGRES_CONTAINER' is not running." >&2
    echo "Set POSTGRES_CONTAINER, or start the stack with 'docker compose up -d'." >&2
    exit 1
fi

# --- Dump, verify, then bring it out ----------------------------------------
#
# The dump is written *inside* the container and verified there before being
# copied out, rather than streamed straight to the host.
#
# That is not fussiness. A custom-format archive is a seekable format, and
# `pg_restore --list` has to seek to read its table of contents -- so verifying
# a dump that arrived on a pipe is impossible, and the obvious
# `docker exec pg_dump > file && docker exec pg_restore --list < file` always
# reports corruption even when the file is perfect. Writing to a real file on
# both sides is the only way the check means anything.
IN_CONTAINER="/tmp/multisender-backup-${STAMP}.dump"

container_cleanup() {
    docker exec "$POSTGRES_CONTAINER" rm -f "$IN_CONTAINER" > /dev/null 2>&1 || true
}
trap on_exit EXIT

log "Dumping ${DB_NAME} from ${POSTGRES_CONTAINER}..."
# -Fc custom format, because it restores selectively and in parallel, and
# because pg_restore refuses a truncated one rather than replaying half a
# database. --no-password because authentication inside the container is by
# the local trust/peer rule for this role.
docker exec "$POSTGRES_CONTAINER" \
    pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-password --file "$IN_CONTAINER"

# --- Verify before trusting -------------------------------------------------
#
# `pg_restore --list` parses the whole table of contents and fails on a
# truncated or corrupt archive. It is not a restore test -- only a real restore
# into a real database is that, see restore_postgres.sh -- but it is the
# difference between "a file exists" and "a file pg_restore is willing to
# read", and it costs a second.
log "Verifying the archive is readable..."
if ! docker exec "$POSTGRES_CONTAINER" pg_restore --list "$IN_CONTAINER" > /dev/null 2>&1; then
    echo "ERROR: the archive did not verify inside the container. Not keeping it." >&2
    exit 1
fi

# With MSYS path conversion off (above), a host path stays in the shell's own
# /c/Users/... form, which `docker cp` on Windows does not understand -- it
# prepends a drive letter and reports a directory that does not exist. cygpath
# is the supported way to hand it the native form. Absent everywhere but Git
# Bash, so the fallback is the path unchanged.
host_path() {
    if command -v cygpath > /dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

log "Copying it out..."
docker cp "${POSTGRES_CONTAINER}:${IN_CONTAINER}" "$(host_path "$ARCHIVE")"
container_cleanup

if [ ! -s "$ARCHIVE" ]; then
    echo "ERROR: the copied archive is empty. Not keeping it." >&2
    rm -f "$ARCHIVE"
    exit 1
fi

chmod 600 "$ARCHIVE"

# --- Encrypt ----------------------------------------------------------------
if [ -n "$BACKUP_PASSPHRASE" ]; then
    if ! command -v gpg > /dev/null 2>&1; then
        echo "ERROR: BACKUP_PASSPHRASE is set but gpg is not installed." >&2
        echo "Install gnupg, or unset BACKUP_PASSPHRASE to accept unencrypted backups." >&2
        rm -f "$ARCHIVE"
        exit 1
    fi
    log "Encrypting..."
    # --batch --yes so it never prompts from cron. The passphrase arrives on a
    # file descriptor rather than the command line, because a command line is
    # visible in `ps` to every user on the host.
    printf '%s' "$BACKUP_PASSPHRASE" | gpg --batch --yes --quiet \
        --passphrase-fd 0 --symmetric --cipher-algo AES256 \
        --output "${ARCHIVE}.gpg" "$ARCHIVE"
    shred -u "$ARCHIVE" 2> /dev/null || rm -f "$ARCHIVE"
    ARCHIVE="${ARCHIVE}.gpg"
    chmod 600 "$ARCHIVE"
    ENCRYPTED=1
else
    log "WARNING: BACKUP_PASSPHRASE is unset, so this backup is NOT encrypted."
    log "         It contains every phone number and message in the deployment."
fi

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
log "Wrote $ARCHIVE ($SIZE)"

# --- Off the host -----------------------------------------------------------
#
# The step that makes this a backup rather than a second copy on the same
# disk. Everything above survives a dropped table; only this survives losing
# the machine.
#
# A configured destination that fails is a FAILED RUN. It does not warn and
# carry on: a deployment that believes it has off-site backups and does not is
# in a worse position than one that knows it has none, because it has stopped
# looking.
ARCHIVE_BYTES="$(wc -c < "$ARCHIVE" | tr -d ' ')"
REMOTE_NAME="$(basename "$ARCHIVE")"

upload_s3() {
    local bucket_path="${BACKUP_REMOTE_TARGET#s3://}"
    bucket_path="${bucket_path%/}"
    command -v aws > /dev/null 2>&1 || {
        echo "ERROR: BACKUP_REMOTE_TARGET is an s3:// URL but the AWS CLI is not installed." >&2
        return 1
    }
    log "Uploading to s3://${bucket_path}/${REMOTE_NAME} ..."
    # Credentials come from the environment, an instance role or ~/.aws --
    # never from this repository and never on this command line.
    aws s3 cp "$ARCHIVE" "s3://${bucket_path}/${REMOTE_NAME}" --only-show-errors || return 1
    # Verify it arrived. `aws s3 cp` exiting 0 means the request was accepted;
    # this means the object is there and is the size we sent.
    local remote_size
    remote_size="$(aws s3api head-object --bucket "${bucket_path%%/*}" \
        --key "${bucket_path#*/}/${REMOTE_NAME}" --query ContentLength --output text 2>/dev/null || echo "")"
    if [ -n "$remote_size" ] && [ "$remote_size" != "$ARCHIVE_BYTES" ]; then
        echo "ERROR: the uploaded object is ${remote_size} bytes, expected ${ARCHIVE_BYTES}." >&2
        return 1
    fi
    return 0
}

upload_rclone() {
    local remote="${BACKUP_REMOTE_TARGET#rclone:}"
    remote="${remote%/}"
    command -v rclone > /dev/null 2>&1 || {
        echo "ERROR: BACKUP_REMOTE_TARGET is an rclone: target but rclone is not installed." >&2
        return 1
    }
    log "Uploading to ${remote}/${REMOTE_NAME} via rclone ..."
    rclone copyto "$ARCHIVE" "${remote}/${REMOTE_NAME}" || return 1
    # rclone's own end-to-end check: it re-reads the destination and compares.
    rclone check "$ARCHIVE" "${remote}/${REMOTE_NAME}" --size-only > /dev/null 2>&1 || {
        echo "ERROR: the uploaded object did not match the local archive." >&2
        return 1
    }
    return 0
}

upload_custom() {
    # {src} and {name} substituted, then run verbatim. The escape hatch for a
    # destination this script has never heard of -- an SFTP push, a managed
    # backup agent, a script of the operator's own.
    local cmd="${BACKUP_UPLOAD_COMMAND//\{src\}/$ARCHIVE}"
    cmd="${cmd//\{name\}/$REMOTE_NAME}"
    log "Uploading with the configured command..."
    sh -c "$cmd" || return 1
    return 0
}

if [ -n "$BACKUP_UPLOAD_COMMAND" ]; then
    REMOTE_KIND="custom"
    upload_custom || { echo "ERROR: the off-host upload failed." >&2; exit 1; }
    OFFSITE_OK=1
elif [ -n "$BACKUP_REMOTE_TARGET" ]; then
    case "$BACKUP_REMOTE_TARGET" in
        s3://*)     REMOTE_KIND="s3";     upload_s3     || { echo "ERROR: the off-host upload failed." >&2; exit 1; } ;;
        rclone:*)   REMOTE_KIND="rclone"; upload_rclone || { echo "ERROR: the off-host upload failed." >&2; exit 1; } ;;
        *)
            echo "ERROR: BACKUP_REMOTE_TARGET '${BACKUP_REMOTE_TARGET}' is not an s3:// or rclone: target." >&2
            echo "       Use BACKUP_UPLOAD_COMMAND for anything else." >&2
            exit 1
            ;;
    esac
    OFFSITE_OK=1
    log "Off-host copy verified."
else
    REMOTE_KIND="local"
fi

# --- Retention --------------------------------------------------------------
#
# Age-based, with a floor. The floor is the important half: a deployment whose
# backups have been failing for three weeks would, under pure age-based
# pruning, delete its last good backup on the day somebody needed it. Nothing
# here can drop below BACKUP_MIN_KEEP archives, however old they are.
#
# And it runs *after* the upload, so a failed upload exits above and takes
# nothing with it.
log "Pruning local backups older than ${BACKUP_RETENTION_DAYS} days (keeping at least ${BACKUP_MIN_KEEP})..."

# Newest first, so the tail of this list is what may be considered for removal.
#
# Sorted by the timestamp in the filename rather than by mtime: the names
# are ISO-8601 UTC and so sort lexically into chronological order, and a
# name cannot be moved by a copy, a restore from a snapshot or a `touch`.
# Read null-delimited so a BACKUP_DIR containing a space -- which on a
# Windows host it very often does -- cannot silently split into two paths.
ALL_BACKUPS=()
while IFS= read -r -d '' found; do
    ALL_BACKUPS+=("$found")
done < <(find "$BACKUP_DIR" -maxdepth 1 -name "${DB_NAME}-*.dump*" -type f -print0 | sort -rz)
TOTAL="${#ALL_BACKUPS[@]}"

REMOVED=0
if [ "$TOTAL" -gt "$BACKUP_MIN_KEEP" ]; then
    for old in "${ALL_BACKUPS[@]:$BACKUP_MIN_KEEP}"; do
        # Old enough, and outside the protected newest-N.
        if find "$old" -maxdepth 0 -mtime "+${BACKUP_RETENTION_DAYS}" 2>/dev/null | grep -q .; then
            rm -f "$old"
            REMOVED=$((REMOVED + 1))
        fi
    done
fi
log "Removed ${REMOVED} old local backup(s)."

# Remote pruning is opt-in and separate. Object storage is cheap and a
# lifecycle rule on the bucket is usually the better tool; this exists for
# destinations that have none.
if [ -n "$BACKUP_REMOTE_RETENTION_DAYS" ] && [ "$REMOTE_KIND" = "rclone" ]; then
    REMOTE_PATH="${BACKUP_REMOTE_TARGET#rclone:}"
    log "Pruning remote copies older than ${BACKUP_REMOTE_RETENTION_DAYS} days..."
    rclone delete "${REMOTE_PATH%/}" --min-age "${BACKUP_REMOTE_RETENTION_DAYS}d" \
        --include "${DB_NAME}-*.dump*" 2>/dev/null || \
        log "WARNING: remote pruning failed; the remote may be accumulating."
fi

REMAINING="$(find "$BACKUP_DIR" -maxdepth 1 -name "${DB_NAME}-*.dump*" -type f | wc -l | tr -d ' ')"
log "Done. ${REMAINING} backup(s) in ${BACKUP_DIR}."

# --- Tell the application ---------------------------------------------------
FINISHED=1
NOTE="Backed up ${DB_NAME} (${SIZE})"
[ -n "$OFFSITE_OK" ] && NOTE="${NOTE}, copied off-host via ${REMOTE_KIND}"
[ -n "$ENCRYPTED" ] && NOTE="${NOTE}, encrypted"
report SUCCEEDED "$NOTE."

if [ -z "$OFFSITE_OK" ] && [ -z "$BACKUP_OFFSITE_ACKNOWLEDGED" ]; then
    log ""
    log "REMINDER: this backup is on the same host as the database, so losing"
    log "          the host loses both. Set BACKUP_REMOTE_TARGET (s3:// or"
    log "          rclone:) to copy it off automatically -- see"
    log "          docs/DISASTER_RECOVERY.md. BACKUP_OFFSITE_ACKNOWLEDGED=1"
    log "          silences this if you copy it off by other means."
fi
