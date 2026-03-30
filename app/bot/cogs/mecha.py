import discord
from discord import app_commands
from discord.ext import commands
import uuid
import asyncio
import logging
from app.bot.common.view import UniversalDashboard
from app.core.logger import req_id_context

logger = logging.getLogger("MechaCog")

class MechaCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 3. BOUNDED SEMAPHORE: Limits concurrent metadata/warmup phases to 3
        self.semaphore = asyncio.BoundedSemaphore(3)
        self._seen_interactions = set()

    @app_commands.command(name="mecha", description="Browse & Download from MechaComic")
    async def mecha(self, interaction: discord.Interaction, url: str):
        """
        Phase 1: Intelligence (Validated & Throttled).
        """
        # DEDUP GUARD: Ignore re-delivered stale interactions
        if interaction.id in self._seen_interactions:
            return
        self._seen_interactions.add(interaction.id)
        
        # Cleanup old IDs (prevent memory leak)
        if len(self._seen_interactions) > 100:
            self._seen_interactions.clear()

        # 1. IMMEDIATE DEFER (3-Second Rule)
        try:
            await interaction.response.defer()
        except (discord.errors.HTTPException, discord.errors.NotFound):
            # Interaction already acknowledged (re-delivered after restart, or double-invoke)
            # Safe to continue — followup.send() will still work
            pass

        # 2. STRICT VALIDATION (The Bouncer)
        if "mechacomic.jp" not in url.lower():
            error_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [
                        {
                            "type": 10,
                            "content": "⛔ **Protocol Violation**\nExpected `mechacomic.jp` link."
                        },
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 2, "style": 2, "label": "Back to Dashboard",
                                    "custom_id": "v2Dash_Home", "emoji": {"name": "🏠"}
                                }
                            ]
                        }
                    ]
                }]
            }
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            return await self.bot.http.request(route, json=error_payload)

        # 3. SEMAPHORE GATEKEEPER (Max 3 Concurrent Processing)
        # If 3 users are already fetching metadata, the 4th waits here automatically.
        async with self.semaphore:
            req_id = str(uuid.uuid4())[:8].upper()
            token = req_id_context.set(req_id)

            try:
                # 4. MAX LEVEL LOGGING
                logger.info("="*50)
                logger.info(f"📥 NEW REQUEST: Mecha Service")
                logger.info(f"👤 USER: {interaction.user} ({interaction.user.id})")
                logger.info(f"🔗 URL: {url}")
                logger.info("="*50)

                # 5. Metadata Fetch
                scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
                logger.info(f"🔍 Fetching metadata for: {url}")
                
                # S-Grade Async
                data = await scraper.get_series_info(url)
                
                title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
                
                # 6. Pack Context
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

                # 7. Launch Dashboard
                view = UniversalDashboard(self.bot, ctx_data, "mecha")
                view.interaction = interaction
                
                payload_data = {"flags": 32768, "components": view.build_v2_payload()}
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=payload_data)
                logger.info(f"✅ Dashboard active for '{title}'")

                # 8. SPECULATIVE WARMUP
                asyncio.create_task(self.bot.task_queue.browser_service.start())
                logger.info("🔥 Browser Warmup Triggered (Background)")

            except Exception as e:
                logger.error(f"System Failure: {e}", exc_info=True)
                error_embed = discord.Embed(
                    title="❌ Mecha Service Error",
                    description=f"Failed to initialize.\n**Reason:** `{str(e).splitlines()[0]}`",
                    color=0xe67e22
                )
                await interaction.followup.send(embed=error_embed)
            finally:
                req_id_context.reset(token)

async def setup(bot):
    """Adds the Mecha Cog to the bot session."""
    await bot.add_cog(MechaCog(bot))
