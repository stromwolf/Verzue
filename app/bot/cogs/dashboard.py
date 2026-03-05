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

            # --- Universal Dashboard Navigation & Actions ---
            elif any(custom_id.startswith(p) for p in ["btn_open_menu_", "mode_select_", "page_select_", "btn_start_", "btn_cancel_"]):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view: return await interaction.response.send_message("❌ Session expired.", ephemeral=True)
                
                view.interaction = interaction

                # A. Open Selection Sub-Menu (The Radio Group)
                if custom_id.startswith("btn_open_menu_"):
                    view.show_selection_menu = True
                    await view.update_view(interaction)

                # B. Handle Selection Mode (SR, Latest, Custom)
                elif custom_id.startswith("mode_select_"):
                    choice = interaction.data.get("values", [None])[0]
                    view.show_selection_menu = False # Close menu after picking
                    
                    if choice == "all":
                        view.selected_indices = set(range(len(view.all_chapters)))
                    elif choice == "latest":
                        # Auto-select the chapter marked 'is_new'
                        idx = next((i for i, ch in enumerate(view.all_chapters) if ch.get('is_new')), None)
                        if idx is not None: view.selected_indices = {idx}
                    elif choice == "custom":
                        # Redirect to the existing modal range picker logic
                        return await self.handle_custom_modal_trigger(interaction, view)
                    
                    await view.update_view(interaction)

                # C. Page Navigation
                elif custom_id.startswith("page_select_"):
                    view.page = int(interaction.data.get("values", ["1"])[0])
                    await view.update_view(interaction)

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
                
                range_val = ""
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    if inner.get("custom_id") == "range_input": range_val = inner.get("value", "")

                view.selected_indices.clear()
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

            elif custom_id.startswith("v2_modal_"):
                await self.handle_platform_modal(interaction, custom_id)

    async def handle_custom_modal_trigger(self, interaction, view):
        """Helper to launch the existing chapter selection modal from the sub-menu."""
        modal_payload = {
            "type": 9,
            "data": {
                "custom_id": f"modal_select_{view.req_id}",
                "title": "Enter Chapter Range",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4, "custom_id": "range_input", "style": 1,
                        "label": "Custom Range (e.g., 1-5, 10)", "required": True
                    }]
                }]
            }
        }
        await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=modal_payload)

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
            await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={
                "type": 4, "data": {"flags": 64, "content": "🚧 **Subscription Coming Soon!**"}
            })
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
        except Exception as e:
            logger.error(f"Interaction expired: {e}")
            return

        try:
            from app.core.logger import req_id_context
            from app.bot.common.view import UniversalDashboard
            req_id = str(uuid.uuid4())[:8].upper()
            token = req_id_context.set(req_id)
            
            scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=("mecha" in platform.lower()))
            data = await asyncio.to_thread(scraper.get_series_info, url)
            title, total_chapters, chapter_list, image_url, series_id = data
            
            ctx_data = {'url': url, 'title': title, 'chapters': chapter_list, 'image_url': image_url, 'series_id': series_id, 'req_id': req_id, 'user': interaction.user}
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

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))