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

(Script logic documented in §7 of the technical guide.)

---

## 8. Monitoring & alerting

Three layers, all free:

### 8.1 Backup failure → Discord webhook

Already wired into the script via `notify_discord` (see §7). Set `DISCORD_WEBHOOK_URL` in `/etc/default/verzue-redis-backup` (see §2.4).

### 8.2 Daily summary via systemd timer

Create `/etc/systemd/system/verzue-backup-summary.service` and timer (see §8.2 of guide).

### 8.3 In-bot visibility

The `$qstats` command from the queue refactor gives you live queue health from Discord. For backup status, use the `$backup_status` admin command.

---

## 9. Decision log — why these choices

- **AOF `everysec` over RDB-only** — RPO ≤1s vs RPO ≤5min. Durability win is worth the disk cost.
- **Local-only (no offsite)** — Operator's explicit choice. Loses VPS-survival but eliminates external dependencies.
- **15-min snapshot cadence** — Cheap on a 100 GB disk. Tighter RPO for operational scenarios.
- **Three retention tiers via hard links** — Storage-free promotion. Lets you recover from old incidents (14 days).

---

## 10. Upgrade path — when local-only stops being enough

1. **Non-regeneratable data**: User purchase history, audit logs.
2. **Monetisation**: Paying users require an SLA.
3. **VPS Instability**: If you lose trust in the VPS host.
4. **Scale**: Moving beyond one VPS.

Upgrade: add `rclone` to B2/R2/S3. Costs ~$0.06/month.

---

## 11. What this runbook does NOT cover

- Multi-region failover.
- Point-in-time recovery (WAL shipping).
- Encrypted backups at rest (assumes VPS hypervisor encryption).
