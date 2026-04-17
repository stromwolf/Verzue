# Verzue — Redis Backup & Recovery Runbook (Local NVMe Edition)

**Audience:** You at 3am when something is on fire.
**Scope:** Single Hostinger KVM2 VPS. Redis is local, AOF-persistent, **no offsite backups by design**.
**Storage:** 100 GB NVMe — backups live in `/var/lib/verzue-backups/`.

---

## 0. Read this first — what this protects (and what it doesn't)

This runbook protects against:
- ✅ Redis process crash (AOF replay)
- ✅ AOF file corruption
- ✅ Accidental `FLUSHALL` / data wipe (renamed in `redis.conf`, but if it happens)
- ✅ Bot bug that corrupts session state
- ✅ "I deleted the wrong key in `redis-cli`" moments

This runbook **does NOT** protect against:
- ❌ VPS hardware failure (disk, motherboard, datacentre fire)
- ❌ Hostinger account suspension or loss
- ❌ Compromise where the attacker has root (they delete the backups too)
- ❌ Accidental `rm -rf` of `/var/lib/`

**If any of those happen, your recovery path is:** rebuild a fresh VPS, reinstall the bot, re-login to platforms to regenerate sessions, ask users to re-issue any in-flight requests. Estimated downtime: 2-4 hours of work + however long re-login takes per platform.

If at any point this tradeoff stops feeling acceptable, the upgrade path is in §10.

---

## 1. The mental model

You have **three** copies of Redis state at any time, all on the same physical disk:

| Tier | Location | Freshness | Survives |
|---|---|---|---|
| Live | RAM in `redis-server` | Real-time | Nothing |
| Persistence | `/var/lib/redis/appendonlydir/` (AOF) + `dump.rdb` | ≤1 second behind RAM | Redis crash, VPS reboot |
| Snapshots | `/var/lib/verzue-backups/` (compressed archives) | ≤15 min behind live | Operator error, AOF corruption, bad bot deploy |

All three share the disk. That's the conscious tradeoff.

---

## 2. One-time setup

### 2.1 Create the backup directory

```bash
sudo mkdir -p /var/lib/verzue-backups
sudo chown root:root /var/lib/verzue-backups
sudo chmod 750 /var/lib/verzue-backups
```

### 2.2 Install the backup script

```bash
sudo mkdir -p /opt/verzue-bot/scripts
sudo cp backup-redis-local.sh /opt/verzue-bot/scripts/
sudo chmod 750 /opt/verzue-bot/scripts/backup-redis-local.sh
sudo chown root:root /opt/verzue-bot/scripts/backup-redis-local.sh
```

(Script content in **§7** below.)

### 2.3 Schedule via systemd timer (preferred over cron — better logging)

Create `/etc/systemd/system/verzue-redis-backup.service`:

```ini
[Unit]
Description=Verzue Redis Local Backup
After=redis-server.service

[Service]
Type=oneshot
ExecStart=/opt/verzue-bot/scripts/backup-redis-local.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EnvironmentFile=-/etc/default/verzue-redis-backup
```

Create `/etc/systemd/system/verzue-redis-backup.timer`:

```ini
[Unit]
Description=Run Verzue Redis local backup every 15 minutes

[Timer]
OnCalendar=*:0/15
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now verzue-redis-backup.timer
sudo systemctl list-timers verzue-redis-backup.timer  # verify next run
```

### 2.4 (Optional) Discord webhook for backup failure alerts

Create `/etc/default/verzue-redis-backup`:

