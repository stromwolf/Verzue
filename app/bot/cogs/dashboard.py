import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import uuid
import asyncio
import math
import time
from config.settings import Settings
from app.models.chapter import TaskStatus

logger = logging.getLogger("Dashboard")

class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        """Phase 1: Launch the V2 Dashboard using raw API payloads."""
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        payload = {
            "type": 4, # MESSAGE_WITH_SOURCE
            "data": {
                "flags": 32768, # 🟢 MAGIC FLAG FOR V2 COMPONENTS
                "components": [
                    {
                        "type": 17, # CONTAINER
                        "components": [
                            {
                                "type": 9, # SECTION COMPONENT
                                "components": [
                                    {
                                        "type": 10, # TEXT DISPLAY
                                        "content": f"# Dashboard of {scan_name}"
                                    }
                                ],
                                "accessory": {
                                    "type": 2, 
                                    "style": 4, 
                                    "emoji": {"name": "✖️"}, 
                                    "custom_id": "btn_close_main_dash"
                                }
                            },
                            {
                                "type": 14, # SEPARATOR
                                "divider": True,
                                "spacing": 1
                            },
                            {
                                "type": 10,
                                "content": "## Platform Lists\n**Available Platforms**\n> * <:Mechacomic:1478369141957333083> Mecha Comic\n> * <:Jumptoon:1478367963928068168> Jumptoon\n> * <:Piccoma:1478368704164134912> Piccoma\n\n**Coming Soon Platforms**\n> * <:KakaoPage:1478366505640001566> KakaoPage\n> * <:KuaikanManhua:1478368412609679380> Kuaikan Manhua\n> * <:acqq:1478369616660140082> AC.QQ"
                            },
                            {
                                "type": 10,
                                "content": "## Your Commands"
                            },
                            {
                                "type": 1, # ACTION ROW
                                "components": [
                                    {
                                        "type": 3, # SELECT
                                        "custom_id": "v2_platform_select",
                                        "placeholder": "Select Platform",
                                        "options": [
                                            {"label": "Mecha Comic", "value": "Mecha Comic", "emoji": {"id": "1478369141957333083", "name": "Mechacomic"}},
                                            {"label": "Jumptoon", "value": "Jumptoon", "emoji": {"id": "1478367963928068168", "name": "Jumptoon"}},
                                            {"label": "Piccoma", "value": "Piccoma", "emoji": {"id": "1478368704164134912", "name": "Piccoma"}}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        try:
            route = discord.http.Route(
                'POST', '/interactions/{interaction_id}/{interaction_token}/callback',
                interaction_id=interaction.id,
                interaction_token=interaction.token
            )
            await self.bot.http.request(route, json=payload)
        except discord.NotFound:
            pass # 🟢 Silently ignore "Unknown Interaction" on double-clicks
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions."""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            
            # --- Platform Selection Modal ---
            if custom_id == "v2_platform_select":
                platform = interaction.data["values"][0]
                modal_payload = {
                    "type": 9,
                    "data": {
                        "custom_id": f"v2_modal_{platform}",
                        "title": f"{platform} Extractor",
                        "components": [
                            {
                                "type": 18,
                                "label": "Choose Action",
                                "component": {
                                    "type": 21,
                                    "custom_id": "action_radio",
                                    "options": [
                                        {"label": "Download Chapters", "value": "download", "default": True},
                                        {"label": "Add Subscription", "value": "subscribe"}
                                    ],
                                    "required": True
                                }
                            },
                            {
                                "type": 18,
                                "label": f"Add {platform} link here:",
                                "component": {
                                    "type": 4, "custom_id": "url_input", "style": 1,
                                    "placeholder": f"Paste {platform} URL...", "required": True
                                }
                            }
                        ]
                    }
                }
                try:
                    await self.bot.http.request(
                        discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                        json=modal_payload
                    )
                except discord.HTTPException as e:
                    # 🟢 Mute "Already acknowledged" (40060) and "Unknown interaction" (10062) 
                    if e.code in [40060, 10062]: 
                        pass
                    else:
                        logger.error(f"Modal launch error: {e}")

            # --- Subscription Hook (Button that opens the 2nd modal) ---
            elif custom_id.startswith("v2_btn_sub_trigger_"):
                # Format: v2_btn_sub_trigger_{platform}|{url}
                parts = custom_id.replace("v2_btn_sub_trigger_", "").split("|", 1)
                platform = parts[0]
                url = parts[1] if len(parts) > 1 else ""
                await self.launch_subscribe_modal(interaction, platform, url)

            # --- Universal Dashboard Navigation & Actions ---
            elif any(custom_id.startswith(p) for p in ["btn_open_menu_", "mode_select_", "page_select_", "btn_start_", "btn_cancel_", "btn_clear_", "btn_error_retry_"]):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view: 
                    error_msg = (
                        "❌ **Session Expired or Process Conflict.**\n\n"
                        "This can happen for two reasons:\n"
                        "1. **Timeout**: The session was inactive for more than 15 minutes.\n"
                        "2. **Process Conflict**: Multiple bots are running. **Please kill all `main.py` processes and run only one.**"
                    )
                    return await interaction.response.send_message(error_msg, ephemeral=True)
                
                view.interaction = interaction
                view.last_interaction_time = time.time() # 🔄 Reset session timer on every interaction

                # NEW: Error Retry Logic
                if custom_id.startswith("btn_error_retry_"):
                    # 1. Immediate Ephemeral Feedback
                    apology = (
                        "We apologize for the inconvenience. I'll re-download this chapter and get back to you soon."
                    )
                    await interaction.response.send_message(apology, ephemeral=True)
                    view.retry_active = True # 🟡 Flag for monitor_tasks to send fresh notification
                    
                    # 2. Forensic Deletion (Remove existing folders on Drive)
                    uploader = self.bot.task_queue.uploader
                    if uploader:
                        logger.info(f"[{req_id}] 🗑️ Retrying: Deleting existing Drive assets...")
                        for task in view.active_tasks:
                            # Search for folder in MAIN
                            folder_name = task.folder_name
                            main_id = task.main_folder_id or Settings.GDRIVE_ROOT_ID
                            existing_id = uploader.find_folder(folder_name, main_id)
                            if existing_id: uploader.delete_file(existing_id)
                            
                            # Search for [Uploading] version
                            temp_name = f"[Uploading] {folder_name}"
                            temp_id = uploader.find_folder(temp_name, main_id)
                            if temp_id: uploader.delete_file(temp_id)

                    # 3. Reset View State
                    view.processing_mode = True
                    view.phases["download"] = "loading"
                    view.final_link = None
                    view.trigger_refresh()
                    
                    # 4. Re-queue tasks
                    new_tasks = []
                    for t in view.active_tasks:
                        # Reset task object
                        t.status = TaskStatus.QUEUED
                        t.pre_created_folder_id = None
                        new_tasks.append(await self.bot.task_queue.add_task(t))
                    
                    view.active_tasks = new_tasks
                    asyncio.create_task(view.monitor_tasks())
                    return

                # A. Clear Selections (Cancel SR)
                if custom_id.startswith("btn_clear_"):
                    view.selected_indices.clear()
                    return await view.update_view(interaction)

                # B. Open Selection Sub-Menu (The Radio Group Modal)
                if custom_id.startswith("btn_open_menu_"):
                    new_ch = next((ch for ch in view.all_chapters if ch.get('is_new')), None)
                    if new_ch:
                        ch_not = new_ch.get("notation", "Ch").strip()
                        ch_tit = new_ch.get("title", "").strip()
                        latest_desc = f"[NEW] {ch_not} - {ch_tit}- released!"
                    else:
                        latest_desc = "No New Chapter released."
                    
                    options = [
                        {"label": "SR", "description": "Select all available chapters.", "value": "all"},
                        {"label": "Select", "description": "Add custom range of chapters.", "value": "custom"}
                    ]
                    # The Latest option is added separately so we can potentially truncate the description since it has a 100 char limit
                    options.append({
                        "label": "Latest", 
                        "description": latest_desc[:100], 
                        "value": "latest"
                    })
                    
                    range_str = ""
                    if view.selected_indices:
                        idxs = sorted(list(view.selected_indices))
                        ranges, s, p = [], idxs[0], idxs[0]
                        for i in idxs[1:]:
                            if i == p + 1: p = i
                            else:
                                ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                                s = p = i
                        ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                        range_str = ", ".join(ranges)

                    modal_payload = {
                        "type": 9,
                        "data": {
                            "custom_id": f"modal_select_{view.req_id}",
                            "title": "Select Chapters",
                            "components": [
                                {
                                    "type": 18,
                                    "label": "Choose Selection Mode",
                                    "component": {
                                        "type": 21,
                                        "custom_id": "mode_radio",
                                        "options": options,
                                        "required": True
                                    }
                                },
                                {
                                    "type": 18,
                                    "label": "Custom Range (e.g., 1-5, 10)",
                                    "description": "Only used if 'Select' is chosen above.",
                                    "component": {
                                        "type": 4, "custom_id": "range_input", "style": 1,
                                        "value": range_str,
                                        "placeholder": "Optional, leave blank if not using Select", "required": False
                                    }
                                }
                            ]
                        }
                    }
                    try:
                        return await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=modal_payload)
                    except discord.HTTPException as e:
                        if e.code in [10062, 40060]: pass
                        else: logger.error(f"Failed to open selection menu: {e}")

                # B. Deprecated Handler for old dropdown (safeguard)
                elif custom_id.startswith("mode_select_"):
                    # Should no longer be triggered, log and ignore
                    logger.warning("Received deprecated mode_select_ component interaction")
                    return await interaction.response.defer()

                # C. Page Navigation
                elif custom_id.startswith("page_select_"):
                    # 🟢 DEFER IMMEDIATELY: Lazy loading can take > 3 seconds
                    await interaction.response.defer(ephemeral=True)
                    view.page = int(interaction.data.get("values", ["1"])[0])
                    
                    # --- NEW LOGIC FOR DYNAMIC JUMPTOON SCALING ---
                    req_ch_index = view.page * view.per_page
                    if req_ch_index > len(view.all_chapters) and getattr(view, 'total_chapters', 0) > len(view.all_chapters):
                        if view.service_type in ["jumptoon", "mecha"]:
                            scraper = self.bot.task_queue.scraper_registry.get_scraper(view.url)
                            
                            # 🟢 Dynamic Math: Jumptoon (30/pg) vs Mecha (10/pg)
                            pg_size = 30 if view.service_type == "jumptoon" else 10
                            target_jt_page = math.ceil(req_ch_index / pg_size)
                            last_jt_page = math.ceil(getattr(view, 'total_chapters', 0) / pg_size)
                            
                            try:
                                logger.info(f"[{req_id}] ⚡ Lazy Loading: Fetching up to {view.service_type} page {target_jt_page}...")
                                seen_ids = {ch['id'] for ch in view.all_chapters}
                                
                                # 🟢 Handle both Async and Sync Scrapers
                                if asyncio.iscoroutinefunction(scraper.fetch_more_chapters):
                                    new_chaps = await scraper.fetch_more_chapters(view.url, target_jt_page, seen_ids, [last_jt_page] if last_jt_page > 1 else [])
                                else:
                                    new_chaps = await asyncio.to_thread(scraper.fetch_more_chapters, view.url, target_jt_page, seen_ids, [last_jt_page] if last_jt_page > 1 else [])
                                
                                if new_chaps:
                                    view.all_chapters.extend(new_chaps)
                                    logger.info(f"[{req_id}] ✅ Lazy Loading: Added {len(new_chaps)} new chapters to memory.")
                            except Exception as e:
                                logger.error(f"[{req_id}] ❌ Lazy Loading failed: {e}")

                    # 🟢 Trigger Refresh: Don't pass 'interaction' here because we already deferred!
                    # Passing None forces the view to use the PATCH /webhooks route.
                    await view.update_view()

                # D. Cancel Session
                elif custom_id.startswith("btn_cancel_"):
                    if view.service_type == "mecha": view.bot.task_queue.scraper_registry.browser.dec_session()
                    UniversalDashboard.active_views.pop(req_id, None)
                    await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={"type": 6})
                    try: await interaction.message.delete()
                    except: pass

                # E. Start Batch Process
                elif custom_id.startswith("btn_start_"):
                    view.processing_mode = True
                    view.phases["analyze"] = "loading"
                    await view.update_view(interaction)
                    
                    asyncio.create_task(view.monitor_tasks())
                    from app.services.batch_controller import BatchController
                    tasks = await BatchController(self.bot).prepare_batch(interaction, sorted(list(view.selected_indices)), view.all_chapters, view.title, view.url, view_ref=view, series_id=view.series_id)
                    if tasks:
                        view.phases.update({"analyze":"done","purchase":"done","download":"loading"})
                        view.active_tasks = [await self.bot.task_queue.add_task(t) for t in tasks]
                        view.trigger_refresh()

        # --- Modal Submissions (URL Entry & Range Picker) ---
        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            
            if custom_id.startswith("modal_select_"):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view: return
                view.last_interaction_time = time.time() # 🔄 Reset session timer on modal submit
                
                range_val = ""
                mode_val = "custom"
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    if inner.get("custom_id") == "range_input": range_val = inner.get("value", "")
                    elif inner.get("custom_id") == "mode_radio": mode_val = inner.get("value", "custom")

                view.selected_indices.clear()
                
                if mode_val == "all":
                    view.selected_indices = set(range(len(view.all_chapters)))
                elif mode_val == "latest":
                    idx = next((i for i, ch in enumerate(view.all_chapters) if ch.get('is_new')), None)
                    if idx is not None: 
                        view.selected_indices = {idx}
                    else:
                        error_payload = {
                            "type": 4, 
                            "data": {
                                "flags": 64, 
                                "content": "⛔ **No New Releases Found**\nThis series currently doesn't have any newly released chapters. Please use the **SR** or **Select** options instead."
                            }
                        }
                        try:
                            return await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=error_payload)
                        except: return
                elif mode_val == "custom" and range_val:
                    try:
                        parts = range_val.replace(" ", "").split(",")
                        for p in parts:
                            if "-" in p:
                                s, e = map(int, p.split("-"))
                                view.selected_indices.update(k-1 for k in range(s, e+1) if 1 <= k <= len(view.all_chapters))
                            elif p.isdigit():
                                k = int(p)
                                if 1 <= k <= len(view.all_chapters): view.selected_indices.add(k-1)
                    except: pass
                    
                await view.update_view(interaction)

            elif custom_id.startswith("v2_modal_subscribe_"):
                await self.handle_subscribe_modal(interaction, custom_id)

            elif custom_id.startswith("v2_modal_"):
                await self.handle_platform_modal(interaction, custom_id)

    async def handle_custom_modal_trigger(self, interaction, view):
        """Deprecated."""
        pass

    async def handle_platform_modal(self, interaction, custom_id):
        """Processes the platform URL submission modal."""
        platform = custom_id.replace("v2_modal_", "")
        url, action = "", "download"
        
        for row in interaction.data.get("components", []):
            inner = row.get("component", {})
            if inner.get("custom_id") == "action_radio": action = inner.get("value", "download") 
            elif inner.get("custom_id") == "url_input": url = inner.get("value", "")
        
        platform_domains = {"Mecha Comic": "mechacomic.jp", "Jumptoon": "jumptoon.com", "KakaoPage": "kakao.com", "Kuaikan Manhua": "kuaikanmanhua.com", "Piccoma": "piccoma.com", "AC.QQ": "ac.qq.com"}
        expected_domain = platform_domains.get(platform)
        
        if expected_domain and expected_domain not in url.lower():
            await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={
                "type": 4, "data": {"flags": 64, "content": f"⛔ **Protocol Violation**\nExpected `{expected_domain}` link."}
            })
            return 

        if action == "subscribe":
            # Respond with a V2 message containing a button to trigger the second modal
            # Chaining modals directly is not allowed by Discord
            trigger_payload = {
                "type": 4,
                "data": {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [
                            {
                                "type": 10,
                                "content": f"✅ **Series Recognized!**\nTo complete the subscription for **{platform}**, click the button below to choose your target channel."
                            },
                            {
                                "type": 1,
                                "components": [{
                                    "type": 2, "style": 1, 
                                    "label": "Configure Channel & Finish",
                                    "custom_id": f"v2_btn_sub_trigger_{platform}|{url}"
                                }]
                            }
                        ]
                    }]
                }
            }
            try:
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=trigger_payload
                )
            except Exception as e:
                logger.error(f"Failed to send subscribe trigger: {e}")
            return

        # 🟢 THE FIX: Send the Analyzing message as a V2 Component so there is no root "content"
        # 🟢 THE FIX: Use type 10 (Text) directly inside the Container. No type 9!
        analyzing_payload = {
            "type": 4, 
            "data": {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10, # <--- CHANGED FROM 9 TO 10
                        "content": f"🔍 **Analyzing {platform} Link:**\n`{url}`\n*Fetching metadata, please wait...*"
                    }]
                }]
            }
        }
        
        try:
            route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
            await self.bot.http.request(route, json=analyzing_payload)
        except discord.HTTPException as e:
            if e.code in [10062, 40060]: 
                # Already acknowledged (e.g., from double clicks/lag), continue scraping but use PATCH later
                pass
            else:
                logger.error(f"Interaction expired: {e}")
                return
        except Exception as e:
            logger.error(f"Interaction expired: {e}")
            return

        try:
            from app.core.logger import req_id_context
            from app.bot.common.view import UniversalDashboard
            req_id = str(uuid.uuid4())[:8].upper()
            token = req_id_context.set(req_id)
            
            scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=("mecha" in platform.lower()))
            logger.info(f"[{req_id}] 🚀 Handoff: Extraction starting for {platform}...")
            
            # 🟢 Handle both Async and Sync Scrapers
            if asyncio.iscoroutinefunction(scraper.get_series_info):
                data = await scraper.get_series_info(url)
            else:
                try:
                    data = await asyncio.to_thread(scraper.get_series_info, url)
                except Exception as e:
                    if platform == "mecha":
                        logger.warning(f"[Dashboard] Mecha API failed ({e}), falling back to Web Scraper...")
                        web_scraper = self.bot.task_queue.scraper_registry.web_scraper
                        data = await web_scraper.get_series_info(url)
                    else:
                        raise e
                
            logger.info(f"[{req_id}] ✅ Handoff: Metadata retrieved successfully.")
            title, total_chapters, chapter_list, image_url, series_id = data
            
            ctx_data = {'url': url, 'title': title, 'chapters': chapter_list, 'total_chapters': total_chapters, 'image_url': image_url, 'series_id': series_id, 'req_id': req_id, 'user': interaction.user}
            service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
            
            view = UniversalDashboard(self.bot, ctx_data, service_type)
            view.interaction = interaction
            
            # 🟢 NO "content": "" here!
            payload_data = {"flags": 32768, "components": view.build_v2_payload()}
            
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=payload_data)
            
            if service_type == "mecha":
                asyncio.create_task(asyncio.to_thread(self.bot.task_queue.scraper_registry.browser.warmup))
                
        except Exception as e:
            logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
            err = str(e).splitlines()[0] if str(e) else "Unknown Error"
            
            # Match V2 format for errors so Discord doesn't crash on the PATCH
            # 🟢 Match V2 format for errors so Discord doesn't crash on the PATCH
            error_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10, # <--- CHANGED FROM 9 TO 10
                        "content": f"❌ **Extraction Failed:**\n`{err}`"
                    }]
                }]
            }
            try:
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=error_payload)
            except:
                pass
        finally:
            try: req_id_context.reset(token)
            except: pass

    async def launch_subscribe_modal(self, interaction, platform, url):
        """Launches the actual subscription modal (called from button click)."""
        subscribe_modal_payload = {
            "type": 9,
            "data": {
                "custom_id": f"v2_modal_subscribe_{platform}",
                "title": f"Subscribe to {platform}",
                "components": [
                    {
                        "type": 18,
                        "label": "Confirm Series URL",
                        "component": {
                            "type": 4, "custom_id": "url_input", "style": 1,
                            "value": url,
                            "required": True
                        }
                    },
                    {
                        "type": 18,
                        "label": "Auto-Download Target Channel ID",
                        "component": {
                            "type": 4, "custom_id": "channel_input", "style": 1,
                            "placeholder": "e.g. 12345678901234567",
                            "required": True
                        }
                    }
                ]
            }
        }
        try:
            await self.bot.http.request(
                discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                json=subscribe_modal_payload
            )
        except Exception as e:
            logger.error(f"Failed to open final subscribe modal: {e}")

    async def handle_subscribe_modal(self, interaction, custom_id):
        """Processes the secondary subscription modal where user inputs the channel ID."""
        platform = custom_id.replace("v2_modal_subscribe_", "")
        url, channel_input = "", ""

        for row in interaction.data.get("components", []):
            inner = row.get("component", {})
            if inner.get("custom_id") == "url_input": url = inner.get("value", "")
            elif inner.get("custom_id") == "channel_input": channel_input = inner.get("value", "")

        # Target channel validation
        try:
            target_channel_id = int(''.join(c for c in channel_input if c.isdigit()))
        except ValueError:
            return await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={
                "type": 4, "data": {"flags": 64, "content": "❌ **Invalid Channel ID.** Please provide a valid numeric Discord Channel ID."}
            })

        # Send Thinking state
        analyzing_payload = {
            "type": 4, 
            "data": {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10,
                        "content": f"📡 **Setting up Subscription...**\n`{url}`"
                    }]
                }]
            }
        }
        
        try:
            route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
            await self.bot.http.request(route, json=analyzing_payload)
        except Exception:
            return

        # Backend Processing
        try:
            import datetime
            from app.services.group_manager import add_subscription, is_series_subscribed_globally
            
            # Fetch Metadata (using existing scraper logic exactly as dashboard does)
            scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=("mecha" in platform.lower()))
            
            if asyncio.iscoroutinefunction(scraper.get_series_info):
                data = await scraper.get_series_info(url)
            else:
                data = await asyncio.to_thread(scraper.get_series_info, url)
                
            title, total_chapters, chapter_list, image_url, series_id = data

            # Check Global Singularity rule
            is_subbed, existing_group = is_series_subscribed_globally(series_id)
            if is_subbed:
                error_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [{
                            "type": 10,
                            "content": f"⚠️ **Subscription Rejected**\n**{title}** is already being tracked by **{existing_group}**. Series can only have one active subscription globally."
                        }]
                    }]
                }
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                return await self.bot.http.request(route, json=error_payload)

            # Determine Group based on the interaction origin
            guild_id = interaction.guild.id if interaction.guild else 0
            origin_channel_id = interaction.channel.id if interaction.channel else 0
            group_name = Settings.SERVER_MAP.get(origin_channel_id) or Settings.SERVER_MAP.get(guild_id)

            if not group_name:
                error_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [{
                            "type": 10,
                            "content": f"❌ **No Group Profile Linked**\nThis server channel must be linked to a Group Profile via `$cdn-menu` before adding subscriptions."
                        }]
                    }]
                }
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                return await self.bot.http.request(route, json=error_payload)

            # Determine the baseline "last_known_chapter"
            last_known = "0"
            if chapter_list:
                # Set baseline to the VERY LATEST id found, regardless of locked/free
                last_known = str(chapter_list[-1]["id"])

            sub = {
                "series_id": series_id,
                "series_title": title,
                "series_url": url,
                "platform": platform,
                "channel_id": target_channel_id,
                "release_day": None, 
                "last_known_chapter_id": last_known,
                "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "added_by": interaction.user.id
            }

            add_subscription(group_name, sub)

            # Send Success UI
            success_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10,
                        "content": f"✅ **Successfully Subscribed!**\n\n**Series:** `{title}`\n**Profile:** `{group_name}`\n**Target Channel:** <#{target_channel_id}>\n\n⚠️ *Auto-download is currently paused. Please use `$sub-day {title}, <Day>` to set the polling schedule.*"
                    }]
                }]
            }
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=success_payload)
            
            # Send Notification to Target Channel
            target_chan = self.bot.get_channel(target_channel_id)
            if target_chan:
                try:
                    embed = discord.Embed(
                        title="📡 Auto-Download Linked",
                        description=f"This channel has been set as the auto-download destination for **{title}**.\n\n*(Assigned by <@{interaction.user.id}>)*",
                        color=0x3498db
                    )
                    await target_chan.send(embed=embed)
                except Exception as e:
                    logger.warning(f"Could not send target channel notification: {e}")

            # 🟢 SEND ADMIN ALERT
            from app.services.group_manager import get_admin_settings
            admin_settings = get_admin_settings(group_name)
            admin_channel_id = admin_settings.get("channel_id")
            if admin_channel_id:
                admin_chan = self.bot.get_channel(admin_channel_id)
                if admin_chan:
                    role_id = admin_settings.get("role_id")
                    ping_str = f"<@&{role_id}> " if role_id else ""
                    
                    alert_embed = discord.Embed(
                        title="📡 New Subscription Alert",
                        description=(
                            f"**Series:** `{title}`\n"
                            f"**URL:** <{url}>\n"
                            f"**Requested by:** <@{interaction.user.id}>\n"
                            f"**Group:** `{group_name}`\n\n"
                            "⚠️ **Action Required:** Please set the weekly release day for this series using:\n"
                            f"`$sub-day {url}, <Day>`"
                        ),
                        color=0xf1c40f
                    )
                    try:
                        await admin_chan.send(content=ping_str, embed=alert_embed)
                    except Exception as e:
                        logger.warning(f"Could not send admin alert: {e}")

        except Exception as e:
            logger.error(f"Subscription setup failed: {e}", exc_info=True)
            err = str(e).splitlines()[0] if str(e) else "Unknown Error"
            error_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10,
                        "content": f"❌ **Subscription Failed:**\n`{err}`"
                    }]
                }]
            }
            try:
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=error_payload)
            except: pass

            try:
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=error_payload)
            except: pass

            try:
                route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=error_payload)
            except: pass

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))