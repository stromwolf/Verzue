#!/usr/bin/env bash
# Verzue Redis Local Backup — 15-minute snapshot to /var/lib/verzue-backups/.
# Tiers: 15min (8hr) → hourly (48hr) → daily (14d). Promotion happens at the
# tier boundary (top of hour, 02:00 UTC) by hard-linking the same archive.
#
# Hard-linking means promotion is free (no extra disk, no copy time) and
# deletion of any tier doesn't affect the others — they're independent
# directory entries pointing at one inode.

set -euo pipefail

# --- Config ---------------------------------------------------------------
REDIS_DIR="/var/lib/redis"
BACKUP_ROOT="/var/lib/verzue-backups"
DIR_15MIN="$BACKUP_ROOT/15min"
DIR_HOURLY="$BACKUP_ROOT/hourly"
DIR_DAILY="$BACKUP_ROOT/daily"

RETAIN_15MIN=32   # 8 hours of 15-min snapshots
RETAIN_HOURLY=48  # 48 hours
RETAIN_DAILY=14   # 14 days

HOSTNAME_TAG="$(hostname -s)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MINUTE="$(date -u +%M)"
HOUR="$(date -u +%H)"
ARCHIVE_NAME="verzue-redis-${HOSTNAME_TAG}-${TIMESTAMP}.tar.gz"

# --- Setup ----------------------------------------------------------------
mkdir -p "$DIR_15MIN" "$DIR_HOURLY" "$DIR_DAILY"
TMP_WORK="$(mktemp -d -p "$BACKUP_ROOT" .work.XXXXXX)"

# Discord webhook (optional). Set DISCORD_WEBHOOK_URL in
# /etc/default/verzue-redis-backup and the systemd unit will load it.
notify_discord() {
    local msg="$1"
    [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
    curl -sS --max-time 10 -H "Content-Type: application/json" \
        -d "$(printf '{"content":"%s"}' "$msg")" \
        "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

cleanup() {
    rm -rf "$TMP_WORK"
}

on_error() {
    local exit_code=$?
    notify_discord "🚨 **Verzue Redis backup FAILED** on \`$HOSTNAME_TAG\` at \`$TIMESTAMP\` (exit $exit_code) — check \`journalctl -u verzue-redis-backup\`"
    cleanup
    exit $exit_code
}

trap on_error ERR
trap cleanup EXIT

log() { echo "[$(date -Iseconds)] $*"; }

# --- 1. Trigger a fresh BGSAVE so dump.rdb is current ---------------------
log "Triggering BGSAVE..."
LAST_SAVE=$(redis-cli LASTSAVE)
redis-cli BGSAVE >/dev/null

# Wait for BGSAVE to finish (poll LASTSAVE)
NEW_SAVE=$LAST_SAVE
for _ in $(seq 1 60); do
    NEW_SAVE=$(redis-cli LASTSAVE)
    if [ "$NEW_SAVE" != "$LAST_SAVE" ]; then
        log "BGSAVE complete."
        break
    fi
    sleep 2
done

if [ "$NEW_SAVE" = "$LAST_SAVE" ]; then
    log "ERROR: BGSAVE did not complete within 120s. Aborting."
    exit 1
fi

# --- 2. Copy dump.rdb + AOF dir into staging ------------------------------
log "Copying persistence files..."
cp "$REDIS_DIR/dump.rdb" "$TMP_WORK/dump.rdb"
if [ -d "$REDIS_DIR/appendonlydir" ]; then
    cp -a "$REDIS_DIR/appendonlydir" "$TMP_WORK/appendonlydir"
fi

# Capture metadata for the post-mortem you'll write someday
redis-cli INFO     > "$TMP_WORK/redis-info.txt"   2>/dev/null || true
redis-cli DBSIZE   > "$TMP_WORK/dbsize.txt"       2>/dev/null || true
redis-cli INFO keyspace > "$TMP_WORK/keyspace.txt" 2>/dev/null || true
echo "$TIMESTAMP" > "$TMP_WORK/backup-timestamp.txt"

# --- 3. Tar+gzip into 15min tier ------------------------------------------
log "Creating archive: $ARCHIVE_NAME"
tar -czf "$DIR_15MIN/$ARCHIVE_NAME" -C "$TMP_WORK" .

ARCHIVE_SIZE=$(stat -c%s "$DIR_15MIN/$ARCHIVE_NAME")
log "Archive size: $(numfmt --to=iec $ARCHIVE_SIZE)"

# --- 4. Tier promotion (free via hard links) ------------------------------
# Promote to hourly tier on the :00 minute (the timer fires at :00, :15, :30, :45;
# only :00 promotes to hourly).
if [ "$MINUTE" = "00" ]; then
    log "Promoting to hourly tier (hard link)"
    ln "$DIR_15MIN/$ARCHIVE_NAME" "$DIR_HOURLY/$ARCHIVE_NAME"

    # Promote to daily tier at 02:00 UTC
    if [ "$HOUR" = "02" ]; then
        log "Promoting to daily tier (hard link)"
        ln "$DIR_15MIN/$ARCHIVE_NAME" "$DIR_DAILY/$ARCHIVE_NAME"
    fi
fi

# --- 5. Prune each tier independently -------------------------------------
prune_tier() {
    local dir="$1"
    local keep="$2"
    local removed
    removed=$(ls -1t "$dir"/verzue-redis-*.tar.gz 2>/dev/null | tail -n +$((keep + 1)) || true)
    if [ -n "$removed" ]; then
        echo "$removed" | xargs -r rm -f
        local count
        count=$(echo "$removed" | wc -l)
        log "Pruned $count old archives from $dir"
    fi
}

prune_tier "$DIR_15MIN"  "$RETAIN_15MIN"
prune_tier "$DIR_HOURLY" "$RETAIN_HOURLY"
prune_tier "$DIR_DAILY"  "$RETAIN_DAILY"

# --- 6. Disk space sanity check -------------------------------------------
# If /var/lib partition usage exceeds 85%, alert. NVMe is large so this
# should never fire from backups alone, but it's worth catching if logs
# or other data fill the disk.
USED_PCT=$(df --output=pcent /var/lib | tail -1 | tr -dc '0-9')
if [ "$USED_PCT" -gt 85 ]; then
    notify_discord "⚠️ **Verzue VPS disk at ${USED_PCT}%** — investigate before backups start failing"
fi

log "Backup complete. Total backup footprint: $(du -sh $BACKUP_ROOT | cut -f1)"