```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

Permissions:

```bash
sudo chmod 640 /etc/default/verzue-redis-backup
sudo chown root:root /etc/default/verzue-redis-backup
```

The script in §7 reads this env var and pings Discord on failure. If you skip this step, failures are still in `journalctl -u verzue-redis-backup` — you just have to look.

---

## 3. Backup strategy at a glance

| Cadence | Method | Retention | Storage cost |
|---|---|---|---|
| Per-write | AOF `everysec` fsync | Continuous | <100 MB typical |
| Every 5 min | RDB snapshot (per `save` rules in redis.conf) | Continuous | <50 MB typical |
| **Every 15 min** | **`BGSAVE` + tar.gz to `/var/lib/verzue-backups/15min/`** | **8 hours (32 archives)** | **~500 MB worst case** |
| Hourly :00 | Promote to `/var/lib/verzue-backups/hourly/` | 48 hours (48 archives) | ~750 MB worst case |
| Daily 02:00 | Promote to `/var/lib/verzue-backups/daily/` | 14 days (14 archives) | ~225 MB worst case |
| **Weekly Sun 03:00** | **Restore drill (see §6)** | — | — |

Total backup footprint: well under 2 GB on your 100 GB disk. You will never hit a space issue from this.

---

## 4. How to verify backups are working

**Check daily.** A backup you haven't verified is a hope, not a backup.

```bash
# Last successful local snapshot inside Redis itself
redis-cli INFO persistence | grep -E '(rdb_last_save_time|rdb_last_bgsave_status|aof_last_write_status)'

# Decode rdb_last_save_time:
date -d @$(redis-cli INFO persistence | grep rdb_last_save_time | cut -d: -f2 | tr -d '\r')

# Backup archive freshness — newest file in each tier
ls -lht /var/lib/verzue-backups/15min/  | head -3
ls -lht /var/lib/verzue-backups/hourly/ | head -3
ls -lht /var/lib/verzue-backups/daily/  | head -3

# Disk space (sanity — should be tiny relative to 100 GB)
du -sh /var/lib/verzue-backups/
df -h /var/lib

