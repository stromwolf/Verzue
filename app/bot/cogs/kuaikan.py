import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("KuaikanCog")

class KuaikanCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.semaphore = asyncio.BoundedSemaphore(3)

    @app_commands.command(name="kuaikan", description="Coming Soon")
    async def kuaikan(self, interaction: discord.Interaction, url: str):
        # 1. STRICT VALIDATION
        if "kuaikanmanhua.com" not in url:
            error_embed = discord.Embed(
                title="⛔ Invalid Link",
                description="The `/kuaikan` command only accepts links from `kuaikanmanhua.com`.",
                color=0xe74c3c
            )
            return await interaction.response.send_message(embed=error_embed, ephemeral=True)

        # 2. IMMEDIATE DEFER (3-Second Rule)
        await interaction.response.defer()

        # 3. SEMAPHORE GATEKEEPER
        async with self.semaphore:
            req_id = str(uuid.uuid4())[:8].upper()
            token = req_id_context.set(req_id)

            try:
                logger.info("="*50)
                logger.info(f"📥 NEW REQUEST: Kuaikan Service")
                logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
                logger.info(f"🔗 URL: {url}")
                logger.info("="*50)

                # API Metadata Fetch
                scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
                logger.info(f"🔍 Fetching metadata for: {url}")
                
                # S-Grade Async
                data = await scraper.get_series_info(url)
                title, total_chapters, chapter_list, image_url, series_id, _, _, status_label, genre_label = data

                # Pack Context
                ctx_data = {
                    'url': url,
                    'title': title,
                    'chapters': chapter_list,
                    'total_chapters': total_chapters,
                    'image_url': image_url,
                    'series_id': series_id,
                    'status_label': status_label,
                    'genre_label': genre_label,
                    'req_id': req_id,
                    'user': interaction.user
                }

                # Launch Dashboard
                view = UniversalDashboard(self.bot, ctx_data, "kuaikan")
                view.interaction = interaction
                
                payload_data = {"flags": 32768, "components": view.build_v2_payload()}
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=payload_data)
                logger.info(f"✅ Dashboard active for '{title}'")

            except Exception as e:
                logger.error(f"System Failure: {e}", exc_info=True)
                error_embed = discord.Embed(
                    title="❌ Kuaikan Service Error",
                    description=f"Failed to initialize.\n**Reason:** `{str(e).splitlines()[0]}`",
                    color=0xe67e22
                )
                await interaction.followup.send(embed=error_embed)
            finally:
                req_id_context.reset(token)

async def setup(bot):
    await bot.add_cog(KuaikanCog(bot))