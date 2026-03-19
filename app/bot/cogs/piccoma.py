import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("PiccomaCog")

class PiccomaCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="piccoma", description="Extract chapters from Piccoma")
    async def piccoma(self, interaction: discord.Interaction, url: str):
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
            logger.info(f"📥 NEW REQUEST: Piccoma Service")
            logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
            logger.info(f"🔗 URL: {url}")
            logger.info("="*50)

            # 4. Metadata Fetch
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            
            # 5. Fetch Series Info (Returns 5 values: title, total, chapters, image_url, series_id)
            logger.info(f"🔍 Processing metadata request for: {url}")
            data = await scraper.get_series_info(url)
            
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time = data

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

            # 7. Initialize Universal Dashboard with service_type="piccoma"
            view = UniversalDashboard(self.bot, ctx_data, "piccoma")
            view.interaction = interaction
            
            # Send the dashboard using the V2 raw HTTP route
            payload_data = {"flags": 32768, "components": view.build_v2_payload()}
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=payload_data)
            logger.info(f"✅ Dashboard launched for '{title}'")

        except Exception as e:
            logger.error(f"Failed to launch dashboard: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ Piccoma Error",
                description=f"Could not load metadata.\n**Reason:** `{str(e)}`",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=error_embed)
        finally:
            req_id_context.reset(token)

async def setup(bot):
    """Entry point for Discord.py extension loader."""
    await bot.add_cog(PiccomaCog(bot))
