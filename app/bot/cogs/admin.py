from discord import app_commands
from discord.ext import commands
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
            
        return ctx.author.id in Settings.ALLOWED_IDS

    @app_commands.command(name="cdn-menu", description="Set the Scan Group name for the dashboard")
    @app_commands.describe(
        scan_name="The name of your scan group (Leave blank for help guide)",
        target_id="The Discord Server or Channel ID (Leave blank for help guide)"
    )
    async def cdn_menu(self, interaction: discord.Interaction, scan_name: str = None, target_id: str = None):
        # 1. Admin Security Check
        if Settings.ALLOWED_IDS and interaction.user.id not in Settings.ALLOWED_IDS:
            return await interaction.response.send_message("❌ You are not authorized to use this command.", ephemeral=True)

        # 2. Trigger the "Invisible" Guide if they run it blank
        if not scan_name or not target_id:
            guide_embed = discord.Embed(
                title="ℹ️ How to use `/cdn-menu`",
                description=(
                    "This command links a specific Server or Channel to your Scan Group's name so the `/dashboard` updates its title.\n\n"
                    "**When you type the command, use Discord's popup options to fill these in:**\n"
                    "🔹 `scan_name`: The display name (e.g., `Thunder Scan`)\n"
                    "🔹 `target_id`: The ID of the Server or Channel (e.g., `1443643769751736523`)\n\n"
                    "**Example:**\n`/cdn-menu scan_name:Thunder Scan target_id:1443643769751736523`"
                ),
                color=0x3498db
            )
            # ephemeral=True makes it the "invisible" message
            return await interaction.response.send_message(embed=guide_embed, ephemeral=True)

        # 3. Process the setup if they provided the arguments
        try:
            t_id = int(target_id.strip())
            Settings.SERVER_MAP[t_id] = scan_name.strip()
            Settings.save_server_map()
            
            success_embed = discord.Embed(
                title="✅ Dashboard Mapped",
                description=f"Successfully mapped ID `{t_id}` to **{scan_name}**.\nThe `/dashboard` command will now say *Menu of {scan_name}* here.",
                color=0x2ecc71
            )
            await interaction.response.send_message(embed=success_embed, ephemeral=True)
            logger.info(f"Dashboard mapping updated: {t_id} -> {scan_name} by {interaction.user}")
        except ValueError:
            await interaction.response.send_message("❌ **Error:** `target_id` must be a valid number!", ephemeral=True)

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
            logger.info("Reboot: Terminating Browser Engine (if any)...")
            browser = self.bot.task_queue.scraper_registry.browser
            if browser and hasattr(browser, 'stop'):
                if asyncio.iscoroutinefunction(browser.stop):
                    await browser.stop()
                else:
                    browser.stop()
            
            # 2. KILL ANY PREVIOUSLY SAVED PID (stale instances)
            if PID_FILE.exists():
                try:
                    old_pid = int(PID_FILE.read_text().strip())
                    if old_pid != os.getpid():  # Don't kill ourselves yet
                        if os.name == 'nt':
                            # Windows alternative to SIGTERM
                            os.system(f"taskkill /F /PID {old_pid}")
                        else:
                            os.kill(old_pid, signal.SIGTERM)
                        logger.info(f"Reboot: Killed stale instance PID {old_pid}")
                except (ProcessLookupError, ValueError, Exception) as e:
                    logger.debug(f"Reboot: Stale instance PID cleanup note: {e}")
                PID_FILE.unlink(missing_ok=True)

            # 3. UPDATE UI
            await msg.edit(content="👋 **Services stopped. Rebooting now...**")
            await asyncio.sleep(1)

            # 4. EXECUTE RESTART IN SAME CONSOLE
            logger.info(f"Reboot: Process re-executing by {ctx.author}")
            
            import subprocess
            # Spawn the new bot in the exact same terminal window
            subprocess.Popen([sys.executable] + sys.argv)
            
            # Cleanly disconnect this old bot from Discord
            await self.bot.close()
            sys.exit(0)
            
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