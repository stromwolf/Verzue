import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("KakaoCog")

class KakaoCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="kakao", description="Coming Soon")
    async def kakaopage(self, interaction: discord.Interaction, url: str):
        # 1. THE 3-SECOND RULE (First line)
        try:
            await interaction.response.defer()
        except (discord.errors.HTTPException, discord.errors.NotFound):
            pass
        
        # 2. GENERATE AND SET REQUEST ID EARLY
        req_id = str(uuid.uuid4())[:8].upper()
        token = req_id_context.set(req_id)

        try:
            # 3. MAX LEVEL LOGGING (Make the terminal noisy)
            logger.info("="*50)
            logger.info(f"📥 NEW REQUEST: Kakao Service")
            logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
            logger.info(f"🔗 URL: {url}")
            logger.info("="*50)

            # 4. Metadata Fetch
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            data = await scraper.get_series_info(url)
            
            ctx = {
                'url': url,
                'title': data[0],
                'chapters': data[2],
                'image_url': data[3],
                'series_id': data[4],
                'req_id': req_id,
                'user': interaction.user
            }
            
            # 5. Launch Dashboard
            view = UniversalDashboard(self.bot, ctx, "kakao") 
            view.interaction = interaction # CRITICAL for edits
            
            payload_data = {"flags": 32768, "components": view.build_v2_payload()}
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=payload_data)
            logger.info(f"✅ Dashboard successfully launched.")
            
        except Exception as e:
            logger.error(f"❌ Kakao Metadata Failure: {e}", exc_info=True)
            await interaction.followup.send(f"❌ **Metadata Error:** {e}")
        finally:
            req_id_context.reset(token)

async def setup(bot):
    await bot.add_cog(KakaoCog(bot))