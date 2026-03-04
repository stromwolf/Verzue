import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import uuid
import asyncio
from config.settings import Settings

logger = logging.getLogger("Dashboard")

class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        """Phase 1: Launch the V2 Dashboard using raw API payloads."""
        # 1. Gather Context
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        # 2. Construct Raw V2 JSON Payload
        payload = {
            "type": 4, # MESSAGE_WITH_SOURCE
            "data": {
                "flags": 32768, # 🟢 MAGIC FLAG FOR V2 COMPONENTS
                "components": [
                    {
                        "type": 17, # CONTAINER
                        "components": [
                            {
                                "type": 10, # TEXT DISPLAY
                                "content": f"# Dashboard of {scan_name}"
                            },
                            {
                                "type": 14, # SEPARATOR
                                "divider": True,
                                "spacing": 1
                            },
                            {
                                "type": 10,
                                "content": "## Platform Lists\n**Available Platforms**\n> * <:Mechacomic:1478369141957333083> Mecha Comic\n> * <:Jumptoon:1478367963928068168> Jumptoon\n\n**Coming Soon Platforms**\n> * <:KakaoPage:1478366505640001566> KakaoPage\n> * <:KuaikanManhua:1478368412609679380> Kuaikan Manhua\n> * <:Piccoma:1478368704164134912> Piccoma\n> * <:acqq:1478369616660140082> AC.QQ"
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
                                            {"label": "Jumptoon", "value": "Jumptoon", "emoji": {"id": "1478367963928068168", "name": "Jumptoon"}}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        # Send raw HTTP request to Discord
        try:
            route = discord.http.Route(
                'POST', '/interactions/{interaction_id}/{interaction_token}/callback',
                interaction_id=interaction.id,
                interaction_token=interaction.token
            )
            await self.bot.http.request(route, json=payload)
            
        except discord.NotFound:
            logger.warning("[Dashboard] Interaction timed out. This is normal on startup.")
        except discord.HTTPException as e:
            if e.code == 40060:
                pass # Already acknowledged (Double-click), ignore silently
            else:
                logger.error(f"Failed to send V2 Dashboard: {e}")
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions."""

        # 3. Handle Select Menu Click -> Launch V2 Modal
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            
            if custom_id == "v2_platform_select":
                platform = interaction.data["values"][0]

                modal_payload = {
                    "type": 9, # MODAL
                    "data": {
                        "custom_id": f"v2_modal_{platform}",
                        "title": f"{platform} Extractor",
                        "components": [
                            {
                                "type": 18, # LABEL
                                "label": "Choose Action",
                                "component": {
                                    "type": 21, # RADIO GROUP
                                    "custom_id": "action_radio",
                                    "options": [
                                        {"label": "Download Chapters", "value": "download", "default": True},
                                        {"label": "Add Subscription", "value": "subscribe"}
                                    ],
                                    "required": True
                                }
                            },
                            {
                                "type": 18, # LABEL
                                "label": f"Add {platform} link here:",
                                "component": {
                                    "type": 4, # TEXT INPUT
                                    "custom_id": "url_input",
                                    "style": 1,
                                    "placeholder": f"Paste {platform} URL...",
                                    "required": True
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
                except discord.NotFound:
                    logger.warning("[Dashboard] Modal launch timed out.")
                except discord.HTTPException as e:
                    if e.code != 40060: logger.error(f"Modal launch error: {e}")

            # 5. Handle Universal Dashboard V2 Native Interactions
            elif any(custom_id.startswith(prefix) for prefix in ["btn_select_", "btn_start_", "btn_cancel_", "page_select_", "modal_select_"]):
                req_id = custom_id.split("_")[-1]
                
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view:
                    return await interaction.response.send_message("❌ Session expired or invalid.", ephemeral=True)
                
                view.interaction = interaction

                # A. Change Page (String Select)
                if custom_id.startswith("page_select_"):
                    val = interaction.data.get("values", ["1"])[0]
                    view.page = int(val)
                    await view.update_view(interaction)

                # B. Cancel Extraction
                elif custom_id.startswith("btn_cancel_"):
                    if view.service_type == "mecha": view.bot.task_queue.scraper_registry.browser.dec_session()
                    UniversalDashboard.active_views.pop(req_id, None)
                    payload = {"type": 7, "data": {"content": "❌ **Dashboard Closed**", "components": [], "flags": 0}}
                    route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                    await self.bot.http.request(route, json=payload)

                # C. Start Extraction
                elif custom_id.startswith("btn_start_"):
                    from app.core.logger import req_id_context
                    req_id_context.set(view.req_id)
                    view.processing_mode = True
                    view.phases["analyze"] = "loading"
                    view.sub_status = "Identifying Client"
                    view.purchase_count = 0 
                    await view.update_view(interaction)
                    
                    asyncio.create_task(view.monitor_tasks())
                    from app.services.batch_controller import BatchController
                    controller = BatchController(self.bot)
                    tasks = await controller.prepare_batch(interaction, sorted(list(view.selected_indices)), view.all_chapters, view.title, view.url, view_ref=view, series_id=view.series_id)
                    
                    if tasks:
                        view.phases.update({"analyze":"done","purchase":"done","download":"loading"})
                        actual_tasks = []
                        for t in tasks:
                            t.is_smartoon = True
                            t.series_id_key = view.series_id
                            actual_tasks.append(await self.bot.task_queue.add_task(t))
                        view.active_tasks = actual_tasks
                        view.trigger_refresh()

                # D. Launch Selection Modal (Stateful Pre-fill)
                elif custom_id.startswith("btn_select_"):
                    sel_count = len(view.selected_indices)
                    total_chapters = len(view.all_chapters)
                    
                    current_range = ""
                    def_radio = "select"
                    
                    # Pre-fill logic based on previous selections
                    if sel_count == total_chapters and total_chapters > 0:
                        current_range = f"1-{total_chapters}"
                        def_radio = "sr"
                    elif sel_count > 0:
                        idxs = sorted(list(view.selected_indices))
                        ranges, s, p = [], idxs[0], idxs[0]
                        for i in idxs[1:]:
                            if i == p + 1: p = i
                            else:
                                ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                                s = p = i
                        ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                        current_range = ", ".join(ranges)
                    
                    modal_payload = {
                        "type": 9,
                        "data": {
                            "custom_id": f"modal_select_{req_id}",
                            "title": "Select Chapters",
                            "components": [
                                {
                                    "type": 18,
                                    "label": "Selection Method",
                                    "component": {
                                        "type": 21, # Radio Group
                                        "custom_id": "method_radio",
                                        "options": [
                                            {"value": "sr", "label": "SR", "description": "Selects all available chapters", "default": def_radio == "sr"},
                                            {"value": "select", "label": "Select", "description": "Use the custom range box below", "default": def_radio == "select"}
                                        ]
                                    }
                                },
                                {
                                    "type": 18,
                                    "label": "Enter Custom Range (If SR isn't chosen.)",
                                    "description": "e.g., 1-5, 8, 11-20",
                                    "component": {
                                        "type": 4, # Text Input
                                        "custom_id": "range_input",
                                        "style": 1,
                                        "required": False,
                                        "value": current_range # 🟢 Injects the previously selected range!
                                    }
                                }
                            ]
                        }
                    }
                    route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                    await self.bot.http.request(route, json=modal_payload)

        # 4. Handle V2 Modal Submission
        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            
            # Identify Dashboard selection modals
            if custom_id.startswith("modal_select_"):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view:
                    return await interaction.response.send_message("❌ Session expired or invalid.", ephemeral=True)
                
                method, range_val = "select", ""
                
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    cid = inner.get("custom_id")
                    if cid == "method_radio": method = inner.get("value")
                    elif cid == "range_input": range_val = inner.get("value", "")

                total_chapters = len(view.all_chapters)
                clean_range = range_val.replace(" ", "")

                # 🟢 NEW: Conflict Validation Rule
                if method == "sr" and clean_range and clean_range != f"1-{total_chapters}":
                    error_payload = {
                        "type": 4, # MESSAGE_WITH_SOURCE
                        "data": {
                            "flags": 64, # EPHEMERAL (Invisible)
                            "content": f"⚠️ **Selection Conflict:** You checked **SR** (Select All) but entered a custom range (`{range_val}`).\n\nPlease either check **Select** to use your custom range, or change the range back to `1-{total_chapters}`."
                        }
                    }
                    try:
                        route = discord.http.Route('POST', '/interactions/{interaction_id}/{interaction_token}/callback', interaction_id=interaction.id, interaction_token=interaction.token)
                        await self.bot.http.request(route, json=error_payload)
                    except: pass
                    return # Stop processing so the dashboard doesn't update incorrectly
                
                # Proceed with updating the selection
                view.selected_indices.clear()
                
                if method == "sr":
                    view.selected_indices.update(range(total_chapters))
                elif range_val.strip():
                    # Parse custom range
                    try:
                        parts = clean_range.split(",")
                        for p in parts:
                            if "-" in p:
                                s, e = map(int, p.split("-"))
                                view.selected_indices.update(k-1 for k in range(s, e+1) if 1 <= k <= total_chapters)
                            elif p.isdigit():
                                k = int(p)
                                if 1 <= k <= total_chapters: view.selected_indices.add(k-1)
                    except Exception as e:
                        pass # Ignore malformed text input
                            
                await view.update_view(interaction)

            # Handle platform submission modals
            elif custom_id.startswith("v2_modal_"):
                platform = custom_id.replace("v2_modal_", "")
                
                action = "download"
                url = ""

                # Extract values from the V2 Label > Component nesting
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    cid = inner.get("custom_id")
                    if cid == "action_radio":
                        action = inner.get("value", "download") 
                    elif cid == "url_input":
                        url = inner.get("value", "")

                # STRICT URL VALIDATION
                platform_domains = {
                    "Mecha Comic": "mechacomic.jp",
                    "Jumptoon": "jumptoon.com",
                    "KakaoPage": "kakao.com",
                    "Kuaikan Manhua": "kuaikanmanhua.com",
                    "Piccoma": "piccoma.com",
                    "AC.QQ": "ac.qq.com"
                }
                
                expected_domain = platform_domains.get(platform)
                if expected_domain and expected_domain not in url.lower():
                    error_payload = {
                        "type": 4, # MESSAGE_WITH_SOURCE
                        "data": {
                            "flags": 64, # EPHEMERAL
                            "content": f"⛔ **Protocol Violation**\nYou selected **{platform}**, but provided a link for a different site.\n\nPlease provide a valid `{expected_domain}` link."
                        }
                    }
                    try:
                        route = discord.http.Route('POST', '/interactions/{interaction_id}/{interaction_token}/callback', interaction_id=interaction.id, interaction_token=interaction.token)
                        await self.bot.http.request(route, json=error_payload)
                    except discord.NotFound:
                        pass
                    return 

                # Subscription Check
                if action == "subscribe":
                    msg_payload = {
                        "type": 4, # MESSAGE_WITH_SOURCE
                        "data": {
                            "flags": 64, # EPHEMERAL
                            "content": f"🚧 **Subscription Feature Coming Soon!**\nWe are currently building the tracking database for **{platform}**."
                        }
                    }
                    try:
                        route = discord.http.Route('POST', '/interactions/{interaction_id}/{interaction_token}/callback', interaction_id=interaction.id, interaction_token=interaction.token)
                        await self.bot.http.request(route, json=msg_payload)
                    except discord.NotFound:
                        pass
                    return

                # Download Path
                msg_target = interaction
                try:
                    await interaction.response.send_message(f"🔍 **Analyzing {platform} Link:**\n`{url}`\n*Fetching metadata, please wait...*")
                except discord.NotFound:
                    if interaction.channel:
                        msg_target = await interaction.channel.send(f"🔍 **Analyzing {platform} Link for {interaction.user.mention}:**\n`{url}`\n*Fetching metadata...*")
                    else:
                        return 
                except discord.HTTPException as e:
                    if e.code == 40060: pass

                try:
                    from app.core.logger import req_id_context
                    from app.bot.common.view import UniversalDashboard

                    req_id = str(uuid.uuid4())[:8].upper()
                    token = req_id_context.set(req_id)
                    
                    is_smartoon = "mecha" in platform.lower()
                    scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=is_smartoon)
                    data = await asyncio.to_thread(scraper.get_series_info, url)
                    
                    title, total_chapters, chapter_list, image_url, series_id = data
                    ctx_data = {
                        'url': url, 'title': title, 'chapters': chapter_list,
                        'image_url': image_url, 'series_id': series_id,
                        'req_id': req_id, 'user': interaction.user
                    }
                    service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
                    
                    # 4. Mount Universal Dashboard (Pure V2 JSON)
                    view = UniversalDashboard(self.bot, ctx_data, service_type)
                    view.interaction = interaction
                    
                    payload_data = {"flags": 32768, "components": view.build_v2_payload(), "content": ""}
                    
                    if hasattr(msg_target, 'edit_original_response'):
                        route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                        await self.bot.http.request(route, json=payload_data)
                    else:
                        route = discord.http.Route('PATCH', f'/channels/{msg_target.channel.id}/messages/{msg_target.id}')
                        await self.bot.http.request(route, json=payload_data)
                    
                    if service_type == "mecha":
                        browser = self.bot.task_queue.scraper_registry.browser
                        asyncio.create_task(asyncio.to_thread(browser.warmup))
                        logger.info("🔥 Browser Warmup Triggered (Background)")

                except Exception as e:
                    logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
                    error_text = str(e).splitlines()[0] if str(e) else "Unknown Error"
                    if hasattr(msg_target, 'edit_original_response'):
                        await msg_target.edit_original_response(content=f"❌ **Extraction Failed**\nCould not fetch metadata for `{url}`.\n**Reason:** `{error_text}`")
                    else:
                        await msg_target.edit(content=f"❌ **Extraction Failed for {interaction.user.mention}**\nCould not fetch metadata for `{url}`.\n**Reason:** `{error_text}`")
                finally:
                    try:
                        req_id_context.reset(token)
                    except:
                        pass

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
