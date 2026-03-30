import discord
from discord.ext import commands
from discord import app_commands
import logging
import json
import asyncio
from app.core.logger import logger
from app.providers.manager import ProviderManager
from app.services.redis_manager import RedisManager
from app.bot.common.notification_builder import build_new_series_notification_payload, build_notification_payload
from app.models.chapter import ChapterTask
from config.settings import Settings

class Discovery(commands.Cog):
    """Cog for discovering new series and managing the 'Premiere Brain'."""
    def __init__(self, bot):
        self.bot = bot
        self.pm = ProviderManager()
        # Handle both main bot and helper bot
        self.main_bot = bot if hasattr(bot, 'redis_brain') else getattr(bot, 'main_bot', None)

    @commands.command(name="test_ui")
    async def test_discovery_ui(self, ctx, platform: str = "jumptoon"):
        """Debug command to visualize the New Series Detection UI for a specific platform."""
        platform = platform.lower()
        logger.info(f"🧪 [Discovery] Triggered $test UI preview for {platform} by {ctx.author}")
        
        # Mapping for mock data
        mocks = {
            "jumptoon": {
                "title": "The Rebirth of the S-Grade Ranker",
                "series_id": "JT00138",
                "url": "https://jumptoon.com/series/JT00138",
                "poster_url": "https://assets.jumptoon.com/series/JT00138/episode/11/v2_0_0/20260310020123/83253720a97dd8e1caa50e3f75ec075b0978567ebcff56773dc1dc319b06ee0f.webp"
            },
            "piccoma": {
                "title": "Solo Leveling (Piccoma Edition)",
                "series_id": "5535",
                "url": "https://piccoma.com/web/product/5535",
                "poster_url": "https://piccoma.com/web/product/5535" # Just a placeholder link
            },
            "mecha": {
                "title": "Blue Lock (MechaComic)",
                "series_id": "123456",
                "url": "https://mechacomic.jp/books/123456",
                "poster_url": "https://mechacomic.jp/books/123456" 
            }
        }
        
        mock_series = mocks.get(platform, mocks["jumptoon"])
        
        payload = build_new_series_notification_payload(
            platform=platform if platform in mocks else "jumptoon",
            series_title=mock_series["title"],
            poster_url=mock_series["poster_url"],
            series_url=mock_series["url"],
            series_id=mock_series["series_id"]
        )
        
        try:
            route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=ctx.channel.id)
            await self.bot.http.request(route, json=payload)
        except Exception as e:
            await ctx.send(f"❌ Failed to render V2 Component: `{e}`")

    @commands.command(name="test_uich", aliases=["test_chapter_ui"])
    async def test_chapter_ui(self, ctx, target: str = "jumptoon"):
        """Debug command to visualize the New Chapter Update UI. Target can be a platform or live URL."""
        logger.info(f"🧪 [Discovery] Triggered $test UI preview for {target} by {ctx.author}")
        
        # 1. Determine if target is a URL or platform
        is_url = target.startswith("http")
        platform = "jumptoon" # Default
        
        if is_url:
            provider = self.pm.get_provider_for_url(target)
            if not provider:
                return await ctx.send(f"❌ Unsupported URL or platform not detected: `{target}`")
            
            # Identify platform for accent colors
            url_lower = target.lower()
            if "mechacomic.jp" in url_lower: platform = "mecha"
            elif "jumptoon.com" in url_lower: platform = "jumptoon"
            elif "piccoma.com" in url_lower: platform = "piccoma"
            elif "kakao.com" in url_lower: platform = "kakao"
            elif "kuaikanmanhua.com" in url_lower: platform = "kuaikan"
            elif "ac.qq.com" in url_lower: platform = "acqq"

            # Fetch Real Data
            try:
                msg = await ctx.send("🔍 Fetching live series data...")
                data = await provider.get_series_info(target)
                # (title, total_chapters, all_chapters, image_url, series_id, ...)
                title, total_chapters, chapter_list, image_url, s_id, _, _, _, _ = data
                
                if not chapter_list:
                    return await msg.edit(content="⚠️ URL processed but no chapters found.")
                
                # Use the latest chapter (usually index 0 for most providers)
                latest_ch = chapter_list[0]
                
                mock_data = {
                    "title": title,
                    "custom_title": latest_ch.get("notation", "New Chapter"),
                    "series_id": s_id or target.split("/")[-1],
                    "url": target,
                    "poster_url": image_url,
                    "chapter_id": latest_ch.get("id") # Added
                }
                await msg.delete()
            except Exception as e:
                logger.error(f"Live Test Error: {e}")
                return await ctx.send(f"❌ Failed to fetch live data: `{e}`")
        else:
            # Original Mock Logic
            platform = target.lower()
            mocks = {
                "jumptoon": {
                    "title": "The Rebirth of the S-Grade Ranker",
                    "custom_title": "Chapter 45",
                    "series_id": "JT00138",
                    "url": "https://jumptoon.com/series/JT00138",
                    "poster_url": "https://assets.jumptoon.com/series/JT00138/episode/11/v2_0_0/20260310020123/83253720a97dd8e1caa50e3f75ec075b0978567ebcff56773dc1dc319b06ee0f.webp",
                    "chapter_id": "11"
                },
                "piccoma": {
                    "title": "Solo Leveling (Piccoma Edition)",
                    "custom_title": "Episode 179",
                    "series_id": "5535",
                    "url": "https://piccoma.com/web/product/5535",
                    "poster_url": "https://piccoma.com/web/product/5535",
                    "chapter_id": "179"
                },
                "mecha": {
                    "title": "Blue Lock (MechaComic)",
                    "custom_title": "Chapter 250",
                    "series_id": "123456",
                    "url": "https://mechacomic.jp/books/123456",
                    "poster_url": "https://mechacomic.jp/books/123456",
                    "chapter_id": "250"
                }
            }
            mock_data = mocks.get(platform, mocks["jumptoon"])
            platform = platform if platform in mocks else "jumptoon"

        payload = build_notification_payload(
            platform=platform,
            role_id=None, # No ping in test
            series_title=mock_data["title"],
            custom_title=mock_data["custom_title"],
            poster_url=mock_data["poster_url"],
            series_url=mock_data["url"],
            series_id=mock_data["series_id"],
            notification_id=9999,
            chapter_id=mock_data.get("chapter_id")
        )
        
        try:
            route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=ctx.channel.id)
            await self.bot.http.request(route, json=payload)
        except Exception as e:
            await ctx.send(f"❌ Failed to render V2 Component: `{e}`")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
            
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("discovery:"):
            return
            
        # Parse: discovery:action:platform:series_id:[chapter_id]
        parts = custom_id.split(":")
        if len(parts) < 4: return
        
        action = parts[1]
        platform = parts[2]
        series_id = parts[3]
        
        if action == "download_all":
            await self._handle_download_all(interaction, platform, series_id)
        elif action == "preview_first":
            await self._handle_preview_first(interaction, platform, series_id)
        elif action == "download_chapter":
            chapter_id = parts[4] if len(parts) > 4 else None
            await self._handle_download_chapter(interaction, platform, series_id, chapter_id)

    async def _handle_download_chapter(self, interaction: discord.Interaction, platform: str, series_id: str, chapter_id: str):
        if not chapter_id:
            return await interaction.response.send_message("❌ Missing Chapter ID.", ephemeral=True)
        # 🟢 S-GRADE: Defer (Type 5) without ephemeral to transform the original message
        await interaction.response.defer()
        try:
            provider = self.pm.get_provider(platform)
            url = f"{provider.BASE_URL}{provider.SERIES_PATH}{series_id}"
            
            # Fetch Series Info
            data = await provider.get_series_info(url)
            title, total_chapters, chapter_list, image_url, s_id, r_day, r_time, status_label, genre_label = data
            
            if not chapter_list:
                return await interaction.followup.send("⚠️ No chapters found to download.", ephemeral=True)
            
            # Find the specific chapter
            target_ch = next((ch for ch in chapter_list if str(ch["id"]) == str(chapter_id)), None)
            
            if not target_ch:
                return await interaction.followup.send(f"⚠️ Chapter ID `{chapter_id}` not found in the current list.", ephemeral=True)

            if target_ch.get("is_locked"):
                return await interaction.followup.send("⚠️ This chapter is locked and cannot be downloaded.", ephemeral=True)

            # 3. Queue for Download via BatchController
            from app.services.batch_controller import BatchController
            controller = BatchController(self.bot)
            
            target_idx = next((i for i, ch in enumerate(chapter_list) if str(ch["id"]) == str(chapter_id)), None)
            if target_idx is None:
                 return await interaction.followup.send("❌ Chapter not found in list.", ephemeral=True)

            # 🟢 S-GRADE: UI Transformation (Base Dashboard setup)
            from app.bot.common.view import UniversalDashboard
            ctx_data = {
                'url': url, 'title': title, 'chapters': chapter_list,
                'image_url': image_url, 'req_id': f"DISC-{series_id[:4]}",
                'series_id': series_id, 'user': interaction.user.id,
                'status_label': status_label, 'genre_label': genre_label
            }
            view = UniversalDashboard(self.bot, ctx_data, platform)
            view.interaction = interaction
            view.processing_mode = True
            view.phases = {"analyze": "loading", "purchase": "waiting", "download": "waiting"}
            await view.update_view(interaction)

            # 🟢 Folder Resolution & Task Preparation
            tasks = await controller.prepare_batch(
                interaction=interaction,
                selected_indices=[target_idx],
                all_chapters=chapter_list,
                title=title,
                url=url,
                view_ref=view,
                series_id=series_id
            )
            
            if not tasks:
                 # It might have been skipped because it already exists; the dashboard will show results.
                 return

            # Start the background tasks
            for t in tasks:
                 await self.bot.task_queue.add_task(t)
            
            asyncio.create_task(view.monitor_tasks())
            
        except Exception as e:
            logger.error(f"Download Chapter Error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to start download: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to start download: `{e}`", ephemeral=True)

    async def _handle_preview_first(self, interaction: discord.Interaction, platform: str, series_id: str):
        await interaction.response.defer()
        try:
            provider = self.pm.get_provider(platform)
            url = f"{provider.BASE_URL}{provider.SERIES_PATH}{series_id}"
            
            # Fetch Series Info (Normal fetch to ensure sorting)
            data = await provider.get_series_info(url)
            title, total_chapters, chapter_list, image_url, s_id, r_day, r_time, status_label, genre_label = data
            
            if not chapter_list:
                return await interaction.followup.send("⚠️ No chapters found for preview.", ephemeral=True)
            
            # Find the FIRST chapter (already sorted by provider)
            first_ch = chapter_list[0]
            
            # Queue for Download
            group_name = Settings.SERVER_MAP.get(interaction.guild_id, Settings.DEFAULT_CLIENT_NAME)
            ch_url = f"{url}/episodes/{first_ch['id']}" if platform == "jumptoon" else first_ch.get("url")
            
            # 3. Queue for Preview Preview via BatchController
            from app.services.batch_controller import BatchController
            controller = BatchController(self.bot)
            
            # 🟢 S-GRADE: UI Transformation
            from app.bot.common.view import UniversalDashboard
            ctx_data = {
                'url': url, 'title': title, 'chapters': chapter_list,
                'image_url': image_url, 'req_id': f"PREVIEW-{series_id[:4]}",
                'series_id': series_id, 'user': interaction.user.id,
                'status_label': status_label, 'genre_label': genre_label
            }
            view = UniversalDashboard(self.bot, ctx_data, platform)
            view.interaction = interaction
            view.processing_mode = True
            view.phases = {"analyze": "loading", "purchase": "waiting", "download": "waiting"}
            await view.update_view(interaction)

            # 🟢 Folder Resolution & Task Preparation
            tasks = await controller.prepare_batch(
                interaction=interaction,
                selected_indices=[0], # Preview always first chapter
                all_chapters=chapter_list,
                title=title,
                url=url,
                view_ref=view,
                series_id=series_id
            )
            
            if not tasks: return

            for t in tasks:
                 await self.bot.task_queue.add_task(t)
            
            asyncio.create_task(view.monitor_tasks())
            
        except Exception as e:
            logger.error(f"Preview Error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to start preview: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to start preview: `{e}`", ephemeral=True)

    async def _handle_download_all(self, interaction: discord.Interaction, platform: str, series_id: str):
        await interaction.response.defer()
        # 1. Get Provider
        try:
            provider = self.pm.get_provider(platform)
            url = f"{provider.BASE_URL}{provider.SERIES_PATH}{series_id}"
            
            # 2. Fetch Series Info
            data = await provider.get_series_info(url)
            title, total_chapters, chapter_list, image_url, s_id, r_day, r_time, status_label, genre_label = data
            
            if not chapter_list:
                return await interaction.followup.send("⚠️ No chapters found to download.", ephemeral=True)
                
            # 🟢 S-GRADE: UI Transformation for Bulk Download
            from app.bot.common.view import UniversalDashboard
            from app.services.batch_controller import BatchController
            
            req_id = f"BULK-{series_id[:4]}"
            ctx_data = {
                'url': url, 'title': title, 'chapters': chapter_list,
                'image_url': image_url, 'req_id': req_id,
                'series_id': series_id, 'user': interaction.user.id,
                'status_label': status_label, 'genre_label': genre_label
            }
            view = UniversalDashboard(self.bot, ctx_data, platform)
            view.interaction = interaction
            view.processing_mode = True
            view.phases = {"analyze": "loading", "purchase": "waiting", "download": "waiting"}
            await view.update_view(interaction)

            # 🟢 Folder Resolution & Task Preparation (Bulk)
            # Filter to free chapters
            free_indices = [i for i, ch in enumerate(chapter_list) if not ch.get("is_locked")]
            if not free_indices:
                 return await interaction.followup.send("⚠️ No free chapters found to download.", ephemeral=True)

            controller = BatchController(self.bot)
            tasks = await controller.prepare_batch(
                interaction=interaction,
                selected_indices=free_indices,
                all_chapters=chapter_list,
                title=title,
                url=url,
                view_ref=view,
                series_id=series_id
            )
            
            if not tasks: return

            for t in tasks:
                 await self.bot.task_queue.add_task(t)

            asyncio.create_task(view.monitor_tasks())
            
        except Exception as e:
            logger.error(f"Download All Error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to start bulk download: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to start bulk download: `{e}`", ephemeral=True)

    @commands.command(name="check_new", aliases=["check_new_series"])
    async def manual_new_series_check(self, ctx, platform: str = None):
        """Forces a check for new series on specific or all platforms (Jumptoon, Piccoma, MechaComic)."""
        # 1. Feedback
        msg = await ctx.send(f"🔍 **Starting New Series Detection Sweep...**")
        
        # 2. Get Poller
        poller = getattr(self.bot, "auto_poller", None)
        if not poller:
            return await msg.edit(content="❌ `AutoPoller` not initialized correctly on this bot instance.")
            
        # 3. Choose Platforms (Supported: jumptoon, piccoma, mecha)
        from app.tasks.poller import JUMPTOON_NEW_SERIES_CHANNEL_ID
        all_platforms = ["jumptoon", "piccoma", "mecha"]
        
        if platform and platform.lower() not in all_platforms:
             return await msg.edit(content=f"❌ Unsupported platform: `{platform}`. Supported: `jumptoon`, `piccoma`, `mecha`.")

        targets = [platform.lower()] if platform else all_platforms
        
        results = []
        for p_name in targets:
            try:
                await msg.edit(content=f"🔍 Scanning **{p_name.capitalize()}** for new releases...")
                # We reuse the existing poller logic (generic discovery engine)
                await poller._run_discovery_for_platform(p_name, JUMPTOON_NEW_SERIES_CHANNEL_ID)
                results.append(f"✅ {p_name.capitalize()}")
                # Small delay to prevent rate-limiting or log flooding
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Manual Discovery Error for {p_name}: {e}")
                results.append(f"❌ {p_name.capitalize()}: `{e}`")

        # 4. Final Response (Self-Destructs or Clean status)
        res_text = "\n".join(results)
        await msg.edit(content=f"🏁 **Discovery Sweep Complete!**\n{res_text}\n\n*Announcement(s) sent to Discovery Channel if anything new was found.*")

async def setup(bot):
    await bot.add_cog(Discovery(bot))
