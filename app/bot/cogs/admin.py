import discord
from discord import app_commands
from discord.ext import commands
import sys
import os
import sys
import os
import signal
import asyncio
import logging
from pathlib import Path

from config.settings import Settings
from app.services.login_service import LoginService

logger = logging.getLogger("AdminCog")
PID_FILE = Path("bot.pid")

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.login_service = LoginService()

    async def cog_check(self, ctx):
        """
        Security Gatekeeper: Only allows users in ALLOWED_IDS 
        to run these commands.
        """
        if not Settings.ALLOWED_IDS:
            return True # Open access if no IDs are set (Dev Mode)
            
        return ctx.author.id in Settings.ALLOWED_IDS or ctx.author.id == 1216284053049704600


    async def _wait_for_drain(self, ctx, action_name: str):
        """Helper to wait for active tasks to finish before an action."""
        if not hasattr(self.bot, 'task_queue'):
            return True
        
        tq = self.bot.task_queue
        if tq.busy_workers == 0:
            return True
            
        tq.is_draining = True
        busy = tq.busy_workers
        msg = await ctx.send(f"⚠️ **Graceful {action_name.capitalize()} Initiated**\nFound **{busy}** active chapter(s). Finishing them before {action_name}...\n-# (No new tasks will be accepted during this time)")
        
        # Max wait: 120 seconds
        for _ in range(24): 
            await asyncio.sleep(5)
            if tq.busy_workers == 0:
                await msg.edit(content=f"✅ Workers finished. Proceeding with **{action_name}**...")
                return True
            await msg.edit(content=f"⏳ Still finishing **{tq.busy_workers}** chapter(s)... ({_}/24)")
            
        await msg.edit(content=f"⚠️ Timeout waiting for workers. Proceeding with **{action_name}** anyway for stability.")
        return True

    @commands.group(name="admin_ops", invoke_without_command=True)
    async def admin_ops(self, ctx):
        """Root for admin-only operations."""
        pass

    @commands.command(name="sync")
    @commands.is_owner() # Restriction: Owner only. Admins in joined guilds cannot reboot production.
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands. [Owner/Admin Only]"""
        # 🟢 S-GRADE: Graceful Check
        await self._wait_for_drain(ctx, "sync")

        msg = await ctx.send("🔄 Syncing slash commands...")
        try:
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced **{len(synced)}** slash commands globally.")
        except Exception:
            await msg.edit(content=f"Come <@1216284053049704600>. New Error")
        finally:
            if hasattr(self.bot, 'task_queue'):
                self.bot.task_queue.is_draining = False

    @commands.command(name="restart", aliases=["reboot", "reset"])
    @commands.is_owner() # Restriction: Owner only. Admins in joined guilds cannot reboot production.
    async def restart_bot(self, ctx, target: str | None = None):
        """Usage: $restart [Main|Testing]. Reboots the bot instance. [Owner/Admin Only]"""

        # 🟢 S-GRADE: Identity Check
        # Identify instance identity based on the token in use
        if self.bot.token_str == Settings.TESTING_BOT_TOKEN or self.bot.token_str == Settings.STAGING_TOKEN:
            my_identity = "Testing"
        elif self.bot.token_str == Settings.ADMIN_BOT_TOKEN:
            my_identity = "Admin"
        else:
            my_identity = "Main"

        if target:
            if target.lower() != my_identity.lower():
                # If a target was specified and it's not me, stay silent or do nothing
                # This prevents both bots from restarting if they are in the same channel.
                return

        # 🟢 S-GRADE: Graceful Check
        await self._wait_for_drain(ctx, "restart")

        await ctx.send(f"🔄 **Initiating {my_identity} System Reboot...**")
        logger.info(f"Reboot: {my_identity} Process initiated via $restart by {ctx.author}")

        import sys
        import subprocess

        # 1. SHUT DOWN BROWSER
        try:
            # Gracefully stop the browser if possible
            if hasattr(self.bot, 'task_queue'):
                browser = self.bot.task_queue.browser_service
                if browser and hasattr(browser, 'stop'):
                    await browser.stop()
                    
            # Kill poller
            if hasattr(self.bot, 'auto_poller'):
                try:
                    self.bot.auto_poller.poll_loop.cancel()
                except Exception:
                    pass
            
            # 2. KILL ANY PREVIOUSLY SAVED PID
            from pathlib import Path
            pid_file = Path("bot.pid")
            pid_file.unlink(missing_ok=True)

            # 3. EXECUTE RESTART
            subprocess.Popen([sys.executable] + sys.argv)
            
            # Disconnect
            await self.bot.close()
            # 🟢 Use os._exit to kill the process immediately without 
            # triggering discord.py's task exception logger.
            import os
            os._exit(0)
            
        except Exception:
            logger.error(f"Restart Failed")
            await ctx.send(f"Come <@1216284053049704600>. New Error")

    @commands.command(name="ping")
    async def ping(self, ctx):
        """Usage: $ping. Checks bot latency and service health."""
        latency = round(self.bot.latency * 1000)
        
        # Verify Redis
        from app.services.redis_manager import RedisManager
        is_redis = await RedisManager().check_connection()
        
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"**Latency:** `{latency}ms`\n**Status:** `Operational`",
            color=0x2ecc71
        )
        embed.add_field(name="Global Brain", value="✅ Connected" if is_redis else "❌ Disconnected", inline=True)
        await ctx.send(embed=embed)



    @commands.command(name="test")
    async def test_metadata(self, ctx, url: str):
        """Usage: $test <URL>. Verifies metadata extraction and schedule."""
        msg = await ctx.send(f"🔍 **Testing Metadata Extraction:**\n`{url}`")
        
        try:
            # 1. Get Provider
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            if not scraper:
                return await msg.edit(content=f"❌ **Error:** No provider found for this URL.")
            
            # 2. Fetch Info
            data = await scraper.get_series_info(url)
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
            
            # 3. Format Response
            latest = chapter_list[-1] if chapter_list else {"notation": "N/A", "id": "N/A"}
            
            embed = discord.Embed(
                title="🧪 Metadata Test Result",
                description=f"**Title:** {title}\n**ID:** `{series_id}`",
                color=0x3498db
            )
            embed.add_field(name="Chapters", value=f"`{total_chapters}` total\nLatest: `{latest.get('notation')}`", inline=True)
            schedule_str = release_day or 'Not Detected'
            if release_time:
                schedule_str += f" @ {release_time} UTC"
            embed.add_field(name="Release Schedule", value=f"📅 `{schedule_str}`", inline=True)
            
            if image_url:
                embed.set_thumbnail(url=image_url)
            
            await msg.edit(content=None, embed=embed)
            
        except Exception:
            import traceback
            logger.error(f"Metadata Test Failed\n{traceback.format_exc()}")
            await msg.edit(content=f"Come <@1216284053049704600>. New Error")

    @commands.group(name="clear_platform", invoke_without_command=True)
    @commands.check_any(commands.is_owner(), commands.has_permissions(administrator=True))
    async def clear_platform(self, ctx, platform: str):
        """Usage: $clear_platform <platform>. Purges all Redis sessions for a platform."""
        try:
            msg = await ctx.send(f"🧹 **Cleaning sessions for: {platform.capitalize()}...**")
            
            from app.services.redis_manager import RedisManager
            redis_manager = RedisManager()
            platform_key = platform.lower()
            account_ids = await redis_manager.list_sessions(platform_key)
            
            if not account_ids:
                return await msg.edit(content=f"ℹ️ No active sessions found for **{platform}**.")
            
            count = 0
            for aid in account_ids:
                await redis_manager.delete_session(platform_key, aid)
                count += 1
            
            await msg.edit(content=f"✅ Successfully purged **{count}** sessions for `{platform}`. Bot will now require fresh cookies.")
            logger.info(f"🧹 [ADMIN] Sessions cleared for {platform} by {ctx.author}")
        except Exception:
            logger.error(f"Clear Platform Failed")
            await ctx.send(f"Come <@1216284053049704600>. New Error")

    # ==========================================
    # 📊 QUEUE & BACKUP DIAGNOSTICS
    # ==========================================

    @commands.command(name="qstats")
    @commands.is_owner()
    async def queue_stats(self, ctx):
        """Show current Redis queue depths and worker levels."""
        depths = await self.bot.task_queue.redis.queue.queue_depths()
        embed = discord.Embed(title="📊 Queue Depths", color=0x3498db)
        embed.add_field(name="Global (pending)", value=f"`{depths.get('global', 0)}`")
        embed.add_field(name="Dead Letter", value=f"`{depths.get('dead_letter', 0)}`")
        
        proc = depths.get("processing_by_worker", {})
        if proc:
            val = "\n".join(f"`{w.split(':')[-1]}`: {n}" for w, n in proc.items())
            embed.add_field(name="In-flight by worker (PID)", value=val, inline=False)
        else:
            embed.add_field(name="In-flight", value="`0`", inline=False)
            
        await ctx.send(embed=embed)

    @commands.command(name="dlq_replay")
    @commands.is_owner()
    async def replay_dead_letter(self, ctx, max_count: int = 100):
        """Replay failed tasks back into the global queue."""
        n = await self.bot.task_queue.redis.queue.replay_dead_letter(max_count=max_count)
        await ctx.send(f"♻️ Replayed **{n}** tasks from dead-letter back to global queue.")

    @commands.command(name="backup_status")
    @commands.is_owner()
    async def backup_status(self, ctx):
        """Usage: $backup_status. Checks the freshness of local snapshots."""
        import os
        from datetime import datetime
        
        backup_path = "/var/lib/verzue-backups/15min/"
        # 🟢 Cross-platform adaptation for Windows testing
        if os.name == 'nt':
            # Check if we have a local dev folder or just use a dummy for testing
            if not os.path.exists(backup_path):
                backup_path = "data/backups/15min/" # Potential local dev path
        
        try:
            if not os.path.exists(backup_path):
                return await ctx.send(f"❌ **Backup path not found**: `{backup_path}`\n-# (This usually means backups aren't configured yet on this host)")

            # Get files sorted by modified time
            files = [f for f in os.listdir(backup_path) if f.endswith('.tar.gz')]
            files.sort(key=lambda x: os.path.getmtime(os.path.join(backup_path, x)), reverse=True)
            
            # Calculate total directory size
            import math
            def convert_size(size_bytes):
                if size_bytes == 0: return "0B"
                size_name = ("B", "KB", "MB", "GB", "TB")
                i = int(math.floor(math.log(size_bytes, 1024)))
                p = math.pow(1024, i)
                s = round(size_bytes / p, 2)
                return f"{s} {size_name[i]}"

            root_path = os.path.dirname(os.path.dirname(backup_path))
            total_size = 0
            for dirpath, dirnames, filenames in os.walk(root_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    total_size += os.path.getsize(fp)

            embed = discord.Embed(title="💾 Local Backup Status", color=0x9b59b6)
            embed.add_field(name="Storage Usage", value=f"`{convert_size(total_size)}`", inline=True)
            
            if files:
                recent = files[:3]
                val = ""
                for f in recent:
                    mtime = os.path.getmtime(os.path.join(backup_path, f))
                    dt = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
                    val += f"• `{f}`\n  -# ({dt})\n"
                embed.add_field(name="Recent 15-min Snapshots", value=val, inline=False)
            else:
                embed.add_field(name="Alert", value=f"❌ No snapshots found in `{backup_path}`", inline=False)
                
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"⚠️ **Error checking backups**: `{e}`")

    @commands.command(name="re-schedule")
    @commands.check_any(commands.is_owner(), commands.has_permissions(administrator=True))
    async def reschedule(self, ctx, platform: str = None):
        """
        Usage:
          $re-schedule              — re-check ALL subscriptions across all groups
          $re-schedule jumptoon     — only Jumptoon subs
          $re-schedule piccoma      — only Piccoma subs
          $re-schedule mecha        — only Mecha subs

        Fetches fresh metadata for every unscheduled (or all) subscription
        and writes the detected UTC release day back to disk + Redis.
        """
        from app.services.group_manager import (
            load_group, save_group, _group_filename
        )
        from config.settings import Settings

        # ── Normalize platform filter ──────────────────────────────────────────
        platform_filter = platform.lower().strip() if platform else None
        VALID_PLATFORMS = {"jumptoon", "piccoma", "mecha"}
        if platform_filter and platform_filter not in VALID_PLATFORMS:
            return await ctx.send(
                f"❌ Unknown platform `{platform_filter}`. "
                f"Valid options: `jumptoon`, `piccoma`, `mecha` (or omit for all)."
            )

        # ── Discover all group JSON files ──────────────────────────────────────
        if not Settings.GROUPS_DIR.exists():
            return await ctx.send("❌ Groups directory not found.")

        all_groups = [p.stem.replace("_", " ") for p in Settings.GROUPS_DIR.glob("*.json")]
        if not all_groups:
            return await ctx.send("ℹ️ No group profiles found.")

        label = f"`{platform_filter}`" if platform_filter else "**all platforms**"
        status_msg = await ctx.send(
            f"🔄 **Re-Schedule started** for {label}...\n"
            f"-# Scanning {len(all_groups)} group(s). This may take a while."
        )

        # ── Counters ───────────────────────────────────────────────────────────
        total      = 0   # subs inspected
        updated    = 0   # subs that got a day written
        skipped    = 0   # already had a day (not re-checked to save API calls)
        failed     = 0   # scraper raised an exception
        no_day     = 0   # scraper returned None for release_day

        results_log = []  # (title, old_day, new_day, status_emoji)

        for group_name in all_groups:
            data = load_group(group_name)
            subs = data.get("subscriptions", [])
            changed = False

            for sub in subs:
                url   = sub.get("series_url", "")
                title = sub.get("series_title", sub.get("series_id", "Unknown"))

                # ── Platform filter ────────────────────────────────────────────
                sub_platform = sub.get("platform", "").lower()
                if not sub_platform:
                    # Infer from URL as fallback
                    if   "jumptoon" in url.lower(): sub_platform = "jumptoon"
                    elif "piccoma"  in url.lower(): sub_platform = "piccoma"
                    elif "mecha"    in url.lower(): sub_platform = "mecha"

                if platform_filter and sub_platform != platform_filter:
                    continue

                total += 1
                old_day = sub.get("release_day")

                # ── Skip already-scheduled subs ────────────────────────────────
                # Remove this block if you want a full force-refresh for everyone
                if old_day:
                    skipped += 1
                    continue

                # ── Fetch fresh metadata ───────────────────────────────────────
                try:
                    # Primary: route by URL
                    scraper = None
                    if url:
                        try:
                            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
                        except Exception:
                            pass  # fall through to platform fallback

                    # Fallback: route by platform field (handles empty/malformed URLs)
                    if not scraper and sub_platform:
                        try:
                            scraper = self.bot.task_queue.provider_manager.get_provider(sub_platform)
                        except Exception:
                            pass

                    if not scraper:
                        failed += 1
                        results_log.append((title, old_day, None, "❓"))
                        logger.warning(f"[ReSchedule] No scraper for '{title}' (url={url!r}, platform={sub_platform!r})")
                        continue

                    if not url:
                        failed += 1
                        results_log.append((title, old_day, None, "❓"))
                        logger.warning(f"[ReSchedule] Empty URL for '{title}', cannot fetch metadata")
                        continue

                    scrape_data = await scraper.get_series_info(url, fast=True)
                    _, _, _, _, _, release_day, release_time, _, _ = scrape_data

                    if release_day:
                        sub["release_day"]  = release_day
                        sub["release_time"] = release_time or "15:00"
                        changed = True
                        updated += 1
                        results_log.append((title, old_day, release_day, "✅"))
                        logger.info(
                            f"[ReSchedule] {group_name} | {title}: "
                            f"{old_day or 'None'} → {release_day}"
                        )
                    else:
                        no_day += 1
                        results_log.append((title, old_day, None, "⚠️"))
                        logger.warning(f"[ReSchedule] No day detected for {title} ({url})")

                except Exception as e:
                    failed += 1
                    results_log.append((title, old_day, None, "❌"))
                    logger.error(f"[ReSchedule] Failed for {title}: {e}")

                # ── Small delay to avoid rate-hammering the providers ──────────
                await asyncio.sleep(1.5)

            # ── Persist if anything changed ────────────────────────────────────
            if changed:
                save_group(group_name, data)

        # ── Build summary embed ────────────────────────────────────────────────
        embed = discord.Embed(
            title="📅 Re-Schedule Complete",
            color=0x2ecc71 if not failed else 0xe67e22
        )
        embed.add_field(name="Platform",  value=label,           inline=True)
        embed.add_field(name="Inspected", value=str(total),      inline=True)
        embed.add_field(name="Skipped (had day)", value=str(skipped), inline=True)
        embed.add_field(name="✅ Updated",  value=str(updated),  inline=True)
        embed.add_field(name="⚠️ No Day Found", value=str(no_day), inline=True)
        embed.add_field(name="❌ Errors",   value=str(failed),   inline=True)

        # ── Detailed log (only changed/failed rows, max 15 to stay under embed limit) ──
        if results_log:
            log_lines = []
            for (t, old, new, emoji) in results_log[:15]:
                old_str = old or "Hiatus"
                new_str = new or "—"
                log_lines.append(f"{emoji} **{t}**\n`{old_str}` → `{new_str}`")
            if len(results_log) > 15:
                log_lines.append(f"*...and {len(results_log) - 15} more (check logs)*")
            embed.add_field(
                name="Details",
                value="\n".join(log_lines),
                inline=False
            )

        await status_msg.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))