import discord
from discord.ext import commands
import sys
import os
import asyncio
import logging
from config.settings import Settings

logger = logging.getLogger("AdminCog")

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
            
        return ctx.author.id in Settings.ALLOWED_IDS

    @commands.command(name="sync")
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands."""
        msg = await ctx.send("🔄 **Syncing slash commands...**")
        try:
            # Syncs all app_commands (Slash Commands) to Discord
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ **Success!** Synced {len(synced)} commands globally.")
            logger.info(f"Manual Sync: {len(synced)} commands synced by {ctx.author}")
        except Exception as e:
            await msg.edit(content=f"❌ **Sync failed:** `{e}`")

    @commands.command(name="restart", aliases=["reboot", "reset"])
    async def restart_bot(self, ctx):
        """Clean shutdown of services and process reboot."""
        msg = await ctx.send("🔄 **Initiating System Reboot...**")
        
        try:
            # 1. SHUT DOWN BROWSER
            # We access the browser via the shared registry
            logger.info("Reboot: Terminating Browser Engine (if any)...")
            # The new BrowserService is ephemeral, so there's nothing to stop globally.
            # This is a no-op now, but left for compatibility.
            browser = self.bot.task_queue.scraper_registry.browser
            if browser and hasattr(browser, 'stop'):
                # Handle both sync and async stop methods for future compatibility
                if asyncio.iscoroutinefunction(browser.stop):
                    await browser.stop()
                else:
                    browser.stop()
            
            # 2. UPDATE UI
            await msg.edit(content="👋 **Services stopped. Rebooting now...**")
            await asyncio.sleep(1) # Short pause for Discord to send the msg

            # 3. EXECUTE RESTART
            # This replaces the current process with a new one
            logger.info(f"Reboot: Process re-executing by {ctx.author}")
            os.execv(sys.executable, [sys.executable] + sys.argv)
            
        except Exception as e:
            logger.error(f"Restart Failed: {e}")
            await msg.edit(content=f"❌ **Restart failed:** `{e}`")

    @commands.command(name="ping")
    async def ping(self, ctx):
        """Basic health check for the bot and event loop."""
        latency = round(self.bot.latency * 1000)
        
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"**Latency:** `{latency}ms`\n**Status:** `Operational`",
            color=0x2ecc71
        )
        # Check Redis connection via the task queue
        from app.services.redis_manager import RedisManager
        is_redis = await RedisManager().check_connection()
        
        embed.add_field(name="Global Brain", value="✅ Connected" if is_redis else "❌ Disconnected", inline=True)
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))