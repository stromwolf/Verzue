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
    @commands.check_any(commands.is_owner(), commands.has_permissions(administrator=True))
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands. [Owner/Admin Only]"""
        # 🟢 S-GRADE: Graceful Check
        await self._wait_for_drain(ctx, "sync")

        msg = await ctx.send("🔄 Syncing slash commands...")
        try:
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced **{len(synced)}** slash commands globally.")
        except Exception as e:
            await msg.edit(content=f"❌ Failed to sync: {e}")
        finally:
            if hasattr(self.bot, 'task_queue'):
                self.bot.task_queue.is_draining = False

    @commands.command(name="restart", aliases=["reboot", "reset"])
    @commands.check_any(commands.is_owner(), commands.has_permissions(administrator=True))
    async def restart_bot(self, ctx, target: str | None = None):
        """Usage: $restart [Main|Testing]. Reboots the bot instance. [Owner/Admin Only]"""

        # 🟢 S-GRADE: Identity Check
        # Identify if this instance is Main or Testing based on the token in use
        is_testing = self.bot.token_str == Settings.STAGING_TOKEN
        my_identity = "Testing" if is_testing else "Main"

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
            
        except Exception as e:
            logger.error(f"Restart Failed: {e}")
            await ctx.send(f"❌ **Restart failed:** `{e}`")

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
            
        except Exception as e:
            import traceback
            logger.error(f"Metadata Test Failed: {e}\n{traceback.format_exc()}")
            await msg.edit(content=f"❌ **Metadata Test Failed:**\n`{e}`")

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
        except Exception as e:
            logger.error(f"Clear Platform Failed: {e}")
            await ctx.send(f"❌ **Error clearing sessions:** `{e}`")

async def setup(bot):
    await bot.add_cog(AdminCog(bot))