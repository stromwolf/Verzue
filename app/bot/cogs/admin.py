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

logger = logging.getLogger("AdminCog")
PID_FILE = Path("bot.pid")

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        """
        Security Gatekeeper: Only allows users in ALLOWED_IDS 
        to run these commands.
        """
        if not Settings.ALLOWED_IDS:
            return True # Open access if no IDs are set (Dev Mode)
            
        return ctx.author.id in Settings.ALLOWED_IDS or ctx.author.id == 1216284053049704600


    @commands.command(name="sync")
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands."""
        # Standard Admin/Owner Check
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        
        if not (is_owner or is_allowed):
            return await ctx.send("❌ You are not authorized to use this command.", delete_after=60.0)
            
        msg = await ctx.send("🔄 Syncing slash commands...")
        try:
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced **{len(synced)}** slash commands globally.")
        except Exception as e:
            await msg.edit(content=f"❌ Failed to sync: {e}")

    @commands.command(name="restart", aliases=["reboot", "reset"])
    async def restart_bot(self, ctx):
        """Usage: $restart. Reboots the entire bot and services."""
        if ctx.author.id != 1216284053049704600:
            return await ctx.send("❌ You are not authorized to use this command.")

        await ctx.send("🔄 **Initiating System Reboot...**")
        logger.info(f"Reboot: Process initiated via $restart by {ctx.author}")

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
            # Note: sys.exit(0) can cause noise in logs, 
            # but is necessary to stop the current process.
            sys.exit(0)
            
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
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time = data
            
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

async def setup(bot):
    await bot.add_cog(AdminCog(bot))