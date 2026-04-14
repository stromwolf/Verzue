# 🏮 Verzue Bot: VPS Operations Guide

This guide contains the essential commands for managing your production bot on the Hostinger KVM2 VPS.

## 🚀 1. Fast Management (Service)
Use these commands to control the bot's life-cycle:

| Action | Command |
| :--- | :--- |
| **Restart Bot** | `systemctl restart verzue-bot` |
| **Stop Bot** | `systemctl stop verzue-bot` |
| **Start Bot** | `systemctl start verzue-bot` |
| **Check Status** | `systemctl status verzue-bot` |

---

## 📑 2. Monitoring Logs (The "Brain" Feed)
Use these to see what the bot is doing in real-time:

*   **Live Stream (Follow):**
    ```bash
    journalctl -u verzue-bot -f
    ```
*   **See Recent 100 Lines:**
    ```bash
    journalctl -u verzue-bot -n 100 --no-pager
    ```

---

## 🔄 3. Updating from GitHub
To bring new code from your laptop to the VPS:
1.  **On Laptop:** `git push` your changes.
2.  **On VPS (Run this one-liner):**
    ```bash
    cd /opt/verzue-bot && git pull origin main && systemctl restart verzue-bot && journalctl -u verzue-bot -f
    ```

---

## 📂 4. Structured Log Locations
With the new **Iron Mask** update, logs are organized by group:
*   **Request Logs:** `/opt/verzue-bot/logs/Requests/`
*   **Notification Logs:** `/opt/verzue-bot/logs/Notification/`

---

> [!TIP]
> **Iron Mask Protection**: The bot is designed to stay alive even if Redis drops. If you see `📡 [Redis] Connection lost` in the logs, **do not restart the bot**—it will reconnect autonomously.
