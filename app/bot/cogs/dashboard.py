from __future__ import annotations
import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import uuid
import asyncio
import math
import time
import datetime
import json
from typing import TYPE_CHECKING, List, Dict, Any, Optional
from config.settings import Settings
from app.models.chapter import TaskStatus
from app.services.group_manager import load_group, get_group_emoji
from app.services.redis_manager import RedisManager

if TYPE_CHECKING:
    from app.bot.common.view import UniversalDashboard

logger = logging.getLogger("Dashboard")

class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        """Phase 1: Launch the V2 Dashboard using raw API payloads."""
        payload = await self.get_dashboard_payload(interaction)
        
        try:
            route = discord.http.Route(
                'POST', '/interactions/{interaction_id}/{interaction_token}/callback',
                interaction_id=interaction.id,
                interaction_token=interaction.token
            )
            await self.bot.http.request(route, json=payload)
        except discord.NotFound:
            pass 
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")

    async def get_dashboard_payload(self, interaction: discord.Interaction, is_update=False):
        """Standardized payload generator for the refined V2 Dashboard."""
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        # 🟢 S-GRADE: Fetch Today's Weeklies from Redis (O(1))
        from app.services.redis_manager import RedisManager
        redis_brain = RedisManager()
        
        jst_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
        today_name = jst_now.strftime("%A")
        
        # Get hydrated sub data for today
        group_subs = await redis_brain.get_schedule_for_group(scan_name, today_name)
        total_scheduled = len(group_subs)
        
        # Limit to TOP 3 as per user request
        display_subs = group_subs[:3]
        
        logger.info(f"Dashboard Load for {scan_name}: Today={today_name}, Found {total_scheduled} weeklies (showing 3).")
        
        # Custom Logo
        custom_emoji = get_group_emoji(scan_name)
        header_logo = f"{custom_emoji} " if custom_emoji else ""

        # 1. HEADER SECTION
        header_section = {
            "type": 10, # TEXT_DISPLAY
            "content": f"# {header_logo}{scan_name}'s Dashboard"
        }

        # 2. WEEKLIES SECTION
        weeklies_text = "# Today Weeklies\n"
        if display_subs:
            for i, sub in enumerate(display_subs, 1):
                weeklies_text += f"{i}. <#{sub.get('channel_id')}>\n"
        else:
            weeklies_text += "> *No scheduled series subscriptions for today.*"
        
        weeklies_section = {
            "type": 10, # TEXT_DISPLAY
            "content": weeklies_text
        }

        # 4. ACTION ROW (View All)
        action_row = {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "View All Subscriptions",
                    "custom_id": f"v2_btn_view_all_subs_{scan_name}",
                    "emoji": {"name": "📋"}
                }
            ]
        }

        # 3. PLATFORM LIST & SELECT
        platform_list_text = (
            "## Platform Lists\n"
            "> - <:Piccoma:1478368704164134912> Piccoma\n"
            "> - <:Mechacomic:1478369141957333083> Mechacomic\n"
            "> - <:Jumptoon:1478367963928068168> Jumptoon"
        )
        platform_list_section = {
            "type": 10, # TEXT_DISPLAY
            "content": platform_list_text
        }

        platform_select_row = {
            "type": 1, 
            "components": [
                {
                    "type": 3,
                    "custom_id": "v2_platform_select",
                    "placeholder": "Select Platform",
                    "options": [
                        {"label": "Piccoma", "value": "Piccoma", "emoji": {"id": "1478368704164134912", "name": "Piccoma"}},
                        {"label": "Mecha Comic", "value": "Mecha Comic", "emoji": {"id": "1478369141957333083", "name": "Mechacomic"}},
                        {"label": "Jumptoon", "value": "Jumptoon", "emoji": {"id": "1478367963928068168", "name": "Jumptoon"}}
                    ]
                }
            ]
        }

        # 4. UTILITY / FOOTER SECTION
        footer_section = {
            "type": 10, # TEXT_DISPLAY
            "content": f"-# CS-ID: {guild_id if guild_id else '0'}"
        }
        
        utility_buttons_row = {
            "type": 1,
            "components": [
                {
                    "type": 2, "style": 2, 
                    "emoji": {"id": "1480959010722021567", "name": "setting"},
                    "custom_id": "v2_btn_settings"
                },
                {
                    "type": 2, "style": 4, 
                    "emoji": {"name": "✖️"},
                    "custom_id": "btn_close_main_dash"
                }
            ]
        }

        # --- ASSEMBLE PAYLOAD ---
        components = [
            {
                "type": 17, # CONTAINER
                "components": [
                    header_section,
                    {"type": 14, "divider": True, "spacing": 1},
                    weeklies_section,
                    action_row, # Moved immediately after weeklies
                    {"type": 14, "divider": True, "spacing": 1},
                    platform_list_section,
                    platform_select_row,
                    {"type": 14, "divider": True, "spacing": 1},
                    footer_section,
                    utility_buttons_row
                ]
            }
        ]

        payload = {
            "type": 7 if is_update else 4,
            "data": {
                "flags": 32768,
                "components": components
            }
        }
        return payload

    async def get_group_subs_list_payload(self, group_name: str, page: int = 0, platform_filter: str | None = None):
        """Generates a paginated, non-ephemeral list of all subscriptions grouped by day."""
        import datetime
        group_data = load_group(group_name)
        subs = list(group_data.get("subscriptions", []))
        overrides: dict = group_data.get("title_overrides", {})

        # Filter
        if platform_filter and platform_filter != "all":
            subs = [s for s in subs if s.get("platform", "").lower() == platform_filter.lower()]

        # Sunday-start day order
        day_order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        
        # Get Current Day (UTC+9 for JST context usually applied in this bot)
        jst_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
        today_name = jst_now.strftime("%A")

        # Sort by Day -> Time -> Title
        def get_sort_key(s):
            day = s.get("release_day")
            d_idx = day_order.index(day) if day in day_order else 7
            time = s.get("release_time", "99:99")
            url = (s.get("series_url") or "").rstrip('/')
            title = (overrides.get(url) or s.get("series_title") or "").lower()
            return (d_idx, time, title)
        
        subs.sort(key=get_sort_key)

        total: int = len(subs)
        start: int = page * 10
        end: int = start + 10
        visible_subs: list = subs[start:end]

        filter_label = platform_filter.capitalize() if platform_filter and platform_filter != 'all' else "All"
        header_text = f"## 📋 {group_name} Team Subscriptions ({filter_label})\n"
        
        content = ""
        last_day = None
        
        # Emojis
        emoji_today = "<:Calendar_T:1485261654777270312>"
        emoji_other = "<:Calendar_U:1485261652713803906>"

        for i, sub in enumerate(visible_subs, 1):
            current_day = sub.get("release_day") or "Other"
            
            # Show day header if it changed OR if it's the first sub on this page
            if current_day != last_day:
                emoji = emoji_today if current_day == today_name else emoji_other
                content += f"\n### {emoji} {current_day}\n"
                last_day = current_day

            url = (sub.get("series_url") or "").rstrip('/')
            overridden = overrides.get(url)
            original = sub.get("series_title")
            
            title_display = f"**{overridden}** ({original})" if overridden else f"**{original}**"
            content += f"{i + start}. {title_display}\n> <#{sub.get('channel_id')}>\n"

        if not content:
            content = "*No subscriptions found.*"

        # Platform Dropdown
        dropdown = {
            "type": 3,
            "custom_id": f"v2Dash_Filter|G:{group_name}",
            "placeholder": "Filter by Platform",
            "options": [
                {"label": "All Platforms", "value": "all", "default": not platform_filter or platform_filter == 'all'},
                {"label": "Piccoma", "value": "piccoma", "default": platform_filter == "piccoma", "emoji": {"id": "1478368704164134912"}},
                {"label": "Mechacomic", "value": "mecha", "default": platform_filter == "mecha", "emoji": {"id": "1478369141957333083"}},
                {"label": "Jumptoon", "value": "jumptoon", "default": platform_filter == "jumptoon", "emoji": {"id": "1478367963928068168"}}
            ]
        }

        # Detail Selection Dropdown
        options: list = []
        for j, sub in enumerate(visible_subs, 1):
            url = (sub.get("series_url") or "").rstrip('/')
            overridden = overrides.get(url)
            original = sub.get("series_title")
            label = f"{j + start}. {overridden or original}"
            options.append({
                "label": label[:100],
                "value": sub['series_id']
            })

        detail_select = {
            "type": 3,
            "custom_id": f"v2Dash_Detail_Select|G:{group_name}",
            "placeholder": "View Details",
            "options": options
        }
        
        detail_rows: list = [{"type": 1, "components": [detail_select]}] if visible_subs else []

        # Pagination Row
        pagination_row: dict[str, Any] = {"type": 1, "components": []}
        if page > 0:
            pagination_row["components"].append({
                "type": 2, "style": 2, "label": "⬅️ Previous",
                "custom_id": f"v2Dash_Pg|P:{page-1}|F:{platform_filter or 'all'}|G:{group_name}"
            })
        
        next_btn = {
            "type": 2, "style": 2, "label": "Next ➡️",
        }
        if end < total:
            next_btn["custom_id"] = f"v2Dash_Pg|P:{page+1}|F:{platform_filter or 'all'}|G:{group_name}"
        else:
            next_btn["custom_id"] = "v2Dash_Disabled_Next"
            next_btn["disabled"] = True
        pagination_row["components"].append(next_btn)

        # Back to Dashboard Home
        pagination_row["components"].append({
            "type": 2, "style": 4, "label": "Back to Dashboard",
            "custom_id": f"v2Dash_Home"
        })

        # ASSEMBLE V2 PAYLOAD
        container_components = [
            {"type": 10, "content": header_text},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 1, "components": [dropdown]},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": content}
        ]

        components = [{"type": 17, "components": container_components}]
        components.extend(detail_rows)
        components.append(pagination_row)

        return {
            "type": 7, # UPDATE_MESSAGE
            "data": {
                "components": components
            }
        }

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions."""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            
            # --- Close Button ---
            # --- Close / Cancel Buttons ---
            if custom_id == "btn_close_main_dash" or custom_id.startswith("v2_btn_sub_cancel_"):
                try: 
                    await interaction.response.defer()
                    await interaction.message.delete()
                except: pass
                return

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

            # --- Settings Button ---
            elif custom_id == "v2_btn_settings":
                await interaction.response.send_message("⚙️ **Settings Menu** coming soon!", ephemeral=True)

            # --- Channel Selection for Subscription ---
            elif custom_id.startswith("v2_select_sub_channel_"):
                # Format: v2_select_sub_channel_{platform}|{url}
                parts = custom_id.replace("v2_select_sub_channel_", "").split("|", 1)
                platform = parts[0]
                url = parts[1] if len(parts) > 1 else ""
                
                # Extract selected channel ID
                values = interaction.data.get("values", [])
                if not values:
                    return # Should not happen
                
                target_channel_id = int(values[0])
                await self.finalize_subscription(interaction, platform, url, target_channel_id)

            # --- Registration Hook ---
            elif custom_id.startswith("v2_btn_sub_confirm_yes_"):
                # Format: v2_btn_sub_confirm_yes_{platform}|{url}
                parts = custom_id.replace("v2_btn_sub_confirm_yes_", "").split("|", 1)
                platform = parts[0]
                url = parts[1] if len(parts) > 1 else ""
                await self.finalize_subscription(interaction, platform, url, interaction.channel_id)
            elif custom_id.startswith("v2_btn_sub_confirm_no_"):
                # Format: v2_btn_sub_confirm_no_{platform}|{url}
                parts = custom_id.replace("v2_btn_sub_confirm_no_", "").split("|", 1)
                platform = parts[0]
                url = parts[1] if len(parts) > 1 else ""
                await self.launch_channel_select(interaction, platform, url)

            # --- View All Subscriptions (Paginated List) ---
            elif custom_id.startswith("v2_btn_view_all_subs_"):
                group_name = custom_id.replace("v2_btn_view_all_subs_", "")
                payload = await self.get_group_subs_list_payload(group_name)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- Filter Subscriptions (Dropdown) ---
            elif custom_id.startswith("v2Dash_Filter|"):
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                platform = interaction.data.get("values", ["all"])[0]
                payload = await self.get_group_subs_list_payload(group_name, platform_filter=platform)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- Paginate Subscriptions ---
            elif custom_id.startswith("v2Dash_Pg|"):
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                page = int(parts.get("P", 0))
                platform = parts.get("F", "all")
                payload = await self.get_group_subs_list_payload(group_name, page=page, platform_filter=platform)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- View Series Detail ---
            elif custom_id.startswith("v2Dash_Detail|") or custom_id.startswith("v2Dash_Detail_Select|"):
                # "View Detail" launches the UniversalDashboard for that series
                if custom_id.startswith("v2Dash_Detail_Select|"):
                    series_id = interaction.data.get("values", [None])[0]
                    parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                    group_name = parts.get("G")
                else:
                    parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                    series_id = parts.get("S")
                    group_name = parts.get("G")
                
                if not series_id: return
                
                # We need to find the series URL and platform
                redis_brain = RedisManager()
                sub_data_raw = await redis_brain.client.hget("verzue:index:subs", series_id)
                if not sub_data_raw:
                    return await interaction.response.send_message("❌ Series metadata not found in Redis. Please try syncing.", ephemeral=True)
                
                sub_data = json.loads(sub_data_raw)
                url = sub_data.get("url")
                
                # Defer to show loading while we fetch current chapters
                await interaction.response.defer(ephemeral=True)
                
                # Launch Extractor logic
                p_url_low = url.lower()
                platform_type = "jumptoon" if "jumptoon" in p_url_low else "piccoma" if "piccoma" in p_url_low else "mecha"
                scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
                
                try:
                    # S-Grade: Use get_series_info instead of non-existent fetch_metadata
                    title, total_chapters, all_chapters, image_url, series_id, release_day, release_time, status_label, genre_label = await scraper.get_series_info(url)
                    
                    ctx_data = {
                        "url": url,
                        "title": title or sub_data.get("title", "Unknown"),
                        "original_title": title or sub_data.get("title", "Unknown"),
                        "chapters": all_chapters or [],
                        "total_chapters": total_chapters or 0,
                        "image_url": image_url or "",
                        "status_label": status_label,
                        "req_id": uuid.uuid4().hex[:8],
                        "series_id": series_id,
                        "user": interaction.user.name
                    }
                    from app.bot.common.view import UniversalDashboard
                    view = UniversalDashboard(self.bot, ctx_data, platform_type)
                    view.interaction = interaction # 🟢 CRITICAL: Link the interaction!
                    await view.update_view(interaction)
                except Exception as e:
                    logger.error(f"Failed to launch detail view: {e}")
                    await interaction.followup.send(f"❌ Failed to fetch series details: {e}", ephemeral=True)

            # --- Back to Home ---
            elif custom_id == "v2Dash_Home":
                payload = await self.get_dashboard_payload(interaction, is_update=True)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- Interaction Redirection (Original Logic) ---
            elif custom_id.startswith("v2_btn_sub_move_yes_"):
                # "Yes" to "Move this series to new channel?" -> Show channel select inline
                parts = custom_id.replace("v2_btn_sub_move_yes_", "").split("|", 1)
                platform = parts[0]
                url = parts[1] if len(parts) > 1 else ""
                await self.launch_channel_select(interaction, platform, url, force_message=True)
            elif custom_id.startswith("v2_btn_sub_move_no_"):
                # "No" -> Cancel / Delete
                try: await interaction.message.delete()
                except: pass

            # --- Universal Dashboard Navigation & Actions ---
            elif any(custom_id.startswith(p) for p in ["btn_open_menu_", "mode_select_", "page_select_", "btn_start_", "btn_cancel_", "btn_home_", "btn_clear_", "btn_error_retry_", "btn_visit_drive_", "btn_report_error_", "btn_pending_link_"]):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view: UniversalDashboard = UniversalDashboard.active_views.get(req_id)
                if not view: 
                    error_msg = (
                        "❌ **Session Expired or Process Conflict.**\n\n"
                        "This can happen for two reasons:\n"
                        "1. **Timeout**: The session was inactive for more than 15 minutes.\n"
                        "2. **Process Conflict**: Multiple bots are running. **Please kill all `main.py` processes and run only one.**"
                    )
                    if interaction.response.is_done():
                        return await interaction.followup.send(error_msg, ephemeral=True)
                    else:
                        return await interaction.response.send_message(error_msg, ephemeral=True)
                
                view.interaction = interaction
                view.last_interaction_time = time.time() # 🔄 Reset session timer on every interaction

                # NEW: Error Report Logic
                if custom_id.startswith("btn_report_error_"):
                    acknowledgement = (
                        "Thank you for reporting this. We'll improve this feature to allow detailed error reporting soon."
                    )
                    return await interaction.response.send_message(acknowledgement, ephemeral=True)

                if custom_id.startswith("btn_pending_link_"):
                    return await interaction.response.send_message("This link is still being generated. Please wait a moment.", ephemeral=True)

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
                    
                    options = [
                        {"label": "SR", "description": "Select all available chapters.", "value": "all"},
                        {"label": "Select", "description": "Add custom range of chapters.", "value": "custom"}
                    ]
                    
                    if new_ch:
                        ch_not = new_ch.get("notation", "Ch").strip()
                        ch_tit = new_ch.get("title", "").strip()
                        latest_desc = f"[NEW] - {ch_not} {ch_tit}".strip()
                    else:
                        latest_desc = "No New Chapter released."

                    latest_opt = {
                        "label": "Latest Chapter:", 
                        "description": latest_desc[:100], 
                        "value": "latest"
                    }
                    options.append(latest_opt)
                    
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
                            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(view.url)
                            
                            # 🟢 Dynamic Math: Jumptoon (30/pg) vs Mecha (10/pg)
                            pg_size = 30 if view.service_type == "jumptoon" else 10
                            target_jt_page = math.ceil(req_ch_index / pg_size)
                            last_jt_page = math.ceil(getattr(view, 'total_chapters', 0) / pg_size)
                            
                            try:
                                logger.info(f"[{req_id}] ⚡ Lazy Loading: Fetching up to {view.service_type} page {target_jt_page}...")
                                seen_ids = {ch['id'] for ch in view.all_chapters}
                                
                                # 🟢 Every Provider is now Async
                                new_chaps = await scraper.fetch_more_chapters(view.url, target_jt_page, seen_ids, [last_jt_page] if last_jt_page > 1 else [])
                                
                                if new_chaps:
                                    view.all_chapters.extend(new_chaps)
                                    logger.info(f"[{req_id}] ✅ Lazy Loading: Added {len(new_chaps)} new chapters to memory.")
                            except Exception as e:
                                logger.error(f"[{req_id}] ❌ Lazy Loading failed: {e}")

                    # 🟢 Trigger Refresh: Don't pass 'interaction' here because we already deferred!
                    # Passing None forces the view to use the PATCH /webhooks route.
                    await view.update_view()

                # D. Home (Redirect to Main)
                elif custom_id.startswith("btn_home_"):
                    if view.service_type == "mecha": 
                        try: self.bot.task_queue.browser_service.dec_session()
                        except: pass
                    UniversalDashboard.active_views.pop(req_id, None)
                    
                    try:
                        main_payload = await self.get_dashboard_payload(interaction, is_update=True)
                        route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                        await self.bot.http.request(route, json=main_payload)
                    except Exception as e:
                        logger.error(f"Failed to return home: {e}")
                    return

                # E. Cancel Session
                elif custom_id.startswith("btn_cancel_"):
                    if view.service_type == "mecha": self.bot.task_queue.browser_service.dec_session()
                    UniversalDashboard.active_views.pop(req_id, None)
                    await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={"type": 6})
                    try: await interaction.message.delete()
                    except: pass

                # E. Start Batch Process
                elif custom_id.startswith("btn_start_"):
                    try:
                        view.processing_mode = True
                        view.phases["analyze"] = "loading"
                        await view.update_view(interaction)
                        
                        asyncio.create_task(view.monitor_tasks())
                        from app.services.batch_controller import BatchController
                        tasks = await BatchController(self.bot).prepare_batch(interaction, sorted(list(view.selected_indices)), view.all_chapters, view.title, view.url, view_ref=view, series_id=view.series_id, original_title=view.original_title)
                        if tasks:
                            view.phases.update({"analyze":"done","purchase":"done","download":"loading"})
                            view.active_tasks = [await self.bot.task_queue.add_task(t) for t in tasks]
                            view.trigger_refresh()
                    except RuntimeError as e:
                        # 🟢 S-GRADE: Friendly Maintenance Feedback
                        view.processing_mode = False
                        view.trigger_refresh()
                        return await interaction.followup.send(f"⚠️ **Maintenance Mode**\n{str(e)}", ephemeral=True)
                    except Exception as e:
                        logger.error(f"Failed to start batch: {e}")
                        return await interaction.followup.send(f"❌ **Error starting batch:** {e}", ephemeral=True)

        # --- Modal Submissions (URL Entry & Range Picker) ---
        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            
            if custom_id.startswith("modal_select_"):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view: UniversalDashboard | None = UniversalDashboard.active_views.get(req_id)
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
                            "type": 4, # CHANNEL_MESSAGE_WITH_SOURCE
                            "data": {
                                "flags": 32832, # Ephemeral + V2
                                "components": [{
                                    "type": 17,
                                    "components": [{
                                        "type": 10,
                                        "content": "<a:error:1482426908699267174> **No New Releases Found**\nThis series currently doesn't have any newly released chapters. Please use the **SR** or **Select** options instead."
                                    }]
                                }]
                            }
                        }
                        try:
                            return await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=error_payload)
                        except: return
                elif mode_val == "custom" and range_val:
                    try:
                        # 🟢 S-GRADE: Support semantic ranges (matching _display_idx from view.py)
                        parts = range_val.replace(" ", "").split(",")
                        for p in parts:
                            if "-" in p:
                                try:
                                    s_str, e_str = p.split("-")
                                    # Support decimal bounds (e.g. 58.1-59)
                                    s_val = float(s_str)
                                    e_val = float(e_str)
                                    
                                    for idx, ch in enumerate(view.all_chapters):
                                        # Compare against the semantic display index (as a number)
                                        disp_idx_str = ch.get('_display_idx')
                                        if disp_idx_str:
                                            try:
                                                curr_val = float(disp_idx_str)
                                                if s_val <= curr_val <= e_val:
                                                    view.selected_indices.add(idx)
                                            except: pass
                                        else:
                                            # Fallback for non-computed views
                                            if s_val <= (idx + 1) <= e_val:
                                                view.selected_indices.add(idx)
                                except ValueError:
                                    pass # Skip malformed range segments
                            else:
                                # 🟢 S-GRADE: Match both integers (63) and decimals (63.1)
                                import re
                                if re.match(r'^\d+(\.\d+)?$', p):
                                    for idx, ch in enumerate(view.all_chapters):
                                        display_idx = str(ch.get('_display_idx', ''))
                                        main_idx = ch.get('_main_idx')
                                        
                                        if display_idx == p:
                                            # Exact match for decimal or integer display
                                            view.selected_indices.add(idx)
                                        elif "." not in p:
                                            # If user entered an integer like "63", 
                                            # also select all its hiatuses (63.1, 63.2 etc)
                                            if main_idx is not None and str(main_idx) == p:
                                                view.selected_indices.add(idx)
                                            elif main_idx is None and str(idx + 1) == p:
                                                view.selected_indices.add(idx)
                    except Exception as e:
                        logger.error(f"Range parse failed: {e}")
                    
                await view.update_view(interaction)

            elif custom_id.startswith(("v2_modal_subscribe_", "v2_modal_sub_channel_select_")):
                await self.handle_subscribe_modal(interaction, custom_id)

            elif custom_id.startswith("v2_modal_"):
                await self.handle_platform_modal(interaction, custom_id)

    async def handle_custom_modal_trigger(self, interaction, view):
        """Deprecated."""
        pass

    async def handle_platform_modal(self, interaction, custom_id):
        """Processes the platform URL submission modal."""
        # 🟢 THE FIX: DEFER IMMEDIATELY to prevent 3s timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            pass

        platform = custom_id.replace("v2_modal_", "")
        url, action = "", "download"
        
        for row in interaction.data.get("components", []):
            inner = row.get("component", {})
            if inner.get("custom_id") == "action_radio": action = inner.get("value", "download") 
            elif inner.get("custom_id") == "url_input": url = inner.get("value", "")
        
        platform_domains = {"Mecha Comic": "mechacomic.jp", "Jumptoon": "jumptoon.com", "KakaoPage": "kakao.com", "Kuaikan Manhua": "kuaikanmanhua.com", "Piccoma": "piccoma.com", "AC.QQ": "ac.qq.com"}
        expected_domain = platform_domains.get(platform)
        
        if expected_domain and expected_domain not in url.lower():
            # Use followup since we deferred. MUST USE V2 structure for V2-deferred messages.
            embed_payload = {
                "flags": 32832, # Ephemeral + V2
                "components": [{
                    "type": 17,
                    "components": [
                        {
                            "type": 10,
                            "content": f"⛔ **Protocol Violation**\nExpected `{expected_domain}` link."
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
            await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=embed_payload)
            return 

        if action == "subscribe":
            # 🟢 INSTANT CHECK: Extract series_id from URL and check local records
            # Jumptoon: /contents/JT00085 or /series/JT00085
            series_id = None
            if "jumptoon" in url.lower():
                import re
                match = re.search(r'/(?:contents|series)/([^/?#]+)', url)
                if match: series_id = match.group(1)
            
            from app.services.group_manager import get_series_by_channel
            occupied_by = get_series_by_channel(interaction.channel_id)
            is_already_here = False
            occupation_series = None
            
            if series_id and occupied_by:
                occ_group, occ_sub = occupied_by
                if occ_sub.get("series_id") == series_id:
                    is_already_here = True
                else:
                    occupation_series = occ_sub
            
            # 🟢 SCENARIO A: Already Subscribed (Instant Response)
            if is_already_here or occupation_series:
                logger.info(f"INSTANT MATCH: series={series_id}, same={is_already_here}")
                trigger_components = []
                
                if is_already_here:
                    content = f"✅ **Series Found but um...**\nLooks like this series is already subscribed to this channel. Would you want to move this series to new channel?"
                    yes_id = f"v2_btn_sub_move_yes_{platform}|{url}"
                    no_id = f"v2_btn_sub_move_no_{platform}|{url}"
                    trigger_components = [
                        {"type": 10, "content": content},
                        {
                            "type": 1,
                            "components": [
                                {"type": 2, "style": 3, "label": "Yes", "custom_id": yes_id},
                                {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"v2_btn_sub_cancel_{platform}"}
                            ]
                        }
                    ]
                else:
                    # 🚫 Uh-Oh: Occupied by another series
                    from app.services.group_manager import get_title_override
                    occ_title = get_title_override(occ_group, occupation_series.get('series_url', '')) or occupation_series.get('series_title', 'Unknown')
                    content = f"🚫 **Uh-Oh**\nThis channel is already occupied by **{occ_title}**.\nPlease choose a dedicated channel for this series."
                    trigger_components = [
                        {"type": 10, "content": content},
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 8, # CHANNEL_SELECT
                                    "custom_id": f"v2_select_sub_channel_{platform}|{url}",
                                    "channel_types": [0],
                                    "placeholder": "Select Channel"
                                }
                            ]
                        }
                    ]

                trigger_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": trigger_components
                    }]
                }
                await self.bot.http.request(
                    discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'),
                    json=trigger_payload
                )
                return

            # 🟢 SCENARIO B: New Series (Fast Metadata Fetch)
            try:
                scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
                # Call with fast=True to skip chapter crawling
                data = await scraper.get_series_info(url, fast=True)
                title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
                series_id = series_id or series_id
                
                # 🟢 S-GRADE: Subscription Block Rule (Mar 25 Request)
                if status_label in ["Oneshot", "Completed", "Novel"]:
                    error_payload = {
                        "flags": 32768,
                        "components": [{
                            "type": 17,
                            "components": [{
                                "type": 10,
                                "content": f"🚫 **Uh-Oh, we can't proceed with the subscription.**\nThis series is marked as **{status_label}**."
                            }]
                        }]
                    }
                    await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=error_payload)
                    return

                logger.info(f"FAST FETCH: {title} ({series_id})")
                
                # Standard prompt
                content = f"✅ **Series Found!**\nWould you like to use this channel for this series subscription?"
                yes_id = f"v2_btn_sub_confirm_yes_{platform}|{url}"
                no_id = f"v2_btn_sub_confirm_no_{platform}|{url}"
                trigger_components = [
                    {"type": 10, "content": content},
                    {
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 3, "label": "Yes", "custom_id": yes_id},
                            {"type": 2, "style": 4, "label": "No (Use other)", "custom_id": no_id},
                            {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"v2_btn_sub_cancel_{platform}"}
                        ]
                    }
                ]

                trigger_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": trigger_components
                    }]
                }
                await self.bot.http.request(
                    discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'),
                    json=trigger_payload
                )
            except Exception as e:
                logger.error(f"Failed to analyze for subscription: {e}")
                error_p = {
                    "flags": 32768, 
                    "components": [{
                        "type": 17, 
                        "components": [
                            {"type": 10, "content": f"❌ **Analysis Failed:**\n`{str(e)}`"},
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
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=error_p)
            return

        # 🟢 Send the Analyzing message via PATCH
        analyzing_payload = {
            "flags": 32768,
            "components": [{
                "type": 17,
                "components": [{
                    "type": 10,
                    "content": f"🔍 **Analyzing {platform} Link:**\n`{url}`\n*Fetching metadata, please wait...*"
                }]
            }]
        }
        
        try:
            route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=analyzing_payload)
        except Exception as e:
            logger.error(f"Interaction expired or PATCH failed: {e}")
            return

        try:
            from app.core.logger import req_id_context
            from app.bot.common.view import UniversalDashboard
            req_id = str(uuid.uuid4())[:8].upper()
            token = req_id_context.set(req_id)
            
            # 📥 LOG NEW REQUEST (User Request - Mar 27)
            log_msg = (
                "==================================================\n"
                f"📥 NEW REQUEST: {platform}\n"
                f"👤 USER: {interaction.user.name} ({interaction.user.id})\n"
                f"🔗 URL: {url}\n"
                "==================================================\n"
            )
            log_path = Settings.REQUEST_LOG_DIR / f"{req_id}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(log_msg)
            
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            logger.info(f"[{req_id}] 🚀 Handoff: Extraction starting for {platform}...")
            # 🟢 Every Provider is now S-Grade Async
            data = await scraper.get_series_info(url)
                
            logger.info(f"[{req_id}] ✅ Handoff: Metadata retrieved successfully.")
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
            
            # 🟢 NOVEL REJECTION (User Request - Mar 27)
            if status_label == "Novel":
                error_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [{
                            "type": 10,
                            "content": f"🚫 **Uh-Oh, we can't proceed with this series.**\nThis series is a **Novel**, which is currently not supported for extraction."
                        }]
                    }]
                }
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=error_payload)
                return

            
            # 🟢 TITLE OVERRIDE: Check if the group has a custom English title for this series
            guild_id = interaction.guild.id if interaction.guild else 0
            channel_id_origin = interaction.channel.id if interaction.channel else 0
            group_name = Settings.SERVER_MAP.get(channel_id_origin) or Settings.SERVER_MAP.get(guild_id)
            original_title = title # Keep the one from scraper
            if group_name:
                from app.services.group_manager import get_title_override
                custom_title = get_title_override(group_name, url)
                if custom_title:
                    logger.info(f"[{req_id}] 🏷️ Title Override: '{title}' → '{custom_title}' (Group: {group_name})")
                    title = custom_title
            
            ctx_data = {
                'url': url, 
                'title': title, 
                'original_title': original_title,
                'chapters': chapter_list, 
                'total_chapters': total_chapters, 
                'image_url': image_url, 
                'series_id': series_id, 
                'req_id': req_id, 
                'user': interaction.user
            }
            service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
            
            view = UniversalDashboard(self.bot, ctx_data, service_type)
            view.interaction = interaction
            
            # 🟢 NO "content": "" here!
            payload_data = {"flags": 32768, "components": view.build_v2_payload()}
            
            route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=payload_data)
            
            if service_type == "mecha":
                asyncio.create_task(self.bot.task_queue.browser_service.start())
                
        except Exception as e:
            logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
            err = str(e).splitlines()[0] if str(e) else "Unknown Error"
            
            # Match V2 format for errors so Discord doesn't crash on the PATCH
            error_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [
                        {
                            "type": 10,
                            "content": f"<a:error:1482426908699267174> **Extraction Failed:**\n`{err}`"
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
            try:
                route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=error_payload)
            except:
                pass
        finally:
            try: req_id_context.reset(token)
            except: pass

    async def launch_channel_select(self, interaction, platform, url, force_message: bool = False):
        """Sends a V2 message with a Channel Select component."""
        channel_select_payload = {
            "flags": 32768,
            "components": [
                {
                    "type": 17,
                    "components": [
                        {
                            "type": 10,
                            "content": f"📡 **Subscription Setup**\nPlease select the target channel for the **{platform}** series subscription below."
                        },
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 8,
                                    "custom_id": f"v2_select_sub_channel_{platform}|{url}",
                                    "channel_types": [0], # Text Channels only
                                    "placeholder": "Search or select a text channel..."
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        try:
            if force_message:
                # 🟢 UPDATE_MESSAGE (7) is the correct way to handle button-click message updates
                callback_payload = {
                    "type": 7,
                    "data": channel_select_payload
                }
                route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                await self.bot.http.request(route, json=callback_payload)
                return

            # Note: This is usually called from a button interaction, so we can PATCH original or POST new.
            # User said "popup" but Select Menus in messages feel like a "step".
            # If they meant a Modal with Channel Select, we need to check if Label supports Type 8.
            # Reference says: "Channel Selects are available in messages and modals. They must be placed inside an Action Row in messages and a Label in modals."
            # The user specifically said "On No button popup is alright", implies they might want a Modal.
            # Let's try to fit it into a Modal if possible.
            
            modal_payload = {
                "type": 9,
                "data": {
                    "custom_id": f"v2_modal_sub_channel_select_{platform}|{url}", # We'll need to handle this in on_interaction too
                    "title": "Select Target Channel",
                    "components": [
                        {
                            "type": 18, # LABEL
                            "label": "Choose the auto-download channel",
                            "component": {
                                "type": 8, # CHANNEL_SELECT
                                "custom_id": f"v2_select_sub_channel_{platform}|{url}",
                                "channel_types": [0],
                                "required": True
                            }
                        }
                    ]
                }
            }
            await self.bot.http.request(
                discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                json=modal_payload
            )
        except Exception as e:
            logger.error(f"Failed to launch channel select: {e}")

    async def handle_subscribe_modal(self, interaction, custom_id):
        """Processes the secondary subscription modal (DEPRECATED for manual, now used for Channel Select)."""
        # Note: Interaction type for Modal Submit is 5.
        # If it's the new modal, we extract from components -> label -> component -> values
        platform, url = "", ""
        if custom_id.startswith("v2_modal_sub_channel_select_"):
            parts = custom_id.replace("v2_modal_sub_channel_select_", "").split("|", 1)
            platform = parts[0]
            url = parts[1] if len(parts) > 1 else ""
            
            # Extract from Modal Data
            # components[0] (Label) -> component (Channel Select) -> values
            try:
                rows = interaction.data.get("components", [])
                if rows:
                    label_comp = rows[0]
                    ch_select = label_comp.get("component")
                    values = ch_select.get("values", [])
                    if values:
                        target_channel_id = int(values[0])
                        return await self.finalize_subscription(interaction, platform, url, target_channel_id)
            except Exception as e:
                logger.error(f"Failed to parse channel select modal data: {e}")
                return

    async def finalize_subscription(self, interaction, platform, url, target_channel_id):
        """Core backend logic for completing a subscription."""
        # defer if not already thinking
        if not interaction.response.is_done():
            analyzing_payload = {
                "type": 7, # 🟢 Update original message instead of creating a new one
                "data": {
                    "flags": 32768,
                    "components": [{"type": 17, "components": [{"type": 10, "content": "📡 **Setting up Subscription...**"}]}]
                }
            }
            try: await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=analyzing_payload)
            except: pass

        try:
            import datetime
            from app.services.group_manager import add_subscription, is_series_subscribed_globally, get_title_override, get_series_by_channel
            
            # 1. Fetch Metadata (Fast Mode)
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            data = await scraper.get_series_info(url, fast=True)
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data

            # 2. Check Global Singularity rule
            is_subbed, existing_group = await is_series_subscribed_globally(series_id)
            if is_subbed:
                error_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [{
                            "type": 10,
                            "content": f"⚠️ **Subscription Rejected**\n**{title}** is already being tracked by **{existing_group}**."
                        }]
                    }]
                }
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=error_payload)
                return self._queue_auto_delete(interaction, 1800)

            # 3. Check Channel Occupancy (Exclusive: One Series Per Channel)
            # 🟢 EXCEPTION: Server 1419393318147719170 allows unlimited series per channel
            occupied_by = get_series_by_channel(target_channel_id)
            is_universal_server = (interaction.guild_id == 1419393318147719170)
            
            if occupied_by and not is_universal_server:
                existing_group, existing_sub = occupied_by
                if existing_sub.get("series_id") != series_id:
                    # 🟢 S-GRADE: Check for Title Override for the OCCUPYING series
                    occ_title = get_title_override(existing_group, existing_sub.get("series_url")) or existing_sub.get("series_title", "Unknown Series")
                    
                    error_payload = {
                        "flags": 32768,
                        "components": [{
                            "type": 17,
                            "components": [
                                {
                                    "type": 10,
                                    "content": f"🚫 **Uh-Oh**\nThis channel is already occupied by **{occ_title}**.\n\n**Select Channel**"
                                },
                                {
                                    "type": 1,
                                    "components": [
                                        {
                                            "type": 8,
                                            "custom_id": f"v2_select_sub_channel_{platform}|{url}",
                                            "channel_types": [0],
                                            "placeholder": "Search or select a text channel..."
                                        }
                                    ]
                                }
                            ]
                        }]
                    }
                    await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=error_payload)
                    return self._queue_auto_delete(interaction, 1800)

            # 3. Determine Group
            guild_id = interaction.guild.id if interaction.guild else 0
            origin_channel_id = interaction.channel.id if interaction.channel else 0
            group_name = Settings.SERVER_MAP.get(origin_channel_id) or Settings.SERVER_MAP.get(guild_id)

            if not group_name:
                error_payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "components": [{"type": 10, "content": "❌ **No Group Profile Linked**\nPlease link this server to a group via `/register-server` first."}]
                    }]
                }
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=error_payload)
                return self._queue_auto_delete(interaction, 1800)

            # Check for Title Override
            display_title = get_title_override(group_name, url) or title

            # 4. Save Subscription
            # 🏆 S-GRADE: Catch-up Notification
            # If the latest chapter is flagged 'New', we set last_known to the PREVIOUS ID
            # so the poller triggers an immediate notification for the user.
            latest_ch = chapter_list[-1] if chapter_list else None
            if latest_ch and latest_ch.get('is_new'):
                last_known = str(chapter_list[-2]["id"]) if len(chapter_list) > 1 else "0"
            else:
                last_known = str(latest_ch["id"]) if latest_ch else "0"
            sub = {
                "series_id": series_id,
                "series_title": title,
                "series_url": url,
                "platform": platform,
                "channel_id": target_channel_id,
                "release_day": release_day, 
                "last_known_chapter_id": last_known,
                "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "added_by": interaction.user.id
            }
            add_subscription(group_name, sub)

            # 5. Success UI
            success_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [{
                        "type": 10,
                        "content": (
                            f"# <a:done_subscription:1482425914456281108> Successfully Subscribed!\n"
                            f"Series: [{display_title}]({url})\n"
                            f"Subscription Channel: <#{target_channel_id}>\n"
                            f"Subscribed by: <@{interaction.user.id}>"
                        )
                    }]
                }]
            }
            
            # Send success message
            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=success_payload)
            
            logger.info(f"✅ Subscription complete: {title} in <#{target_channel_id}>")

            # 🟢 Trigger Background Sync for full chapter list and latest metadata
            if hasattr(scraper, "sync_latest_chapters"):
                asyncio.create_task(scraper.sync_latest_chapters(url))
                
            return self._queue_auto_delete(interaction, 60)

        except Exception as e:
            logger.error(f"Subscription setup failed: {e}", exc_info=True)
            err = str(e).splitlines()[0] if str(e) else "Unknown Error"
            error_p = {"flags": 32768, "components": [{"type": 17, "components": [{"type": 10, "content": f"❌ **Subscription Failed:**\n`{err}`"}]}]}
            try: 
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=error_p)
                # 🟢 S-GRADE: Auto-delete error message after 30 minutes
                self._queue_auto_delete(interaction, 1800)
            except: pass

    def _queue_auto_delete(self, interaction, delay: int):
        """Starts a fire-and-forget task to delete a webhook message after a delay."""
        async def _internal_delete():
            await asyncio.sleep(delay)
            try:
                del_route = discord.http.Route('DELETE', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                await self.bot.http.request(del_route)
            except: 
                pass # Already deleted or expired
        
        asyncio.create_task(_internal_delete())

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))