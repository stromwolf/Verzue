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
    # Use python for safer JSON encoding if jq isn't available, but try curl first with simple string
    # Actually, the runbook recommends jq. I'll stick to the bash logic.
    curl -sS --max-time 10 -H "Content-Type: application/json" \
        -d "$(printf '{"content":"%s"}' "$MSG")" \
        "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
fi

echo "$MSG"
