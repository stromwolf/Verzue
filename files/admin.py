import discord
from discord import app_commands
from discord.ext import commands
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
            return True  # Open access if no IDs are set (Dev Mode)

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
        msg = await ctx.send(
            f"⚠️ **Graceful {action_name.capitalize()} Initiated**\n"
            f"Found **{busy}** active chapter(s). Finishing them before {action_name}...\n"
            f"-# (No new tasks will be accepted during this time)"
        )

        for _ in range(24):
            await asyncio.sleep(5)
            if tq.busy_workers == 0:
                await msg.edit(content=f"✅ Workers finished. Proceeding with **{action_name}**...")
                return True
            await msg.edit(content=f"⏳ Still finishing **{tq.busy_workers}** chapter(s)... ({_}/24)")

        await msg.edit(
            content=f"⚠️ Timeout waiting for workers. Proceeding with **{action_name}** anyway for stability."
        )
        return True

    @commands.group(name="admin_ops", invoke_without_command=True)
    async def admin_ops(self, ctx):
        """Root for admin-only operations."""
        pass

    @commands.command(name="sync")
    @commands.is_owner()  # PHASE 0: was check_any(is_owner, has_permissions(administrator)) — server admins must not sync global commands
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands. [Owner Only]"""
        await self._wait_for_drain(ctx, "sync")

        msg = await ctx.send("🔄 Syncing slash commands...")
        try:
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced **{len(synced)}** slash commands globally.")
        except Exception as e:
            logger.error(f"Sync failed: {e}")
            await msg.edit(content=f"Come <@1216284053049704600>. New Error")
        finally:
            if hasattr(self.bot, 'task_queue'):
                self.bot.task_queue.is_draining = False

    @commands.command(name="restart", aliases=["reboot", "reset"])
    @commands.is_owner()  # PHASE 0: removed has_permissions(administrator=True) — any server admin could reboot production infra
    async def restart_bot(self, ctx, target: str | None = None):
        """Usage: $restart [Main|Testing]. Reboots the bot instance. [Owner Only]"""

        # Identity check
        if self.bot.token_str == Settings.TESTING_BOT_TOKEN or self.bot.token_str == getattr(Settings, 'STAGING_TOKEN', None):
            my_identity = "Testing"
        elif self.bot.token_str == Settings.ADMIN_BOT_TOKEN:
            my_identity = "Admin"
        else:
            my_identity = "Main"

        if target and target.lower() != my_identity.lower():
            return

        await self._wait_for_drain(ctx, "restart")

        await ctx.send(f"🔄 **Initiating {my_identity} System Reboot...**")
        logger.info(f"Reboot: {my_identity} initiated via $restart by {ctx.author}")

        import subprocess

        try:
            if hasattr(self.bot, 'task_queue'):
                browser = self.bot.task_queue.browser_service
                if browser and hasattr(browser, 'stop'):
                    await browser.stop()

            if hasattr(self.bot, 'auto_poller'):
                try:
                    self.bot.auto_poller.poll_loop.cancel()
                except Exception:
                    pass

            pid_file = Path("bot.pid")
            pid_file.unlink(missing_ok=True)

            subprocess.Popen([sys.executable] + sys.argv)

            await self.bot.close()
            os._exit(0)

        except Exception as e:
            logger.error(f"Restart failed: {e}")
            await ctx.send(f"Come <@1216284053049704600>. New Error")

    @commands.command(name="ping")
    async def ping(self, ctx):
        """Usage: $ping. Checks bot latency and service health."""
        latency = round(self.bot.latency * 1000)

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
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            if not scraper:
                return await msg.edit(content=f"❌ **Error:** No provider found for this URL.")

            data = await scraper.get_series_info(url)
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data

            embed = discord.Embed(title=f"✅ {title}", color=0x2ecc71)
            embed.add_field(name="Series ID", value=f"`{series_id}`", inline=True)
            embed.add_field(name="Chapters", value=f"`{total_chapters}`", inline=True)
            embed.add_field(name="Status", value=f"`{status_label}`", inline=True)
            embed.add_field(name="Release", value=f"`{release_day} @ {release_time}`", inline=True)
            if genre_label:
                embed.add_field(name="Genre", value=f"`{genre_label}`", inline=True)
            await msg.edit(content="", embed=embed)

        except Exception as e:
            logger.error(f"$test failed for {url}: {e}", exc_info=True)
            await msg.edit(content=f"❌ **Error:** `{str(e).splitlines()[0]}`")
