import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import re
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("JumptoonCog")

class JumptoonCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jumptoon", description="Download from Jumptoon (High-Speed API)")
    async def jumptoon(self, interaction: discord.Interaction, url: str):
        """
        Phase 1: Intelligence.
        Fetches metadata and launches the Universal Dashboard.
        """
        # 1. Immediate Defer (The 3-Second Rule)
        try:
            await interaction.response.defer()
        except (discord.errors.HTTPException, discord.errors.NotFound):
            pass
        
        # 2. GENERATE AND SET REQUEST ID EARLY
        req_id = str(uuid.uuid4())[:8].upper()
        token = req_id_context.set(req_id)

        try:
            # 3. MAX LEVEL LOGGING
            logger.info("="*50)
            logger.info(f"📥 NEW REQUEST: Jumptoon Service")
            logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
            logger.info(f"🔗 URL: {url}")
            logger.info("="*50)

            # 4. Access the specialized Jumptoon Scraper
            # Registry handles the instance management
            scraper = self.bot.task_queue.scraper_registry.jumptoon
            
            # 5. Fetch Series Info (Returns 5 values: title, total, chapters, image_url, series_id)
            # We run this in a thread to keep the Discord heartbeat alive
            logger.info(f"🔍 Processing metadata request for: {url}")
            data = await asyncio.to_thread(scraper.get_series_info, url)
            
            title = data[0]
            total_chapters = data[1]
            chapter_list = data[2]
            image_url = data[3]
            series_id = data[4] # e.g. JT00130

            # 6. Pack Context for the Universal Dashboard
            ctx_data = {
                'url': url,
                'title': title,
                'chapters': chapter_list,
                'image_url': image_url,
                'series_id': series_id,
                'req_id': req_id,
                'user': interaction.user
            }

            # 7. Initialize Universal Dashboard with service_type="jumptoon"
            view = UniversalDashboard(self.bot, ctx_data, "jumptoon")
            view.interaction = interaction
            
            await interaction.followup.send(embed=view.build_live_embed(), view=view)
            logger.info(f"✅ Dashboard launched for '{title}'")

        except Exception as e:
            logger.error(f"Failed to launch dashboard: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ Jumptoon Error",
                description=f"Could not load metadata.\n**Reason:** `{str(e).splitlines()[0]}`",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=error_embed)
        finally:
            req_id_context.reset(token)

async def setup(bot):
    """Entry point for Discord.py extension loader."""
    await bot.add_cog(JumptoonCog(bot))