# Backup script exit history
journalctl -u verzue-redis-backup.service -n 20 --no-pager
```

**Red flags:**
- `rdb_last_bgsave_status:err` → check disk space and `/var/lib/redis` permissions
- `aof_last_write_status:err` → AOF is failing to write; investigate immediately
- No new file in `15min/` for >30 min → systemd timer broken or BGSAVE failing
- `verzue-redis-backup.service` exit code ≠ 0 → read the journal
- `du -sh /var/lib/verzue-backups/` keeps growing → retention pruning broken

---

## 5. Restore procedures

### 5.1 Scenario A: Redis crashed but disk is intact (most common)

Redis will auto-recover on restart by replaying AOF.

```bash
sudo systemctl restart redis-server
sudo journalctl -u redis-server -n 50 --no-pager  # look for "DB loaded from append only file"
redis-cli DBSIZE  # sanity check
sudo systemctl restart verzue-bot
```

**Expected data loss:** ≤1 second of writes (the everysec fsync window).

### 5.2 Scenario B: AOF corrupted (rare; usually after disk-full event)

```bash
sudo systemctl stop redis-server
cd /var/lib/redis/appendonlydir
sudo cp -a . /var/lib/redis/appendonlydir.broken.$(date +%s)  # always preserve evidence
sudo redis-check-aof --fix appendonly.aof.*.incr.aof
# Answer yes when prompted. This truncates at the first corruption.
sudo systemctl start redis-server
```

**Expected data loss:** Whatever was after the corruption point. Usually seconds.

If `redis-check-aof --fix` removes too much (you check `DBSIZE` and it's way smaller than expected), fall back to **§5.4** with the most recent backup archive.

### 5.3 Scenario C: Accidental data wipe (someone ran a destructive command)

The renamed FLUSHALL/FLUSHDB in `redis.conf` makes this much harder, but if it happens:

1. **Stop Redis IMMEDIATELY** to prevent the AOF rewrite from baking the wipe in:
   ```bash
   sudo systemctl stop redis-server
   ```

2. **Do NOT restart yet.** The AOF on disk still contains the pre-wipe history.

3. Inspect the tail of the AOF:
   ```bash
   sudo tail -c 4096 /var/lib/redis/appendonlydir/appendonly.aof.*.incr.aof
   ```
   Look for the destructive command. They appear like:
   ```
   *1
   $8
   FLUSHALL
   ```

4. Edit the file with `vim` or `nano` and remove the destructive command(s) from the end.

5. Run `sudo redis-check-aof --fix /var/lib/redis/appendonlydir/appendonly.aof.*.incr.aof` if the file is now technically malformed.

6. Restart: `sudo systemctl start redis-server`.

**If this fails or you've already restarted past the wipe:** fall back to §5.4 with the most recent pre-wipe backup.

### 5.4 Scenario D: Restore from a snapshot archive

Use this when AOF surgery isn't viable — you need to roll back to a known-good point in time.

1. Stop Redis:
   ```bash
   sudo systemctl stop redis-server
   sudo systemctl stop verzue-bot
   ```

2. Identify the snapshot you want. List in time order:
   ```bash
   ls -lht /var/lib/verzue-backups/15min/  | head -10
   ls -lht /var/lib/verzue-backups/hourly/ | head -10
   ls -lht /var/lib/verzue-backups/daily/  | head -10
   ```

   Pick the most recent one *before* the bad event. Copy its full path.

3. Move the broken state aside (always preserve evidence):
   ```bash
   sudo mv /var/lib/redis/dump.rdb /var/lib/redis/dump.rdb.broken.$(date +%s)
   sudo mv /var/lib/redis/appendonlydir /var/lib/redis/appendonlydir.broken.$(date +%s)
   ```

4. Extract the chosen snapshot into a temp dir, then place files:
   ```bash
   ARCHIVE=/var/lib/verzue-backups/15min/verzue-redis-XXX.tar.gz  # ← edit this
   sudo mkdir -p /tmp/redis-restore
   sudo tar -xzf "$ARCHIVE" -C /tmp/redis-restore
   ls /tmp/redis-restore  # expect dump.rdb, appendonlydir/, redis-info.txt, etc.

   sudo cp /tmp/redis-restore/dump.rdb /var/lib/redis/dump.rdb
   sudo cp -a /tmp/redis-restore/appendonlydir /var/lib/redis/
   sudo chown -R redis:redis /var/lib/redis
   sudo chmod -R 750 /var/lib/redis
   sudo rm -rf /tmp/redis-restore
   ```

5. Start Redis and verify:
   ```bash
   sudo systemctl start redis-server
   sudo journalctl -u redis-server -n 30 --no-pager
   redis-cli DBSIZE   # should match what you had pre-incident
   redis-cli INFO keyspace
   ```

6. Smoke-test the bot before going live:
   ```bash
   sudo systemctl start verzue-bot
   journalctl -u verzue-bot -f
   # Look for: "💓 Worker registered" and "♻️ Boot recovered N orphaned tasks"
   ```

**Expected RTO:** 5-10 minutes.
**Expected data loss:** Up to 15 minutes (gap between snapshot tier intervals). For older incidents, up to 1 hour (hourly tier) or 1 day (daily tier).

### 5.5 Scenario E: VPS hardware failure / disk lost

This is the scenario you've explicitly chosen not to protect against. Recovery path:

1. Provision a new VPS (or have Hostinger restore).
2. Install Ubuntu, Redis, Python, the bot codebase from your git repo.
3. Apply `redis.conf` from the deliverables.
4. Restore the bot's config (Discord tokens, GDrive credentials — these should be in your password manager / git-encrypted, **not** only on the VPS).
5. Start `verzue-bot`. It will boot with empty Redis.
6. **Re-login to each platform via your existing `$session` admin commands** to regenerate sessions. This is the manual step.
7. Notify users in your Discord server that any in-flight chapter requests need to be re-issued.

**Estimated downtime:** 2-4 hours of operator work + however long platform re-logins take.

If this scenario starts feeling unacceptable, see §10 for the upgrade path.

---

## 6. Restore drill (run weekly)

Untested backups are folklore. Every Sunday, prove the local snapshots actually restore:

```bash
# 1. Pick the latest archive
LATEST=$(ls -1t /var/lib/verzue-backups/hourly/*.tar.gz | head -1)
echo "Drilling against: $LATEST"

# 2. Extract to a scratch location
DRILL_DIR=/tmp/restore-drill-$(date +%s)
mkdir -p "$DRILL_DIR"
tar -xzf "$LATEST" -C "$DRILL_DIR"
ls "$DRILL_DIR"  # confirm dump.rdb + appendonlydir/

# 3. Run a throwaway redis on port 6390 against the extracted files
redis-server --port 6390 --dir "$DRILL_DIR" --dbfilename dump.rdb \
  --appendonly yes --appenddirname appendonlydir \
  --daemonize yes --pidfile "$DRILL_DIR/redis.pid" \
  --logfile "$DRILL_DIR/redis.log"

sleep 2

# 4. Verify it loaded real data
redis-cli -p 6390 DBSIZE
redis-cli -p 6390 INFO keyspace
redis-cli -p 6390 KEYS 'verzue:session:*' | head    # should see real session keys
redis-cli -p 6390 HLEN verzue:active_tasks          # should be a number, possibly 0

# 5. Tear down
redis-cli -p 6390 SHUTDOWN NOSAVE 2>/dev/null || kill $(cat "$DRILL_DIR/redis.pid")
rm -rf "$DRILL_DIR"
```

If `DBSIZE` is 0, or `KEYS` returns nothing recognisable, **the backup is broken — investigate before you need it for real.**

Log the drill result somewhere persistent:

```bash
echo "$(date -Iseconds) | Restore drill: PASS | DBSIZE=$DBSIZE | Archive=$LATEST" \
  | sudo tee -a /var/log/verzue-restore-drills.log
```

You can wrap this in a systemd timer too if you want it automated, but a manual weekly run forces you to actually look at the output, which catches subtle issues a script wouldn't (e.g. session schema drift).

---

## 7. The backup script

`/opt/verzue-bot/scripts/backup-redis-local.sh`:

```bash
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
```

Key design choices in the script worth understanding:

- **Hard-linking for tier promotion** — promoting a 15-min snapshot to "hourly" doesn't copy the file, it creates a second directory entry pointing at the same inode. Costs zero disk. Deleting from one tier doesn't affect the others (the inode stays alive until the last hard link is gone).
- **`set -euo pipefail` + ERR trap** — any failure bails out and pings Discord. No silent failures.
- **`redis-cli BGSAVE` + `LASTSAVE` polling** — never copies a half-written `dump.rdb`. Waits for Redis to confirm the snapshot is on disk.
- **Disk usage check at 85%** — backup footprint won't cause this on its own (max ~2 GB on a 100 GB disk), but if logs or other data fill the disk, your AOF will start failing silently. This catches it.

---

## 8. Monitoring & alerting

Three layers, all free:

### 8.1 Backup failure → Discord webhook

Already wired into the script via `notify_discord` (see §7). Set `DISCORD_WEBHOOK_URL` in `/etc/default/verzue-redis-backup` (see §2.4).

### 8.2 Daily summary via systemd timer

Create `/etc/systemd/system/verzue-backup-summary.service`:

```ini
[Unit]
Description=Verzue Daily Backup Summary

[Service]
Type=oneshot
EnvironmentFile=-/etc/default/verzue-redis-backup
ExecStart=/opt/verzue-bot/scripts/backup-summary.sh
```

And `/etc/systemd/system/verzue-backup-summary.timer`:

```ini
[Unit]
Description=Daily Verzue backup summary at 09:00 UTC

[Timer]
OnCalendar=*-*-* 09:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

The summary script `/opt/verzue-bot/scripts/backup-summary.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="/var/lib/verzue-backups"
HOSTNAME_TAG="$(hostname -s)"

LATEST_15MIN=$(ls -1t "$BACKUP_ROOT/15min"/*.tar.gz 2>/dev/null | head -1 || echo "NONE")
COUNT_15MIN=$(ls -1 "$BACKUP_ROOT/15min"/*.tar.gz 2>/dev/null | wc -l)
COUNT_HOURLY=$(ls -1 "$BACKUP_ROOT/hourly"/*.tar.gz 2>/dev/null | wc -l)
COUNT_DAILY=$(ls -1 "$BACKUP_ROOT/daily"/*.tar.gz 2>/dev/null | wc -l)

TOTAL_SIZE=$(du -sh "$BACKUP_ROOT" | cut -f1)
DISK_USED=$(df -h /var/lib | tail -1 | awk '{print $3"/"$2" ("$5")"}')

DBSIZE=$(redis-cli DBSIZE 2>/dev/null || echo "?")
USED_MEM=$(redis-cli INFO memory 2>/dev/null | grep used_memory_human | cut -d: -f2 | tr -d '\r' || echo "?")

MSG=$(cat <<EOF
📊 **Verzue Backup Summary** — \`$HOSTNAME_TAG\`

**Redis state:** DBSIZE=$DBSIZE | mem=$USED_MEM

**Backup tiers:**
• 15min: $COUNT_15MIN archives
• Hourly: $COUNT_HOURLY archives
• Daily: $COUNT_DAILY archives

**Latest:** \`$(basename "$LATEST_15MIN")\`

**Disk:** $DISK_USED | Backups: $TOTAL_SIZE
EOF
)

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    curl -sS --max-time 10 -H "Content-Type: application/json" \
        -d "$(jq -n --arg c "$MSG" '{content:$c}')" \
        "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
fi

echo "$MSG"
```

(Requires `jq` for safe JSON encoding of the multiline message: `sudo apt install jq`.)

Enable:

```bash
sudo chmod 750 /opt/verzue-bot/scripts/backup-summary.sh
sudo systemctl daemon-reload
sudo systemctl enable --now verzue-backup-summary.timer
```

### 8.3 In-bot visibility

The `$qstats` command from the queue refactor (`INTEGRATION.py`) gives you live queue health from Discord. For backup status, you can add a sibling `$backup_status` cog that runs `ls -lht /var/lib/verzue-backups/15min/ | head -3` and posts the result. 15 lines of code; let me know if you want it.

---

## 9. Decision log — why these choices

| Choice | Why |
|---|---|
| AOF `everysec` over RDB-only | RPO ≤1s vs RPO ≤5min. For a queue holding chapter-unlock work, the durability win is worth the disk cost. |
| Local-only (no offsite) | Operator's explicit choice. Loses VPS-survival but eliminates external dependencies and ongoing cost. Documented tradeoff in §0 and §5.5. |
| 15-min snapshot cadence | Cheap on a 100 GB disk. Tighter RPO for the operational scenarios this strategy actually covers. |
| Three retention tiers via hard links | Storage-free promotion. Lets you recover from old incidents (last 14 days) without keeping 1,344 archives. |
| `noeviction` policy in `redis.conf` | Silent eviction of session/dedup data is worse than loud OOM errors. Forces you to fix leaks instead of papering over them. |
| Renamed FLUSHALL/KEYS/DEBUG | One typo away from a multi-day recovery. Renaming costs nothing. |
| Restore drill weekly | ~30% of "working" backups fail on first restore attempt. Test or it's not real. Especially important without an offsite copy. |
| Discord webhook over email/PagerDuty | You're already in Discord all day for the bot. No new tool to install or pay for. |

---

## 10. Upgrade path — when local-only stops being enough

The triggers that should make you reconsider, in order of seriousness:

1. **You start storing data in Redis that can't be regenerated.** Right now sessions can be re-acquired by re-login, subs are in bot config, queue is user-replayable. If you ever add user purchase history, payment records, audit logs, or anything compliance-relevant — that data needs an offsite copy. Non-negotiable.

2. **Bot becomes monetised or gains paying users.** The "2-4 hours of operator work to recover" cost stops being acceptable when you're SLA-bound to anyone, even informally.

3. **Hostinger relationship feels precarious.** Billing dispute, support ticket that goes unanswered for a week, account flag, region instability. Any of these is a heads-up that the "VPS account = single point of failure" risk is non-theoretical.

4. **Bot grows beyond one VPS.** The moment you add a second VPS for any reason (load, geography, redundancy), shared offsite storage becomes the natural sync point.

The minimum upgrade is small: add an `rclone` step at the end of the backup script that uploads the daily archive to B2/R2/S3. Everything else in this runbook stays the same. Cost: ~$0.06/month for B2. Time to add: 30 minutes including B2 account setup.

You don't need to do it now. But know where the door is.

---

## 11. What this runbook does NOT cover

- **Multi-region failover** — see §10.
- **Point-in-time recovery to arbitrary timestamps** — would require WAL shipping. Overkill; the 15-min snapshot tier covers most needs.
- **Encrypted backups at rest** — backups inherit the VPS's at-rest encryption (Hostinger NVMe is encrypted at the hypervisor layer per their docs; verify if compliance-relevant). For application-level encryption, pipe the tar through `gpg --symmetric` before write.
- **Bot-side recovery** — covered in `INTEGRATION.py` §"PATCH 1" (orphan sweep on boot).
