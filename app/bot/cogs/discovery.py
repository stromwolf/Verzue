import discord
from discord.ext import commands
from discord import app_commands
import logging
import json
import asyncio
from app.core.logger import logger
from app.providers.manager import ProviderManager
from app.services.redis_manager import RedisManager
from app.bot.common.notification_builder import build_new_series_notification_payload, build_notification_payload, build_hiatus_notification_payload
from app.models.chapter import ChapterTask
from app.core.events import EventBus
from config.settings import Settings

class Discovery(commands.Cog):
    """Cog for discovering new series and managing the 'Premiere Brain'."""
    def __init__(self, bot):
        self.bot = bot
        self.pm = ProviderManager()
        # Handle both main bot and helper bot
        self.main_bot = bot if hasattr(bot, 'redis_brain') else getattr(bot, 'main_bot', None)

    @commands.command(name="test_ui")
    async def test_discovery_ui(self, ctx, platform: str = "jumptoon", url: str | None = None):
        """Debug command to visualize the New Series Detection UI for a specific platform."""
        
        # Support: $test_ui <url> directly (no platform needed)
        if platform.startswith("http"):
            url = platform
            platform = "jumptoon"  # will be overridden below

        platform = platform.lower()
        logger.info(f"🧪 [Discovery] Triggered $test UI preview (Platform: {platform}, URL: {url}) by {ctx.author}")

        if url:
            try:
                msg = await ctx.send(f"🔍 **Scraping Live Data:** <{url}>")
                provider = self.pm.get_provider_for_url(url)
                if not provider:
                    return await msg.edit(content=f"❌ Unsupported URL: `{url}`")

                data = await provider.get_series_info(url)
                title, _, _, image_url, series_id, _, _, _, _ = data

                # Auto-detect platform from URL
                url_lower = url.lower()
                if "mechacomic.jp" in url_lower: platform = "mecha"
                elif "jumptoon.com" in url_lower: platform = "jumptoon"
                elif "piccoma.com" in url_lower: platform = "piccoma"
                elif "kakao.com" in url_lower: platform = "kakao"
                elif "kuaikanmanhua.com" in url_lower: platform = "kuaikan"
                elif "ac.qq.com" in url_lower: platform = "acqq"

                mock_series = {
                    "title": title,
                    "series_id": series_id,
                    "url": url,
                    "poster_url": image_url,
                }
                await msg.delete()
            except Exception as e:
                logger.error(f"Live Test Error: {e}")
                return await ctx.send(f"❌ Failed to fetch live data: `{e}`")
        else:
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
            platform = platform if platform in mocks else "jumptoon"
        
        payload = build_new_series_notification_payload(
            platform=platform,
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
    async def test_chapter_ui(self, ctx, target: str = "jumptoon", url: str | None = None):
        """Debug command to visualize the New Chapter Update UI. Target can be a platform or live URL."""
        # Normalize Logics: Support $test_uich <url> and $test_uich <platform> <url>
        final_url = url if url else (target if target.startswith("http") else None)
        final_platform = target if not target.startswith("http") else "jumptoon"
        
        logger.info(f"🧪 [Discovery] Triggered $test UI preview (Target: {final_platform}, URL: {final_url}) by {ctx.author}")
        
        if final_url:
            try:
                msg = await ctx.send(f"🔍 **Scraping Live Data:** <{final_url}>")
                provider = self.pm.get_provider_for_url(final_url)
                if not provider:
                    return await msg.edit(content=f"❌ Unsupported URL or platform not detected: `{final_url}`")
                
                # Fetch Real Data (The DEVELOPER_MODE logs are inside here)
                data = await provider.get_series_info(final_url)
                title, total_chapters, chapter_list, image_url, s_id, _, _, _, _ = data
                
                if not chapter_list:
                    return await msg.edit(content="⚠️ URL processed but no chapters found.")
                
                # Use the latest chapter
                latest_ch = chapter_list[-1]
                
                mock_data = {
                    "title": title,
                    "custom_title": None,  # No vault override in test context
                    "series_id": s_id or final_url.split("/")[-1],
                    "url": final_url,
                    "poster_url": image_url,
                    "chapter_id": latest_ch.get("id"),
                    "chapter_number": latest_ch.get("notation", "第1話")
                }
                # Determine platform for UI accenting
                url_lower = final_url.lower()
                if "mechacomic.jp" in url_lower: final_platform = "mecha"
                elif "jumptoon.com" in url_lower: final_platform = "jumptoon"
                elif "piccoma.com" in url_lower: final_platform = "piccoma"
                elif "kakao.com" in url_lower: final_platform = "kakao"
                elif "kuaikanmanhua.com" in url_lower: final_platform = "kuaikan"
                elif "ac.qq.com" in url_lower: final_platform = "acqq"
                
                await msg.delete()
                platform = final_platform # Sync for payload
            except Exception as e:
                logger.error(f"Live Test Error: {e}")
                return await ctx.send(f"❌ Failed to fetch live data: `{e}`")
        else:
            # Original Mock Logic (Fallback)
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
                    "chapter_id": "250",
                    "chapter_number": "第250話"
                }
            }
            mock_data = mocks.get(platform, mocks["jumptoon"])
            # Ensure mock_data has chapter_number for static mocks too
            if "chapter_number" not in mock_data:
                 mock_data["chapter_number"] = mock_data.get("custom_title", "第1話")
            platform = platform if platform in mocks else "jumptoon"

        try:
            from curl_cffi import requests as curl_requests
            import io
            import discord

            files = []
            use_attachment_proxy = False
            if mock_data.get("poster_url"):
                try:
                    # Download the poster to bypass Discord's proxy blocking
                    if Settings.DEVELOPER_MODE:
                        logger.debug(f"🧪 [Developer] Attempting poster download: {mock_data['poster_url']}")
                    
                    res = curl_requests.get(mock_data["poster_url"], timeout=10, impersonate="chrome", proxy=Settings.get_proxy())
                    
                    if res.status_code == 200:
                        files.append(discord.File(io.BytesIO(res.content), filename="poster.png"))
                        use_attachment_proxy = True
                        if Settings.DEVELOPER_MODE:
                            logger.info(f"🧪 [Developer] 🖼️  Poster downloaded successfully (Size: {len(res.content)} bytes)")
                    else:
                        if Settings.DEVELOPER_MODE:
                            logger.warning(f"🧪 [Developer] ❌ Poster download failed with status: {res.status_code}")
                except Exception as e:
                    logger.error(f"Failed to download poster for attachment: {e}")

            payload = build_notification_payload(
                platform=platform,
                role_id=None, # No ping in test
                series_title=mock_data["title"],
                custom_title=mock_data["custom_title"],
                poster_url=mock_data["poster_url"] if not use_attachment_proxy else None,
                series_url=mock_data["url"],
                series_id=mock_data["series_id"],
                notification_id=9999,
                chapter_id=mock_data.get("chapter_id"),
                chapter_number=mock_data.get("chapter_number"),
                use_attachment_proxy=use_attachment_proxy
            )

            from aiohttp import FormData
            
            # --- Multipart Construction ---
            # To send V2 Components (flags: 32768) with files via raw HTTP, 
            # we must use a 'payload_json' field in a multipart/form-data request.
            data = FormData()
            data.add_field('payload_json', json.dumps(payload))
            
            for i, f in enumerate(files):
                # Ensure the file pointer is at the start
                f.fp.seek(0)
                data.add_field(f'files[{i}]', f.fp, filename=f.filename, content_type='image/png' if f.filename.endswith('.png') else 'application/octet-stream')

            route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=ctx.channel.id)
            
            if Settings.DEVELOPER_MODE:
                logger.info(f"🧪 [Developer] Dispatching V2 Component via Multipart (Files: {len(files)})")

            # We use 'data' instead of 'json' to trigger multipart encoding in the internal HTTP client
            await self.bot.http.request(route, data=data)
        except Exception as e:
            logger.error(f"❌ Failed to render V2 Component: {e}")
            await ctx.send(f"❌ Failed to render V2 Component: `{e}`")

    @commands.command(name="test_hiatus_ui", aliases=["hiatus_ui"])
    async def test_hiatus_ui(self, ctx, *, arg: str = None):
        """
        Usage:
          $test_hiatus_ui                          → jumptoon mock
          $test_hiatus_ui piccoma                  → piccoma mock  
          $test_hiatus_ui https://piccoma.com/...  → live scrape
        """
        if arg and arg.startswith("http"):
            # Live scrape mode
            msg = await ctx.send(f"🔍 Scraping `{arg}`...")
            try:
                provider = self.pm.get_provider_for_url(arg)
                if not provider:
                    return await msg.edit(content=f"❌ Unsupported URL: `{arg}`")
                
                data = await provider.get_series_info(arg, fast=True)
                title, _, _, image_url, series_id, _, _, _, _ = data

                # Detect platform from URL
                url_lower = arg.lower()
                if "mechacomic.jp" in url_lower: platform = "mecha"
                elif "jumptoon.com" in url_lower: platform = "jumptoon"
                elif "piccoma.com" in url_lower: platform = "piccoma"
                elif "kakao.com" in url_lower: platform = "kakao"
                else: platform = "jumptoon"

                files = []
                use_attachment_proxy = False
                if image_url:
                    try:
                        from curl_cffi import requests as curl_requests
                        import io
                        res = curl_requests.get(image_url, timeout=10, impersonate="chrome")
                        if res.status_code == 200:
                            files.append(discord.File(io.BytesIO(res.content), filename="poster.png"))
                            use_attachment_proxy = True
                    except Exception as e:
                        logger.warning(f"Poster fetch failed: {e}")

                payload = build_hiatus_notification_payload(
                    platform=platform,
                    role_id=None,
                    series_title=title,
                    custom_title=None,
                    poster_url=image_url,
                    series_url=arg,
                    series_id=series_id,
                    notification_id=9999,
                    use_attachment_proxy=use_attachment_proxy,
                )

                await msg.delete()

                route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=ctx.channel.id)
                if files:
                    from aiohttp import FormData
                    import json as json_lib
                    form = FormData()
                    form.add_field("payload_json", json_lib.dumps(payload), content_type="application/json")
                    for idx, f in enumerate(files):
                        f.fp.seek(0)
                        form.add_field(f"files[{idx}]", f.fp, filename=f.filename, content_type="image/png")
                    url_str = f"https://discord.com/api/v10/channels/{ctx.channel.id}/messages"
                    headers = {"Authorization": f"Bot {self.bot.http.token}"}
                    async with self.bot.http._HTTPClient__session.post(url_str, data=form, headers=headers) as resp:
                        if resp.status not in (200, 201, 204):
                            await ctx.send(f"❌ Failed: `{resp.status} {await resp.text()}`")
                else:
                    await self.bot.http.request(route, json=payload)

                logger.info(f"🧪 [Discovery] Hiatus UI live preview by {ctx.author} ({arg})")

            except Exception as e:
                logger.error(f"Hiatus UI live test error: {e}")
                await ctx.send(f"❌ Failed: `{e}`")

        else:
            # Mock mode (existing logic)
            platform = (arg or "jumptoon").lower()
            if "mecha" in platform: platform = "mecha"
            elif "piccoma" in platform: platform = "piccoma"
            elif "kakao" in platform: platform = "kakao"
            else: platform = "jumptoon"

            mocks = {
                "jumptoon": {"title": "The Rebirth of the S-Grade Ranker", "series_id": "JT00138", "url": "https://jumptoon.com/series/JT00138", "poster_url": "https://assets.jumptoon.com/series/JT00138/episode/11/v2_0_0/20260310020123/83253720a97dd8e1caa50e3f75ec075b0978567ebcff56773dc1dc319b06ee0f.webp"},
                "piccoma":  {"title": "Solo Leveling", "series_id": "5535", "url": "https://piccoma.com/web/product/5535", "poster_url": None},
                "mecha":    {"title": "Blue Lock", "series_id": "123456", "url": "https://mechacomic.jp/books/123456", "poster_url": None},
            }
            mock = mocks.get(platform, mocks["jumptoon"])

            payload = build_hiatus_notification_payload(
                platform=platform,
                role_id=None,
                series_title=mock["title"],
                custom_title=None,
                poster_url=mock["poster_url"],
                series_url=mock["url"],
                series_id=mock["series_id"],
                notification_id=9999,
            )

            try:
                route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=ctx.channel.id)
                await self.bot.http.request(route, json=payload)
                logger.info(f"🧪 [Discovery] Hiatus UI preview sent by {ctx.author} (platform: {platform})")
            except Exception as e:
                await ctx.send(f"❌ Failed to render hiatus UI: `{e}`")




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
        platform = parts[2].lower()
        if "mecha" in platform: platform = "mecha"
        elif "kakao" in platform: platform = "kakao"
        elif "jumptoon" in platform: platform = "jumptoon"
        elif "piccoma" in platform: platform = "piccoma"
        
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
                 view.active_tasks.append(t) # 🟢 Fix: Link task to view for UI tracking
                 await self.bot.task_queue.add_task(t)
            
            asyncio.create_task(view.monitor_tasks())
            
        except Exception as e:
            logger.error(f"Download Chapter Error: {e}")
            await self.bot.dispatch_error(e, interaction=interaction, event=f"Discovery: {title if 'title' in locals() else 'Series'}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to start download: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to start download: `{e}`", ephemeral=True)

    async def _handle_preview_first(self, interaction: discord.Interaction, platform: str, series_id: str):
        await interaction.response.defer(ephemeral=True)
        try:
            provider = self.pm.get_provider(platform)
            url = f"{provider.BASE_URL}{provider.SERIES_PATH}{series_id}"
            
            # Fetch Series Info (Normal fetch to ensure sorting)
            data = await provider.get_series_info(url)
            title, total_chapters, chapter_list, image_url, s_id, r_day, r_time, status_label, genre_label = data
            
            if not chapter_list:
                return await interaction.followup.send("⚠️ No chapters found for preview.", ephemeral=True)
            
            first_ch = chapter_list[0]
            
            # Queue for Download via BatchController
            from app.services.batch_controller import BatchController
            controller = BatchController(self.bot)
            
            # 🟢 S-GRADE: Use a temporary shim to capture existing links from prepare_batch
            class TempView:
                def __init__(self):
                    self.existing_links = {}
                    self.active_tasks = []
                    self._full_scan_task = None
                    self.req_id = f"PRE-{series_id[:4]}"
                def trigger_refresh(self): pass
                
            temp_view = TempView()

            # 🟢 Resolve folder structure and check existence
            tasks = await controller.prepare_batch(
                interaction=interaction,
                selected_indices=[0], # Preview always first chapter
                all_chapters=chapter_list,
                title=title,
                url=url,
                view_ref=temp_view,
                series_id=series_id
            )
            
            # 🟢 CASE A: Chapter already exists on Google Drive — Swap instantly!
            if not tasks:
                # Resolve the key used in batch_controller (chapter_str)
                chapter_str = first_ch.get('notation') or first_ch.get('number_text') or "1"
                existing = temp_view.existing_links.get(chapter_str)
                drive_link = existing.get("link") if isinstance(existing, dict) else existing
                
                if drive_link:
                    logger.info(f"⚡ [Preview] Ch.1 already exists for '{title}'. Swapping button immediately.")
                    from app.bot.common.notification_builder import build_new_series_notification_payload_with_drive
                    new_payload = build_new_series_notification_payload_with_drive(
                        platform=platform,
                        series_title=title,
                        poster_url=image_url,
                        series_url=url,
                        series_id=series_id,
                        drive_url=drive_link,
                    )
                    route = discord.http.Route(
                        'PATCH',
                        '/channels/{channel_id}/messages/{message_id}',
                        channel_id=interaction.channel_id,
                        message_id=interaction.message.id,
                    )
                    await self.bot.http.request(route, json=new_payload)
                    await interaction.followup.send("✅ Preview link is already available!", ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ Chapter marked as existing but no link found in Drive.", ephemeral=True)
                return

            # 🟢 CASE B: Chapter needs to be downloaded
            await interaction.followup.send("⏳ Downloading Ch.1 for preview...", ephemeral=True)
            
            task = tasks[0]
            await self.bot.task_queue.add_task(task)
            
            # Background: wait for done → patch original message
            asyncio.create_task(
                self._swap_preview_button_on_done(
                    task=task,
                    interaction=interaction,
                    platform=platform,
                    series_id=series_id,
                    series_url=url,
                    series_title=title,
                    poster_url=image_url,
                )
            )
            
        except Exception as e:
            logger.error(f"Preview Error: {e}")
            await interaction.followup.send(f"❌ Failed to start preview: `{e}`", ephemeral=True)

    async def _swap_preview_button_on_done(self, *, task, interaction, platform, series_id, series_url, series_title, poster_url):
        """Waits for task_completed event via EventBus, then patches original notification msg with Drive link button."""
        import asyncio
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        # 🟢 Define the event handlers
        async def on_task_completed(event_data):
            # event_data can be a task object (local) or a dict (bridged from Redis)
            e_req_id = event_data.req_id if hasattr(event_data, "req_id") else event_data.get("req_id")
            if e_req_id == task.req_id and not future.done():
                future.set_result(True)

        async def on_task_failed(failed_task, error):
            e_req_id = failed_task.req_id if hasattr(failed_task, "req_id") else failed_task.get("req_id")
            if e_req_id == task.req_id and not future.done():
                future.set_exception(Exception(f"Task failed: {error}"))

        # 🟢 Subscribe to the global EventBus
        EventBus.subscribe("task_completed", on_task_completed)
        EventBus.subscribe("task_failed", on_task_failed)

        try:
            # 10 minute timeout for preview download + upload
            await asyncio.wait_for(future, timeout=600)
            
            # Event fired — fetch share_link from Redis (stored by the worker on completion)
            redis_key = f"verzue:share_link:{task.req_id}"
            # Use self.bot.redis_brain if it exists, otherwise resolve it
            redis = getattr(self.bot, 'redis_brain', None) or RedisManager()
            drive_link = await redis.client.get(redis_key)

            if not drive_link:
                logger.warning(f"⚠️ [Preview] Task {task.req_id} completed but no Drive link found in Redis.")
                return

            # PATCH the original notification message with the new button
            from app.bot.common.notification_builder import build_new_series_notification_payload_with_drive
            new_payload = build_new_series_notification_payload_with_drive(
                platform=platform,
                series_title=series_title,
                poster_url=poster_url,
                series_url=series_url,
                series_id=series_id,
                drive_url=drive_link,
            )

            route = discord.http.Route(
                'PATCH',
                '/channels/{channel_id}/messages/{message_id}',
                channel_id=interaction.channel_id,
                message_id=interaction.message.id,
            )
            await self.bot.http.request(route, json=new_payload)
            logger.info(f"✅ [Preview] Successfully swapped button for '{series_title}' ({task.req_id})")

        except asyncio.TimeoutError:
            logger.warning(f"⏰ [Preview] Timeout waiting for task {task.req_id}")
        except Exception as e:
            logger.error(f"❌ [Preview] Failed to swap button: {e}")
        finally:
            # 🟢 CRITICAL: Unsubscribe to prevent memory leaks and redundant listener triggers
            EventBus.unsubscribe("task_completed", on_task_completed)
            EventBus.unsubscribe("task_failed", on_task_failed)

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
                 view.active_tasks.append(t)
                 await self.bot.task_queue.add_task(t)

            asyncio.create_task(view.monitor_tasks())
            
        except Exception as e:
            logger.error(f"Download All Error: {e}")
            await self.bot.dispatch_error(e, interaction=interaction, event=f"Discovery: {title if 'title' in locals() else 'Series'}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Failed to start bulk download: `{e}`", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed to start bulk download: `{e}`", ephemeral=True)

    @commands.command(name="check_new", aliases=["check_new_series"])
    async def manual_new_series_check(self, ctx, platform: str = None):
        """Forces a check for new series on specific or all platforms (Jumptoon, Piccoma, MechaComic)."""
        # 1. Feedback
        msg = await ctx.send(f"🔍 **Starting New Series Detection Sweep...**")
        
        # 2. Get Poller (Resolve from main bot if called from helper)
        resolved_bot = self.bot if hasattr(self.bot, 'auto_poller') else getattr(self.bot, 'main_bot', self.bot)
        poller = getattr(resolved_bot, "auto_poller", None)

        if not poller:
            return await msg.edit(content="❌ `AutoPoller` not initialized correctly on this bot instance.")
            
        # 3. Choose Platforms (Supported: jumptoon, piccoma, mecha)
        from app.tasks.poller import JUMPTOON_NEW_SERIES_CHANNEL_ID
        all_platforms = ["jumptoon", "piccoma", "mecha"]
        
        if platform:
            p_low = platform.lower()
            if "mecha" in p_low: platform = "mecha"
            elif "kakao" in p_low: platform = "kakao"
            elif "jumptoon" in p_low: platform = "jumptoon"
            elif "piccoma" in p_low: platform = "piccoma"
            
            if platform not in all_platforms:
                 return await msg.edit(content=f"❌ Unsupported platform: `{platform}`. Supported: `jumptoon`, `piccoma`, `mecha`.")
            targets = [platform]
        else:
            targets = all_platforms
        
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
