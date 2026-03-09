import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("AcQqCog")

class AcQqCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="acqq", description="Coming Soon")
    async def acqq(self, interaction: discord.Interaction, url: str):
        try:
            await interaction.response.defer()
        except (discord.errors.HTTPException, discord.errors.NotFound):
            pass
        
        req_id = str(uuid.uuid4()).upper()
        token = req_id_context.set(req_id)

        try:
            logger.info("="*50)
            logger.info(f"📥 NEW REQUEST: Tencent AC.QQ Service")
            logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
            logger.info(f"🔗 URL: {url}")
            logger.info("="*50)

            scraper = self.bot.task_queue.scraper_registry.acqq
            logger.info(f"🔍 Processing metadata request for: {url}")
            data = await asyncio.to_thread(scraper.get_series_info, url)
            
            # PROPERLY UNPACK THE TUPLE
            title, total_chapters, chapter_list, image_url, series_id = data
            ctx_data = {
                'url': url,
                'title': title,
                'chapters': chapter_list,
                'image_url': image_url,
                'series_id': series_id,
                'req_id': req_id,
                'user': interaction.user
            }

            # Utilizing 'smartoon' style for standard vertical UI handling
            view = UniversalDashboard(self.bot, ctx_data, "smartoon")
            view.interaction = interaction
            
            payload_data = {"flags": 32768, "components": view.build_v2_payload()}
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=payload_data)
            logger.info(f"✅ Dashboard launched for '{title}'")

        except Exception as e:
            logger.error(f"Failed to launch dashboard: {e}", exc_info=True)
            error_embed = discord.Embed(
                title="❌ AC.QQ Error",
                description=f"Could not load metadata.\n**Reason:** `{str(e).splitlines()}`",
                color=0xe74c3c
            )
            await interaction.followup.send(embed=error_embed)
        finally:
            req_id_context.reset(token)

async def setup(bot):
    await bot.add_cog(AcQqCog(bot))