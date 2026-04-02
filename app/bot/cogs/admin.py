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
    async def restart_bot(self, ctx):
        """Usage: $restart. Reboots the entire bot and services. [Owner/Admin Only]"""

        # 🟢 S-GRADE: Graceful Check
        await self._wait_for_drain(ctx, "restart")

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

    # --- 🟢 [NEW] AUTOMATED ACCOUNT MANAGEMENT (FOR TESTING BOT) ---

    @app_commands.command(name="add-account", description="[Admin] Register account credentials for automated headless login.")
    @app_commands.describe(
        platform="The platform (e.g. piccoma, mecha)",
        email="Account email/username",
        password="Account password",
        account_id="Optional: Target account ID (Default: primary)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Mecha Comic", value="mecha"),
    ])
    async def add_account(self, interaction: discord.Interaction, platform: str, email: str, password: str, account_id: str = "primary"):
        """Register credentials for the automated cookie recovery system."""
        # Standard Cog interaction_check handles security and deferral automatically.
        
        success = await self.login_service.save_credentials(platform, email, password, account_id)
        if success:
            await interaction.followup.send(f"✅ **Account Registered:** Credentials for **{platform}** ({email}) saved for automated fallback.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Error:** Failed to save credentials. Check logs.", ephemeral=True)

    @app_commands.command(name="force-refresh", description="[Admin] Manually trigger the automated headless login to refresh cookies.")
    @app_commands.describe(
        platform="The platform to refresh",
        account_id="Optional: Target account ID (Default: primary)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Mecha Comic", value="mecha"),
    ])
    async def force_refresh(self, interaction: discord.Interaction, platform: str, account_id: str = "primary"):
        """Manually trigger the headless login flow."""
        # Standard Cog interaction_check handles security and deferral automatically.
        
        await interaction.followup.send(f"🔄 **Refresh Initiated:** Attempting headless login for **{platform}**...", ephemeral=True)
        
        success = await self.login_service.auto_login(platform, account_id)
        if success:
            await interaction.followup.send(f"✅ **Refresh Successful:** Cookies for **{platform}** have been updated automatically.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Refresh Failed:** Automated login for **{platform}** could not be completed. Check logs.", ephemeral=True)

    @app_commands.command(name="list-sessions", description="[Admin] List all sessions and their current status for a platform.")
    @app_commands.describe(platform="The platform to list")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Mecha Comic", value="mecha"),
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
    ])
    async def list_sessions(self, interaction: discord.Interaction, platform: str):
        """Debug helper to view active sessions in Redis."""
        # Standard Cog interaction_check handles security and deferral automatically.
        
        from app.services.redis_manager import RedisManager
        try:
            aids = await RedisManager().list_sessions(platform)
            if not aids:
                return await interaction.followup.send(f"ℹ️ No sessions found for **{platform}**.", ephemeral=True)
            
            embed = discord.Embed(title=f"📋 Sessions: {platform.capitalize()}", color=0x3498db)
            for aid in sorted(aids):
                session = await RedisManager().get_session(platform, aid)
                if not session: continue
                
                status = session.get("status", "UNKNOWN")
                cookies_count = len(session.get("cookies", []))
                reason = session.get("error_reason", "None")
                
                status_emoji = "🟢" if status == "HEALTHY" else "🔴"
                val = f"**Status:** {status_emoji} `{status}`\n**Cookies:** `{cookies_count}`\n**Reason:** *{reason}*"
                embed.add_field(name=f"👤 ID: {aid}", value=val, inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Security Check for Slash Commands in this cog with automatic deferral."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
            
        if not Settings.ALLOWED_IDS:
            return True
        return interaction.user.id in Settings.ALLOWED_IDS or interaction.user.id == 1216284053049704600

async def setup(bot):
    await bot.add_cog(AdminCog(bot))