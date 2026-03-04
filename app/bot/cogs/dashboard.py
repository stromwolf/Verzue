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
                                "accessory": { # PINS TO THE FAR RIGHT
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
            logger.warning("[Dashboard] Interaction timed out.")
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
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
                                    "type": 4,
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
                except Exception as e:
                    logger.error(f"Modal launch error: {e}")

            elif custom_id == "btn_close_main_dash":
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json={"type": 6}
                )
                try: await interaction.message.delete()
                except: pass

            elif any(custom_id.startswith(prefix) for prefix in ["btn_select_", "btn_start_", "btn_cancel_", "page_select_", "modal_select_", "btn_open_menu_", "mode_select_"]):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view: return await interaction.response.send_message("❌ Session expired.", ephemeral=True)
                view.interaction = interaction

                if custom_id.startswith("page_select_"):
                    view.page = int(interaction.data.get("values", ["1"])[0])
                    await view.update_view(interaction)

                elif custom_id.startswith("btn_open_menu_"):
                    view.show_selection_menu = True
                    await view.update_view(interaction)

                elif custom_id.startswith("mode_select_"):
                    choice = interaction.data.get("values", [None])[0]
                    view.show_selection_menu = False
                    if choice == "all":
                        view.selected_indices = set(range(len(view.all_chapters)))
                        await view.update_view(interaction)
                    elif choice == "latest":
                        new_idx = next((i for i, ch in enumerate(view.all_chapters) if ch.get('is_new')), None)
                        if new_idx is not None:
                            view.selected_indices = {new_idx}
                            await view.update_view(interaction)
                        else:
                            await interaction.response.send_message("❌ No new chapter detected.", ephemeral=True)
                    elif choice == "custom":
                        sel_count = len(view.selected_indices)
                        total_chapters = len(view.all_chapters)
                        current_range = ""
                        def_radio = "select"
                        if sel_count == total_chapters and total_chapters > 0:
                            current_range = f"1-{total_chapters}"; def_radio = "sr"
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
                                            "type": 21, "custom_id": "method_radio",
                                            "options": [
                                                {"value": "sr", "label": "SR", "description": "Selects all", "default": def_radio == "sr"},
                                                {"value": "select", "label": "Select", "description": "Custom range", "default": def_radio == "select"}
                                            ]
                                        }
                                    },
                                    {
                                        "type": 18, "label": "Enter Custom Range",
                                        "component": {
                                            "type": 4, "custom_id": "range_input", "style": 1,
                                            "required": False, "value": current_range
                                        }
                                    }
                                ]
                            }
                        }
                        await self.bot.http.request(
                            discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                            json=modal_payload
                        )

                elif custom_id.startswith("btn_cancel_"):
                    if view.service_type == "mecha": view.bot.task_queue.scraper_registry.browser.dec_session()
                    UniversalDashboard.active_views.pop(req_id, None)
                    await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={"type": 6})
                    try: await interaction.message.delete()
                    except: pass

                elif custom_id.startswith("btn_start_"):
                    from app.core.logger import req_id_context
                    req_id_context.set(view.req_id)
                    view.processing_mode = True
                    view.phases["analyze"] = "loading"
                    view.sub_status = "Identifying Client"; view.purchase_count = 0 
                    await view.update_view(interaction)
                    asyncio.create_task(view.monitor_tasks())
                    from app.services.batch_controller import BatchController
                    controller = BatchController(self.bot)
                    tasks = await controller.prepare_batch(interaction, sorted(list(view.selected_indices)), view.all_chapters, view.title, view.url, view_ref=view, series_id=view.series_id)
                    if tasks:
                        view.phases.update({"analyze":"done","purchase":"done","download":"loading"})
                        actual_tasks = []
                        for t in tasks:
                            t.is_smartoon = True; t.series_id_key = view.series_id
                            actual_tasks.append(await self.bot.task_queue.add_task(t))
                        view.active_tasks = actual_tasks
                        view.trigger_refresh()

        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            if custom_id.startswith("modal_select_"):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view = UniversalDashboard.active_views.get(req_id)
                if not view: return await interaction.response.send_message("❌ Session expired.", ephemeral=True)
                method, range_val = "select", ""
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    if inner.get("custom_id") == "method_radio": method = inner.get("value")
                    elif inner.get("custom_id") == "range_input": range_val = inner.get("value", "")
                
                total_chapters = len(view.all_chapters)
                if method == "sr" and range_val.replace(" ","") and range_val.replace(" ","") != f"1-{total_chapters}":
                    await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={
                        "type": 4, "data": {"flags": 64, "content": f"⚠️ **Selection Conflict:** SR checked but custom range (`{range_val}`) entered."}
                    })
                    return
                view.selected_indices.clear()
                if method == "sr": view.selected_indices.update(range(total_chapters))
                elif range_val.strip():
                    try:
                        parts = range_val.replace(" ", "").split(",")
                        for p in parts:
                            if "-" in p:
                                s, e = map(int, p.split("-"))
                                view.selected_indices.update(k-1 for k in range(s, e+1) if 1 <= k <= total_chapters)
                            elif p.isdigit():
                                k = int(p); view.selected_indices.add(k-1)
                    except: pass
                await view.update_view(interaction)

            elif custom_id.startswith("v2_modal_"):
                platform = custom_id.replace("v2_modal_", ""); action, url = "download", ""
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

                msg_target = interaction
                try: await interaction.response.send_message(f"🔍 **Analyzing {platform} Link...**")
                except:
                    if interaction.channel: msg_target = await interaction.channel.send(f"🔍 **Analyzing {platform} Link...**")
                    else: return 

                try:
                    from app.core.logger import req_id_context
                    from app.bot.common.view import UniversalDashboard
                    req_id = str(uuid.uuid4())[:8].upper(); token = req_id_context.set(req_id)
                    scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=("mecha" in platform.lower()))
                    data = await asyncio.to_thread(scraper.get_series_info, url)
                    title, total_chapters, chapter_list, image_url, series_id = data
                    ctx_data = {'url': url, 'title': title, 'chapters': chapter_list, 'image_url': image_url, 'series_id': series_id, 'req_id': req_id, 'user': interaction.user}
                    service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
                    view = UniversalDashboard(self.bot, ctx_data, service_type); view.interaction = interaction
                    payload_data = {"flags": 32768, "components": view.build_v2_payload(), "content": ""}
                    if hasattr(msg_target, 'edit_original_response'):
                        await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=payload_data)
                    else:
                        await self.bot.http.request(discord.http.Route('PATCH', f'/channels/{msg_target.channel.id}/messages/{msg_target.id}'), json=payload_data)
                    if service_type == "mecha":
                        asyncio.create_task(asyncio.to_thread(self.bot.task_queue.scraper_registry.browser.warmup))
                except Exception as e:
                    logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
                    err = str(e).splitlines()[0] if str(e) else "Unknown Error"
                    if hasattr(msg_target, 'edit_original_response'): await msg_target.edit_original_response(content=f"❌ **Failed**: {err}")
                    else: await msg_target.edit(content=f"❌ **Failed**: {err}")
                finally:
                    try: req_id_context.reset(token)
                    except: pass

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
