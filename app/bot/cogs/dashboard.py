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
import random
from typing import TYPE_CHECKING, List, Dict, Any, Optional
from app.core.exceptions import MechaException
from app.core.events import EventBus
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
        self.redis = RedisManager()
        self._url_store: dict[str, str] = {} # token -> url

    def _store_url(self, url: str) -> str:
        """Stores a URL in transient memory and returns a short token safe for custom_id."""
        token = f"tok_{uuid.uuid4().hex[:8]}"
        self._url_store[token] = url
        return token

    def _resolve_url(self, token_or_url: str) -> str:
        """Resolves a token back to a URL, or returns the input if not a token."""
        if not str(token_or_url).startswith("tok_"):
            return token_or_url
        return self._url_store.get(token_or_url, token_or_url)

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
            logger.debug(f"[Dashboard] Sending Dashboard Payload (User: {interaction.user.id})")
            await self.bot.http.request(route, json=payload)
        except discord.NotFound:
            logger.warning(f"[Dashboard] Dashboard interaction not found or expired.")
        except Exception as e:
            logger.error(f"[Dashboard] Failed to send V2 Dashboard: {e}")

    async def get_weeklies_section(self, scan_name: str):
        """Generates the 'Today Weeklies' text and action row components."""
        from app.services.redis_manager import RedisManager
        redis_brain = RedisManager()
        
        # 🟢 Day Rollover: Use UTC to match AutoPoller
        now = datetime.datetime.now(datetime.timezone.utc)
        today_name = now.strftime("%A")
        
        # Get hydrated sub data for today
        group_subs = await redis_brain.get_schedule_for_group(scan_name, today_name)
        
        # Limit to TOP 3 as per user request
        display_subs = group_subs[:3]
        
        weeklies_text = "## Today Weeklies\n"
        if display_subs:
            # 🟢 S-GRADE: Load title overrides for the specific group
            from app.services.group_manager import load_group
            group_data = load_group(scan_name)
            overrides = group_data.get("title_overrides", {})
            
            for i, sub in enumerate(display_subs, 1):
                url = sub.get("url", "").lower()
                original_title = sub.get("title", "Unknown Series")
                
                # Apply Override if available
                from app.services.group_manager import _clean_url
                clean_url = _clean_url(url)
                title = overrides.get(clean_url) or original_title
                
                # Platform Emoji
                emoji = "📖"
                if "piccoma" in url: emoji = "<:Piccoma:1478368704164134912>"
                elif "mecha" in url: emoji = "<:Mechacomic:1478369141957333083>"
                elif "jumptoon" in url: emoji = "<:Jumptoon:1478367963928068168>"
                
                # 🟢 NEW FORMAT: > i. <Emoji> Title: <#channel_id>
                weeklies_text += f"> {i}. {emoji} {title}: <#{sub.get('channel_id') or '0'}>\n"
        else:
            weeklies_text += "> *No scheduled series subscriptions for today.*"

        weeklies_section = {
            "type": 10, # TEXT_DISPLAY
            "content": weeklies_text
        }

        action_row = {
            "type": 1,
            "components": [
                {
                    "type": 2, "style": 2, "label": "View All Subscriptions",
                    "custom_id": f"v2_btn_view_all_subs_{scan_name}",
                    "emoji": {"name": "📋"}
                }
            ]
        }
        
        return weeklies_section, action_row

    async def get_dashboard_payload(self, interaction: discord.Interaction, is_update=False):
        """Standardized payload generator for the refined V2 Dashboard."""
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        
        # 🟢 S-GRADE: Use dynamic app_state instead of removed static Settings.SERVER_MAP
        state = self.bot.app_state
        scan_name = state.server_map.get(channel_id) or state.server_map.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        # 🟢 S-GRADE: Fetch Weeklies via helper
        weeklies_section, action_row = await self.get_weeklies_section(scan_name)
        
        # Custom Logo
        custom_emoji = get_group_emoji(scan_name)
        header_logo = f"{custom_emoji} " if custom_emoji else ""

        # 1. HEADER SECTION
        header_section = {
            "type": 10, # TEXT_DISPLAY
            "content": f"# {header_logo}{scan_name}'s Dashboard"
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
                        {"label": "Piccoma", "value": "piccoma", "emoji": {"id": "1478368704164134912", "name": "Piccoma"}},
                        {"label": "Mecha Comic", "value": "mecha", "emoji": {"id": "1478369141957333083", "name": "Mechacomic"}},
                        {"label": "Jumptoon", "value": "jumptoon", "emoji": {"id": "1478367963928068168", "name": "Jumptoon"}}
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
        """Generates a paginated, daily-grouped list of subscriptions with premium V2 layout."""
        from app.services.group_manager import load_group, _clean_url
        group_data = load_group(group_name)
        subs = list(group_data.get("subscriptions", []))
        overrides: dict = group_data.get("title_overrides", {})

        # Filter
        if platform_filter and platform_filter != "all":
            subs = [s for s in subs if s.get("platform", "").lower() == platform_filter.lower()]

        # Sort order — Completed goes after Hiatus
        day_order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Hiatus", "Completed"]
        
        # Get Current Day (UTC+9 for JST context)
        # 🟢 Day Rollover: Use UTC to match AutoPoller
        now = datetime.datetime.now(datetime.timezone.utc)
        today_name = now.strftime("%A")

        # Sort by Day -> Time -> Title
        def get_sort_key(s):
            status = (s.get("status") or "").lower()
            if status == "completed":
                day = "Completed"
            elif status == "hiatus":
                day = "Hiatus"
            else:
                day = s.get("release_day") or "Hiatus"
            
            d_idx = day_order.index(day) if day in day_order else 7
            time = s.get("release_time", "99:99")
            url = _clean_url(s.get("series_url") or "")
            title = (overrides.get(url) or s.get("series_title") or "").lower()
            return (d_idx, time, title)
        
        subs.sort(key=get_sort_key)

        total: int = len(subs)
        # 🟢 S-GRADE: Clamp page to prevent stale P-IDs from crashing on empty slices
        max_page = max(0, math.ceil(total / 10) - 1)
        page = min(page, max_page)

        start: int = page * 10
        end: int = start + 10
        visible_subs: list = subs[start:end]

        # 🟢 DYNAMIC HEADER
        filter_label = "All"
        if platform_filter and platform_filter != "all":
            # Map internal keys to display names
            mapping = {"piccoma": "Piccoma", "mecha": "Mechacomic", "jumptoon": "Jumptoon"}
            filter_label = mapping.get(platform_filter.lower(), platform_filter.capitalize())

        # 🟢 EMPTY STATE FALLBACK
        if not visible_subs:
            return {
                "type": 7, # UPDATE_MESSAGE
                "data": {
                    "components": [
                        {
                            "type": 17, # Container
                            "components": [
                                {"type": 10, "content": f"# <:Subscriptions:1488498943271895161> {group_name} Subscriptions"},
                                {"type": 14, "divider": True, "spacing": 1},
                                {"type": 10, "content": f"### No Results Found\nThere are no scheduled series in **{group_name}** matching filter: **{filter_label}**."}
                            ]
                        },
                        {
                            "type": 1, # Action Row
                            "components": [
                                {"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}
                            ]
                        }
                    ]
                }
            }

        header_text = f"# <:Subscriptions:1488498943271895161> {group_name} Team Subscriptions ({filter_label})"
        
        content = ""
        last_day = None
        
        # Emojis for Daily Headers
        emoji_today = "<:Calendar_T:1485261654777270312>"
        emoji_other = "<:Calendar_U:1485261652713803906>"

        for i, sub in enumerate(visible_subs, 1):
            status = (sub.get("status") or "").lower()
            if status == "completed":
                current_day = "Completed"
            elif status == "hiatus":
                current_day = "Hiatus"
            else:
                current_day = sub.get("release_day") or "Hiatus"
            
            # Show day header if it changed OR if it's the first sub on this page
            if current_day != last_day:
                if current_day == "Hiatus":
                    emoji = "💤"
                    day_display = "Hiatus"
                elif current_day == "Completed":
                    emoji = "✅"
                    day_display = "Completed"
                else:
                    emoji = emoji_today if current_day == today_name else emoji_other
                    day_display = f"{current_day} (Today)" if current_day == today_name else current_day
                
                content += f"\n### {emoji} {day_display}\n"
                last_day = current_day

            url = _clean_url(sub.get("series_url") or "")
            title = overrides.get(url) or sub.get("series_title") or "Unknown Series"
            
            # Platform Emoji
            p_emoji = "📖"
            p_url = url.lower()
            if "piccoma" in p_url: p_emoji = "<:Piccoma:1478368704164134912>"
            elif "mecha" in p_url: p_emoji = "<:Mechacomic:1478369141957333083>"
            elif "jumptoon" in p_url: p_emoji = "<:Jumptoon:1478367963928068168>"
            
            # 🟢 NEW FORMAT: [Platform Emoji] [index]. [Series Title](Link) \n > <#[ChannelID]>
            s_url = sub.get("series_url") or "#"
            content += f"{p_emoji} {i + start}. [{title}]({s_url})\n> <#{sub.get('channel_id') or '0'}>\n"

        if not content:
            content = "> *No scheduled series subscriptions found.*"

        # 1. Platform Dropdown
        dropdown = {
            "type": 3,
            "custom_id": f"v2Dash_Filter|G:{group_name}",
            "placeholder": "All Platforms",
            "options": [
                {"label": "All Platforms", "value": "all", "default": not platform_filter or platform_filter == 'all'},
                {"label": "Piccoma", "value": "piccoma", "default": platform_filter == "piccoma", "emoji": {"id": "1478368704164134912"}},
                {"label": "Mechacomic", "value": "mecha", "default": platform_filter == "mecha", "emoji": {"id": "1478369141957333083"}},
                {"label": "Jumptoon", "value": "jumptoon", "default": platform_filter == "jumptoon", "emoji": {"id": "1478367963928068168"}}
            ]
        }

        # 2. Detail Selection Dropdown
        options: list = []
        for j, sub in enumerate(visible_subs, 1):
            url = _clean_url(sub.get("series_url") or "")
            title = overrides.get(url) or sub.get("series_title") or "Unknown Series"
            label = f"{j + start}. {title}"
            options.append({
                "label": label[:100],
                "value": sub['series_id']
            })

        # 🟢 HARD GUARD — Discord rejects 0-option selects (400 Bad Request)
        if not options:
            detail_rows = []
        else:
            detail_select = {
                "type": 3,
                "custom_id": f"v2Dash_Detail_Select|G:{group_name}",
                "placeholder": "View Details",
                "options": options[:25] # Cap at 25 per Discord limits
            }
            detail_rows = [{"type": 1, "components": [detail_select]}]

        # 3. Navigation Buttons Row
        pagination_row: dict[str, Any] = {"type": 1, "components": []}
        
        # 🟢 Conditional Previous Button
        if page > 0:
            pagination_row["components"].append({
                "type": 2, "style": 2, "label": "⬅️ Previous",
                "custom_id": f"v2Dash_Pg|P:{page-1}|F:{platform_filter or 'all'}|G:{group_name}"
            })
        
        # 🟢 Conditional Next Button
        if end < total:
            pagination_row["components"].append({
                "type": 2, "style": 2, "label": "Next ➡️",
                "custom_id": f"v2Dash_Pg|P:{page+1}|F:{platform_filter or 'all'}|G:{group_name}"
            })

        # Back to Dashboard Home
        pagination_row["components"].append({
            "type": 2, "style": 2, "label": "Back to Dashboard",
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
                "flags": 32768,
                "components": components
            }
        }

    async def get_settings_titles_payload(self, group_name: str):
        """V2 payload for Series Titles — scoped to the current group's subscriptions only."""
        from app.services.group_manager import load_group, _clean_url
        
        group_data = load_group(group_name)
        subs = group_data.get("subscriptions", [])
        overrides = group_data.get("title_overrides", {})

        inner = [
            {"type": 10, "content": f"# ✏️ {group_name} — Series Titles"},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": "Rename your group's series for better display in the dashboard and pings."},
        ]

        if not subs:
            inner.append({"type": 10, "content": "*No subscriptions in this group yet.*"})
        else:
            seen_ids = set()
            lines = []
            options = []
            for sub in subs:
                s_id = sub.get("series_id")
                if not s_id or s_id in seen_ids:
                    continue
                seen_ids.add(s_id)

                url = _clean_url(sub.get("series_url") or "")
                original = sub.get("series_title") or "Unknown"
                custom = overrides.get(url)
                label = custom or original
                display = f"**{custom}** (was: {original})" if custom else f"**{original}**"
                lines.append(f"{display}\n-# S-ID: {s_id}")

                options.append({
                    "label": label[:100],
                    "value": s_id,
                    "description": (sub.get("platform") or "Unknown").capitalize()[:100],
                    "emoji": {"name": "✏️"}
                })

            if lines:
                inner.append({"type": 10, "content": "\n".join(lines)[:3900]})
            if options:
                inner.append({
                    "type": 1,
                    "components": [{
                        "type": 3,
                        "custom_id": f"v2_settings_titles_rename|G:{group_name}",
                        "placeholder": "Select a series to rename...",
                        "options": options[:25]
                    }]
                })

        inner.append({"type": 14, "divider": True, "spacing": 1})
        inner.append({
            "type": 1,
            "components": [
                {"type": 2, "style": 2, "label": "Back", "custom_id": "v2_btn_settings"},
                {"type": 2, "style": 2, "label": "Dashboard", "custom_id": "v2Dash_Home"}
            ]
        })

        return {
            "type": 7,
            "data": {
                "flags": 32768,
                "components": [{"type": 17, "components": inner}]
            }
        }


    async def get_settings_notify_payload(self, user_id: int, guild):
        """V2 payload for Notification Recipients settings."""
        from app.services.settings_service import SettingsService, NOTIFY_LIMIT
        settings = SettingsService()
        targets = await settings.get_notify_targets(user_id)

        inner = [
            {"type": 10, "content": "# 🔔 Notification Recipients"},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": "Who gets pinged when your chapters finish."},
        ]

        if not targets:
            inner.append({"type": 10, "content": "*No targets set — only you will be pinged.*"})
        else:
            lines = []
            for t in targets:
                mention = f"<@{t['id']}>" if t["type"] == "user" else f"<@&{t['id']}>"
                name = ""
                if guild:
                    if t["type"] == "user":
                        member = guild.get_member(int(getattr(t["id"], "id", t["id"])))
                        if member: name = f" (`@{member.display_name}`)"
                    else:
                        role = guild.get_role(int(getattr(t["id"], "id", t["id"])))
                        if role: name = f" (`{role.name}`)"
                lines.append(f"• {mention}{name}")
            inner.append({"type": 10, "content": f"**Targets ({len(targets)}/{NOTIFY_LIMIT})**\n" + "\n".join(lines)})

        at_limit = len(targets) >= NOTIFY_LIMIT

        # Add User Select (V2 supports type 5 = User Select)
        if not at_limit:
            inner.append({
                "type": 1,
                "components": [{
                    "type": 5,  # USER_SELECT
                    "custom_id": f"v2_settings_notify_add_user_{user_id}",
                    "placeholder": "➕ Add a user…",
                    "min_values": 1,
                    "max_values": 1
                }]
            })
            inner.append({
                "type": 1,
                "components": [{
                    "type": 6,  # ROLE_SELECT
                    "custom_id": f"v2_settings_notify_add_role_{user_id}",
                    "placeholder": "➕ Add a role…",
                    "min_values": 1,
                    "max_values": 1
                }]
            })

        if targets:
            remove_options = []
            for t in targets:
                if guild:
                    if t["type"] == "user":
                        member = guild.get_member(int(getattr(t["id"], "id", t["id"])))
                        label = f"@{member.display_name}" if member else f"User: {t['id']}"
                        emoji = {"name": "👤"}
                    else:
                        role = guild.get_role(int(getattr(t["id"], "id", t["id"])))
                        label = f"Role: {role.name}" if role else f"Role: {t['id']}"
                        emoji = {"name": "🎭"}
                else:
                    label = f"{t['type'].capitalize()}: {t['id']}"
                    emoji = {"name": "👤" if t["type"] == "user" else "🎭"}

                remove_options.append({
                    "label": label[:100],
                    "value": f"{t['type']}:{t['id']}",
                    "emoji": emoji
                })

            if remove_options:
                inner.append({
                    "type": 1,
                    "components": [{
                        "type": 3,
                        "custom_id": f"v2_settings_notify_remove_{user_id}",
                        "placeholder": "➖ Remove a target…",
                        "options": remove_options[:25]
                    }]
                })

        inner.append({"type": 14, "divider": True, "spacing": 1})
        inner.append({
            "type": 1,
            "components": [
                {"type": 2, "style": 2, "label": "Back", "custom_id": "v2_btn_settings"},
                {"type": 2, "style": 2, "label": "Dashboard", "custom_id": "v2Dash_Home"}
            ]
        })

        return {
            "type": 7,
            "data": {
                "flags": 32768,
                "components": [{"type": 17, "components": inner}]
            }
        }

    async def get_sub_info_payload(self, group_name: str, series_id: str):
        """Generates the metadata-rich 'Subscription Info' panel for a series."""
        from app.services.group_manager import load_group, _clean_url
        group_data = load_group(group_name)
        subs = group_data.get("subscriptions", [])
        
        # 🟢 Hardened ID Matching (Cast both to string to be safe with older numeric IDs)
        sub = next((s for s in subs if str(s.get("series_id")) == str(series_id)), None)
        
        if not sub:
            logger.error(f"Subscription Info Error: Series {series_id} not found in group {group_name}")
            return {
                "type": 7,
                "data": {
                    "flags": 32768,
                    "components": [{"type": 17, "components": [{"type": 10, "content": f"❌ **Subscription not found.**\nSeries ID: `{series_id}`"}]}]
                }
            }

        url = _clean_url(sub.get("series_url") or "")
        overrides = group_data.get("title_overrides", {})
        custom_title = overrides.get(url)
        original_title = sub.get("series_title", "Unknown")
        
        # Status Logic (Derive from day/metadata)
        status = (sub.get("status") or "").lower()
        if status == "completed":
            status_text = "✅ Completed"
        elif status == "hiatus":
            status_text = "💤 Hiatus"
        else:
            day = sub.get("release_day") or "Hiatus"
            status_text = f"🟢 Ongoing (Release at {day})"
        # (Future: check a dedicated 'status' field if we ever add it to the scraper/sub)
        
        # Resolve Names for monospaced display (Mentions don't work in code blocks)
        channel_id = sub.get('channel_id')
        channel_name = f"#{channel_id}"
        if channel_id:
            ch = self.bot.get_channel(int(getattr(channel_id, "id", channel_id)))
            if ch: channel_name = f"#{ch.name}"
            
        user_name = "Not Found"
        if sub.get("added_by"):
            try:
                # 🟢 S-GRADE: Use fetch_user (API) instead of get_user (Cache)
                user_id = int(getattr(sub["added_by"], "id", sub["added_by"]))
                u = await self.bot.fetch_user(user_id)
                user_name = f"@{u.display_name}" if u else f"ID: {sub['added_by']}"
            except:
                user_name = f"ID: {sub['added_by']}"

        # Aligned Labels Logic (Dynamic padding for 3-space gap)
        label_map = {
            "Original Title": original_title,
            f"{group_name}'s Title": custom_title or "Not Found",
            "Platform": sub.get("platform", "Unknown").capitalize(),
            "Channel": channel_name
        }
        
        if sub.get("added_at"):
            try:
                iso_str = sub["added_at"].replace("Z", "+00:00")
                date_obj = datetime.datetime.fromisoformat(iso_str)
                label_map["Subscribed at"] = date_obj.strftime("%Y-%m-%d")
            except: pass
        
        if sub.get("added_by"):
            label_map["Subscribed by"] = user_name

        # Calculate perfect width
        max_label_len = max(len(k) for k in label_map.keys())
        target_width = max_label_len + 3 

        def align(label, value):
            padding = " " * (target_width - len(label))
            return f"{label}{padding}| {value}"

        details = [align(k, v) for k, v in label_map.items()]

        # 🟢 WRAP IN CODE BLOCK
        content = "```\n" + "\n".join(details) + "\n```"

        # Header & Divider
        header_text = f"# <:Series_Subscription:1488496671091462215> Subscription Info"
        status_line = f"-# **Status** | {status_text}"
        
        # Buttons Row
        action_row = {
            "type": 1,
            "components": [
                {
                    "type": 2, "style": 2, "label": "Back", "custom_id": "v2Dash_Home"
                },
                {
                    "type": 2, "style": 2, "custom_id": f"v2Dash_Sub_Delete_Start|G:{group_name}|S:{series_id}",
                    "emoji": {"id": "1488511488842006750"}
                }
            ]
        }

        container_components = [
            {"type": 10, "content": header_text},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": status_line},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": content},
            {"type": 14, "divider": True, "spacing": 1},
            action_row
        ]

        return {
            "type": 7, # UPDATE_MESSAGE
            "data": {
                "components": [
                    {"type": 17, "components": container_components}
                ]
            }
        }

    async def finalize_sub_removal(self, interaction: discord.Interaction, group_name: str, series_id: str, reason: str):
        """Standardizes the backend deletion and administrative reporting for auditing."""
        from app.services.group_manager import load_group, remove_subscription, _clean_url
        group_data = load_group(group_name)
        subs = group_data.get("subscriptions", [])
        sub = next((s for s in subs if str(s.get("series_id")) == str(series_id)), None)
        
        if not sub:
            return await interaction.response.send_message("❌ Failed to delete: Subscription not found or already removed.", ephemeral=True)

        url = sub.get("series_url")
        title = sub.get("series_title", "Unknown")
        
        # 1. DELETE FROM DISK
        remove_subscription(group_name, url)
        
        # 2. REPORT TO AUDIT CHANNEL (1488459998429446226)
        audit_channel_id = 1488459998429446226
        try:
            audit_channel = self.bot.get_channel(audit_channel_id) or await self.bot.fetch_channel(audit_channel_id)
            if audit_channel:
                report_payload = {
                    "embeds": [{
                        "title": "🗑️ Subscription Deleted",
                        "color": 0xe74c3c, # Danger Red
                        "fields": [
                            {"name": "Series", "value": f"[{title}]({url})", "inline": False},
                            {"name": "Group", "value": f"`{group_name}`", "inline": True},
                            {"name": "User", "value": f"<@{interaction.user.id}>", "inline": True},
                            {"name": "Reason", "value": f"**{reason}**", "inline": False}
                        ],
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }]
                }
                # Using Webhook/Bot request for robustness
                route = discord.http.Route('POST', f'/channels/{audit_channel_id}/messages')
                await self.bot.http.request(route, json=report_payload)
        except Exception as e:
            logger.error(f"Failed to send deletion audit report: {e}")

        # 3. SHOW SUCCESS UI
        success_payload = {
            "type": 7, # UPDATE_MESSAGE
            "data": {
                "flags": 32832,
                "components": [
                    {
                        "type": 17,
                        "components": [
                            {"type": 10, "content": f"🗑️ **Subscription Deleted Successfully.**\nThe series tracking has been removed from **{group_name}**."}
                        ]
                    },
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2, "style": 2, "label": "Back to Dashboard", 
                                "custom_id": "v2Dash_Home"
                            }
                        ]
                    }
                ]
            }
        }
        
        # If it was a Modal response, we must use followup if it was already acknowledged, 
        # but since we are triggered by a Modal Submit (type 5), we can respond with UPDATE_MESSAGE (type 7) directly.
        try:
            await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=success_payload)
        except Exception as e:
            # Fallback PATCH if interaction already acknowledged
            logger.debug(f"[Dashboard] POST callback failed, trying PATCH: {e}")
            try:
                route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
                await self.bot.http.request(route, json=success_payload["data"])
            except Exception as e2:
                logger.error(f"[Dashboard] Sub removal PATCH failed: {e2}", exc_info=True)

        # 4. AUTO-DELETE AFTER 5 MINUTES (300s)
        async def delayed_delete():
            await asyncio.sleep(300)
            try:
                # Use standard DELETE route for the original message
                if interaction.message:
                    route = discord.http.Route('DELETE', f'/channels/{interaction.channel_id}/messages/{interaction.message.id}')
                    await self.bot.http.request(route)
            except:
                pass # Already deleted or navigated away
        
        asyncio.create_task(delayed_delete())
    
    async def _trigger_redownload(self, view, interaction: discord.Interaction):
        """Helper to reset view and re-queue tasks for a fresh download."""
        req_id = view.req_id
        
        # 🟢 S-GRADE: Filter only FAILED tasks to avoid wasting resources on successful ones
        failed_tasks = [t for t in view.active_tasks if t.status == TaskStatus.FAILED]
        
        if not failed_tasks:
            try:
                await interaction.followup.send("✅ All selected chapters have already completed successfully. Nothing to retry.", ephemeral=True)
            except: pass
            return

        # 1. Forensic Deletion (Remove existing folders on Drive) for FAILED tasks only
        uploader = self.bot.task_queue.uploader
        redis = self.bot.task_queue.redis
        
        if uploader:
            logger.info(f"[{req_id}] 🗑️ Retrying: Deleting existing assets for {len(failed_tasks)} failed tasks...")
            for task in failed_tasks:
                # 🟢 S-GRADE: Prioritize deletion by ID if we have it
                if task.pre_created_folder_id:
                    logger.info(f"[{req_id}] Deleting folder by ID: {task.pre_created_folder_id}")
                    uploader.delete_file(task.pre_created_folder_id)
                else:
                    # Search fallback by name
                    folder_name = task.folder_name
                    main_id = task.main_folder_id or Settings.GDRIVE_ROOT_ID
                    
                    existing_id = uploader.find_folder(folder_name, main_id)
                    if existing_id: uploader.delete_file(existing_id)
                    
                    temp_name = f"[Uploading] {folder_name}"
                    temp_id = uploader.find_folder(temp_name, main_id)
                    if temp_id: uploader.delete_file(temp_id)

        # 2. Reset View State
        view.processing_mode = True
        view.phases["download"] = "loading"
        view.trigger_refresh()
        
        # 3. Re-queue only the failed tasks
        new_tasks = []
        # Keep the completed tasks as they are in the view, only replace the failed ones with their new instances
        task_map = {t.episode_id: t for t in failed_tasks}
        
        updated_active_tasks = []
        for t in view.active_tasks:
            if t.episode_id in task_map:
                # 🟢 CRITICAL: Clear the Redis Active Task Flag to allow deduplication to pass
                key = f"{t.series_id_key}:{t.episode_id}"
                await redis.remove_active_task(key)

                # Reset task object
                t.status = TaskStatus.QUEUED
                t.pre_created_folder_id = None
                t.share_link = None
                t.error_message = None
                t.source = "dashboard"
                
                # 🟢 S-GRADE: Prune existing_links from view to ensure fresh UI
                if hasattr(view, 'existing_links') and t.chapter_str in view.existing_links:
                    del view.existing_links[t.chapter_str]
                
                new_t = await self.bot.task_queue.add_task(t)
                updated_active_tasks.append(new_t)
            else:
                updated_active_tasks.append(t)
        
        view.active_tasks = updated_active_tasks
        asyncio.create_task(view.monitor_tasks())

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions."""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            logger.info(f"[Dashboard] 🖱️ Component Interaction: {custom_id} (User: {interaction.user.id}, Channel: {interaction.channel_id})")
            
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
                
                # 🟢 S-GRADE: Pretty Names Mapping
                mapping = {"piccoma": "Piccoma", "mecha": "Mechacomic", "jumptoon": "Jumptoon"}
                platform_name = mapping.get(platform, platform.capitalize())
                
                modal_payload = {
                    "type": 9,
                    "data": {
                        "custom_id": f"v2_modal_{platform}",
                        "title": f"{platform_name}'s Downloader",
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
                                "label": f"Add {platform_name} link here:",
                                "component": {
                                    "type": 4, "custom_id": "url_input", "style": 1,
                                    "placeholder": f"Paste {platform_name} URL...", "required": True
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
                # Edit dashboard inline with V2 settings menu
                payload = {
                    "type": 7,  # UPDATE_MESSAGE
                    "data": {
                        "flags": 32768,  # V2
                        "components": [{
                            "type": 17,
                            "components": [
                                {"type": 10, "content": "# ⚙️ Verzue Settings"},
                                {"type": 14, "divider": True, "spacing": 1},
                                {"type": 10, "content": "Select a category to manage your preferences:"},
                                {
                                    "type": 1,
                                    "components": [
                                        {"type": 2, "style": 2, "label": "Notifications", "emoji": {"name": "🔔"}, "custom_id": "v2_settings_nav_notify"},
                                        {"type": 2, "style": 2, "label": "Series Titles", "emoji": {"name": "✏️"}, "custom_id": "v2_settings_nav_titles"},
                                    ]
                                },
                                {"type": 14, "divider": True, "spacing": 1},
                                {
                                    "type": 1,
                                    "components": [
                                        {"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}
                                    ]
                                }
                            ]
                        }]
                    }
                }
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            # --- Settings Navigation ---
            elif custom_id == "v2_settings_nav_titles":
                # 🟢 Determine group from channel/guild context
                state = self.bot.app_state
                channel_id = interaction.channel.id if interaction.channel else 0
                guild_id = interaction.guild.id if interaction.guild else 0
                group_name = state.server_map.get(channel_id) or state.server_map.get(guild_id)
                
                if not group_name:
                    payload = {
                        "type": 7,
                        "data": {
                            "flags": 32768,
                            "components": [{
                                "type": 17,
                                "components": [
                                    {"type": 10, "content": "# ❌ No Group Linked"},
                                    {"type": 14, "divider": True, "spacing": 1},
                                    {"type": 10, "content": "This server is not linked to a group profile. Use `/register-server` first."},
                                    {"type": 1, "components": [
                                        {"type": 2, "style": 2, "label": "Back", "custom_id": "v2_btn_settings"}
                                    ]}
                                ]
                            }]
                        }
                    }
                    await self.bot.http.request(
                        discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                        json=payload
                    )
                    return
                
                payload = await self.get_settings_titles_payload(group_name)
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            elif custom_id == "v2_settings_nav_notify":
                payload = await self.get_settings_notify_payload(interaction.user.id, interaction.guild)
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            # --- Channel Selection for Subscription ---
            elif custom_id.startswith("v2_select_sub_channel_"):
                # Format: v2_select_sub_channel_{platform}|{url_or_token}
                parts = custom_id.replace("v2_select_sub_channel_", "").split("|", 1)
                platform = parts[0]
                token_or_url = parts[1] if len(parts) > 1 else ""
                url = self._resolve_url(token_or_url)
                
                # Extract selected channel ID
                values = interaction.data.get("values", [])
                if not values:
                    return # Should not happen
                
                target_channel_id = int(values[0])
                await self.finalize_subscription(interaction, platform, url, target_channel_id)

            # --- Registration Hook ---
            elif custom_id.startswith("v2_btn_sub_confirm_yes_"):
                # Format: v2_btn_sub_confirm_yes_{platform}|{url_or_token}
                parts = custom_id.replace("v2_btn_sub_confirm_yes_", "").split("|", 1)
                platform = parts[0]
                token_or_url = parts[1] if len(parts) > 1 else ""
                url = self._resolve_url(token_or_url)
                await self.finalize_subscription(interaction, platform, url, interaction.channel_id)
            elif custom_id.startswith("v2_btn_sub_confirm_no_"):
                # Format: v2_btn_sub_confirm_no_{platform}|{url_or_token}
                parts = custom_id.replace("v2_btn_sub_confirm_no_", "").split("|", 1)
                platform = parts[0]
                token_or_url = parts[1] if len(parts) > 1 else ""
                url = self._resolve_url(token_or_url)
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

            # --- View Series Detail (Subscription Info Panel) ---
            elif custom_id.startswith("v2Dash_Detail|") or custom_id.startswith("v2Dash_Detail_Select|"):
                if custom_id.startswith("v2Dash_Detail_Select|"):
                    series_id = interaction.data.get("values", [None])[0]
                    parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                    group_name = parts.get("G")
                else:
                    parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                    series_id = parts.get("S")
                    group_name = parts.get("G")
                
                if not series_id: return
                
                # 🟢 S-GRADE: Show Subscription Info panel instead of Extractor Dashboard
                payload = await self.get_sub_info_payload(group_name, series_id)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- Back to Home ---
            elif custom_id == "v2Dash_Home":
                payload = await self.get_dashboard_payload(interaction, is_update=True)
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=payload)

            # --- Settings: Rename Series Title (open modal) ---
            elif custom_id.startswith("v2_settings_titles_rename|"):
                from app.services.group_manager import load_group, _clean_url
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                s_id = interaction.data.get("values", [None])[0]
                if not s_id or s_id == "__none__" or not group_name: return
                
                group_data = load_group(group_name)
                subs = group_data.get("subscriptions", [])
                sub = next((s for s in subs if str(s.get("series_id")) == str(s_id)), None)
                if not sub: return
                
                url = _clean_url(sub.get("series_url") or "")
                overrides = group_data.get("title_overrides", {})
                current = overrides.get(url) or sub.get("series_title") or ""
                
                modal_payload = {
                    "type": 9,
                    "data": {
                        "custom_id": f"v2_settings_titles_modal|G:{group_name}|U:{url}",
                        "title": "Rename Series",
                        "components": [{
                            "type": 1,
                            "components": [{
                                "type": 4,
                                "custom_id": "new_title",
                                "label": "New Display Title",
                                "style": 1,
                                "value": current[:100],
                                "min_length": 1,
                                "max_length": 100,
                                "required": True
                            }]
                        }]
                    }
                }
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=modal_payload
                )

            # --- Settings: Modal submit for rename ---
            elif custom_id.startswith("v2_settings_titles_modal|"):
                from app.services.group_manager import set_title_override
                parts = dict(p.split(":", 1) for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                url = parts.get("U")
                if not group_name or not url: return
                
                new_title = ""
                for row in interaction.data.get("components", []):
                    for comp in row.get("components", []):
                        if comp.get("custom_id") == "new_title":
                            new_title = comp.get("value", "").strip()
                
                if new_title:
                    set_title_override(group_name, url, new_title)
                
                payload = await self.get_settings_titles_payload(group_name)
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            # --- Settings: Add notify user/role ---
            elif custom_id.startswith("v2_settings_notify_add_user_") or custom_id.startswith("v2_settings_notify_add_role_"):
                from app.services.settings_service import SettingsService
                settings = SettingsService()
                ttype = "user" if "_add_user_" in custom_id else "role"
                target_id = interaction.data.get("values", [None])[0]
                if not target_id: return
                ok, _ = await settings.add_notify_target(interaction.user.id, ttype, int(target_id))
                payload = await self.get_settings_notify_payload(interaction.user.id, interaction.guild)
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            # --- Settings: Remove notify target ---
            elif custom_id.startswith("v2_settings_notify_remove_"):
                from app.services.settings_service import SettingsService
                settings = SettingsService()
                val = interaction.data.get("values", [None])[0]
                if not val: return
                ttype, tid = val.split(":", 1)
                await settings.remove_notify_target(interaction.user.id, ttype, tid)
                payload = await self.get_settings_notify_payload(interaction.user.id, interaction.guild)
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=payload
                )

            # --- Interaction Redirection (Original Logic) ---
                # --- Delete Subscription Workflow ---
            elif custom_id.startswith("v2Dash_Sub_Delete_Start|"):
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                series_id = parts.get("S")
                
                # Step 1: Confirmation
                confirm_payload = {
                    "type": 7, # UPDATE_MESSAGE
                    "data": {
                        "flags": 32768,
                        "components": [
                            {
                                "type": 17,
                                "components": [
                                    {"type": 10, "content": "Would you like to delete this series subscription?"}
                                ]
                            },
                            {
                                "type": 1,
                                "components": [
                                    {
                                        "type": 2, "style": 4, "label": "Yes", 
                                        "custom_id": f"v2Dash_Sub_Delete_Confirm|G:{group_name}|S:{series_id}"
                                    },
                                    {
                                        "type": 2, "style": 2, "label": "Cancel", 
                                        "custom_id": f"v2Dash_Detail|G:{group_name}|S:{series_id}"
                                    }
                                ]
                            }
                        ]
                    }
                }
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=confirm_payload)

            elif custom_id.startswith("v2Dash_Sub_Delete_Confirm|"):
                # Step 2 & 3: IMMEDIATELY SHOW POPUP (Like Select Chapters)
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                series_id = parts.get("S")
                
                modal_payload = {
                    "type": 9, # MODAL
                    "data": {
                        "custom_id": f"v2Dash_Sub_Delete_Modal|G:{group_name}|S:{series_id}",
                        "title": "Reason Selection",
                        "components": [
                            {
                                "type": 18, # Specialized Radio Group Container
                                "label": "Choose Deletion Reason",
                                "component": {
                                    "type": 21, # Radio Group
                                    "custom_id": "sel_sub_delete_reason",
                                    "options": [
                                        {"label": "Not Interested", "value": "Not Interested", "emoji": {"name": "😒"}},
                                        {"label": "Hiatus", "value": "Hiatus", "emoji": {"name": "⏸️"}},
                                        {"label": "We're dropping this series", "value": "We're dropping this series", "emoji": {"name": "📉"}},
                                        {"label": "Others", "value": "others_modal", "emoji": {"name": "✏️"}}
                                    ],
                                    "required": True
                                }
                            },
                            {
                                "type": 18, # Container for Text Input
                                "label": "Write Reason (Optional for others)",
                                "component": {
                                    "type": 4, # TEXT_INPUT
                                    "custom_id": "txt_sub_delete_reason",
                                    "style": 2, # Paragraph
                                    "placeholder": "Type your detailed reason here...",
                                    "required": False,
                                    "min_length": 5
                                }
                            }
                        ]
                    }
                }
                # Directly response with MODAL instead of UPDATE_MESSAGE
                await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=modal_payload)


            elif custom_id.startswith("v2_btn_sub_move_yes_"):
                # "Yes" to "Move this series to new channel?" -> Show channel select inline
                parts = custom_id.replace("v2_btn_sub_move_yes_", "").split("|", 1)
                platform = parts[0]
                token_or_url = parts[1] if len(parts) > 1 else ""
                url = self._resolve_url(token_or_url)
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
                    # 🟢 Premium V2 Error UI
                    error_payload = {
                        "flags": 32832, # Ephemeral + V2
                        "components": [
                            {
                                "type": 17, # Container
                                "accent_color": 0xe74c3c, # Red
                                "components": [
                                    {"type": 10, "content": "### ❌ Session Expired or Process Conflict"},
                                    {"type": 14, "divider": True, "spacing": 1},
                                    {
                                        "type": 9, # Section
                                        "components": [
                                            {"type": 10, "content": "This dashboard session is no longer active.\n- **Timeout**: Inactive for >15 mins.\n- **Conflict**: Multiple processes running."}
                                        ],
                                        "accessory": {
                                            "type": 2, 
                                            "style": 3, # Success (Green)
                                            "label": "Back to Dashboard",
                                            "emoji": {"id": "1480951111614730302", "name": "Success"},
                                            "custom_id": "v2Dash_Home"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                    try:
                        if interaction.response.is_done():
                            # Webhook PATCH for already acknowledged interactions
                            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                            await self.bot.http.request(route, json=error_payload)
                        else:
                            # Initial POST if not responded yet
                            callback_payload = {"type": 4, "data": error_payload}
                            route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                            await self.bot.http.request(route, json=callback_payload)
                    except Exception as e:
                        # Final safety fallback
                        try: 
                            logger.debug(f"[Dashboard] Session error dispatch failed, trying followup: {e}")
                            await interaction.followup.send("❌ Session Expired. Please restart the dashboard.", ephemeral=True)
                        except Exception as e2:
                            logger.error(f"[Dashboard] Final safety followup failed: {e2}", exc_info=True)
                    return
                
                # 🔄 Reset session timer on every interaction
                view.last_interaction_time = time.time() 

                # NEW: Error Report Logic (Launches Modal)
                if custom_id.startswith("btn_report_error_"):
                    modal_payload = {
                        "type": 9, # MODAL_DISPLAY
                        "data": {
                            "custom_id": f"modal_report_error_{req_id}",
                            "title": "Report Download Error",
                            "components": [
                                {
                                    "type": 18, # Radio Group Container
                                    "label": "Select Error Type",
                                    "component": {
                                        "type": 21, # Radio Group
                                        "custom_id": "sel_error_type",
                                        "options": [
                                            {"label": "Stitching Problem", "value": "Stitching Problem"},
                                            {"label": "Missing Page", "value": "Missing Page"},
                                            {"label": "Others", "value": "Others", "default": True}
                                        ],
                                        "required": True
                                    }
                                },
                                {
                                    "type": 18, # Container for Text Input
                                    "label": "Additional Details",
                                    "component": {
                                        "type": 4, # TEXT_INPUT
                                        "custom_id": "error_desc",
                                        "style": 2, # Paragraph
                                        "placeholder": "Provide more details if needed (e.g., Page 5 is blurry)...",
                                        "required": False,
                                        "min_length": 5,
                                        "max_length": 500
                                    }
                                }
                            ]
                        }
                    }
                    try:
                        return await self.bot.http.request(
                            discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                            json=modal_payload
                        )
                    except Exception as e:
                        logger.error(f"Failed to launch Error Modal: {e}")
                        # 🟢 S-GRADE: Use followup if the initial response (modal) failed/expired
                        try:
                            if not interaction.response.is_done():
                                await interaction.response.send_message("❌ Failed to open report form. Please try again.", ephemeral=True)
                            else:
                                await interaction.followup.send("❌ Failed to open report form. Please try again.", ephemeral=True)
                        except: pass
                        return

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
                    
                    # 2. Execute re-download trigger
                    await self._trigger_redownload(view, interaction)
                    return

                # A. Clear Selections (Cancel SR)
                if custom_id.startswith("btn_clear_"):
                    view.interaction = interaction
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
                        else: 
                            logger.error(f"Failed to open selection menu: {e}")
                            try:
                                if not interaction.response.is_done():
                                    await interaction.response.send_message("❌ Failed to open menu. Please try again.", ephemeral=True)
                                else:
                                    await interaction.followup.send("❌ Failed to open menu. Please try again.", ephemeral=True)
                            except: pass
                        return

                # B. Deprecated Handler for old dropdown (safeguard)
                elif custom_id.startswith("mode_select_"):
                    # Should no longer be triggered, log and ignore
                    logger.warning("Received deprecated mode_select_ component interaction")
                    view.interaction = interaction
                    return await interaction.response.defer()

                # C. Page Navigation
                elif custom_id.startswith("page_select_"):
                    # 🟢 DEFER IMMEDIATELY: Lazy loading can take > 3 seconds
                    view.interaction = interaction
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
                    view.interaction = interaction
                    if view.service_type == "mecha": 
                        try: 
                            browser = getattr(self.bot.task_queue, "browser_service", None)
                            if browser: browser.dec_session()
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
                    view.interaction = interaction
                    if view.service_type == "mecha": 
                        try:
                            browser = getattr(self.bot.task_queue, "browser_service", None)
                            if browser: browser.dec_session()
                        except: pass
                    UniversalDashboard.active_views.pop(req_id, None)
                    await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json={"type": 6})
                    try: await interaction.message.delete()
                    except: pass

                # E. Start Batch Process
                elif custom_id.startswith("btn_start_"):
                    view.interaction = interaction
                    try:
                        view.processing_mode = True
                        view.phases["analyze"] = "loading"
                        await view.update_view(interaction)
                        
                        asyncio.create_task(view.monitor_tasks())
                        from app.services.batch_controller import BatchController
                        tasks = await BatchController(self.bot).prepare_batch(interaction, sorted(list(view.selected_indices)), view.all_chapters, view.title, view.url, view_ref=view, series_id=view.series_id, original_title=view.original_title)
                        if tasks:
                            view.phases.update({"analyze":"done","purchase":"done","download":"loading"})
                            for t in tasks:
                                t.source = "dashboard"
                                view.active_tasks.append(t)
                                await self.bot.task_queue.add_task(t)
                            view.trigger_refresh()
                            
                            if getattr(view, "any_waiters", False):
                                try:
                                    await interaction.followup.send(
                                        "⏳ **Heads up!** One or more of these chapters are already being downloaded by someone else. "
                                        "You've been added to the notification list and will receive a ping the moment they're ready! ✅",
                                        ephemeral=True
                                    )
                                except: pass
                    except RuntimeError as e:
                        # 🟢 S-GRADE: Friendly Maintenance Feedback
                        view.processing_mode = False
                        view.trigger_refresh()
                        return await interaction.followup.send(f"⚠️ **Maintenance Mode**\n{str(e)}", ephemeral=True)
                    except Exception as e:
                        logger.error(f"Failed to start batch: {e}", exc_info=True)
                        # 🟢 S-GRADE: Dispatch to Admin Sentinel
                        await self.bot.dispatch_error(e, interaction=interaction)
                        return await interaction.followup.send(f"❌ **Error starting batch:** {e}", ephemeral=True)

        # --- Modal Submissions (URL Entry & Range Picker) ---
        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            
            # 🔴 NEW: Error Report Submission
            if custom_id.startswith("modal_report_error_"):
                from app.bot.common.view import UniversalDashboard
                req_id = custom_id.split("_")[-1]
                view: UniversalDashboard | None = UniversalDashboard.active_views.get(req_id)
                if not view: return
                view.interaction = interaction
                view.last_interaction_time = time.time()
                
                # Extract User Feedback (V2 Modal Structure)
                error_type = "Others"
                user_msg = "No additional details provided."
                
                try:
                    for row in interaction.data.get("components", []):
                        inner = row.get("component", {})
                        cid = inner.get("custom_id")
                        if cid == "sel_error_type":
                            error_type = inner.get("value", "Others")
                        elif cid == "error_desc":
                            user_msg = inner.get("value") or user_msg
                except Exception as e:
                    logger.error(f"Failed to parse Error Modal data: {e}")

                # 🟢 S-GRADE: Prepare Detailed Admin Embed
                admin_channel_id = Settings.ADMIN_LOG_CHANNEL_ID
                admin_channel = self.bot.get_channel(admin_channel_id)
                
                if admin_channel:
                    embed = discord.Embed(
                        title=f"🚨 Chapter Error Reported",
                        description=f"**Series:** [{view.title}]({view.url})\n**User:** {interaction.user.mention} ({interaction.user.name})",
                        color=0xe74c3c, # Red
                        timestamp=datetime.datetime.now(datetime.timezone.utc)
                    )
                    embed.add_field(name="Report Category", value=f"**{error_type}**", inline=True)
                    embed.add_field(name="User Description", value=user_msg, inline=False)
                    
                    # Inspect Task Failures
                    failed_chapters = [t for t in view.active_tasks if t.status == TaskStatus.FAILED]
                    if failed_chapters:
                        fail_text = ""
                        for t in failed_chapters:
                            error = t.error_message or "Unknown internal error."
                            fail_text += f"• **{t.chapter_str}**: `{error}`\n"
                        embed.add_field(name="Technical Failures", value=fail_text[:1024], inline=False)
                    else:
                        # Success case but user reported something (e.g. blurry images)
                        ch_list = ", ".join([t.chapter_str for t in view.active_tasks]) or "No active tasks"
                        embed.add_field(name="Chapters Involved", value=ch_list, inline=False)

                    embed.set_footer(text=f"Request ID: {req_id} | Series ID: {view.series_id}")
                    
                    # Add jump link to original message if available
                    if interaction.message:
                        embed.add_field(name="Context", value=f"[Jump to Message]({interaction.message.jump_url})", inline=False)
                    
                    await admin_channel.send(embed=embed)
                
                # 🟢 S-GRADE: Automatically trigger re-download for the user
                await self._trigger_redownload(view, interaction)
                
                # Friendly confirmation to user
                confirm_msg = (
                    "✅ **Report Sent!** Our team has been notified, and the system is automatically re-initiating "
                    "the download for these chapters. If the issue persists after this second attempt, "
                    "please ping the administrators for a manual review."
                )
                return await interaction.response.send_message(confirm_msg, ephemeral=True)

            elif custom_id.startswith("v2Dash_Sub_Delete_Modal|"):
                # Step 4: Final Modal Submission
                parts = dict(p.split(":") for p in custom_id.split("|")[1:])
                group_name = parts.get("G")
                series_id = parts.get("S")
                
                # Extract reason from modal data using V2 Type 18 structure
                final_reason = "Unknown Reason"
                try:
                    dropdown_val = ""
                    text_val = ""
                    for row in interaction.data.get("components", []):
                        inner = row.get("component", {})
                        cid = inner.get("custom_id")
                        if cid == "sel_sub_delete_reason":
                            dropdown_val = inner.get("value", "")
                        elif cid == "txt_sub_delete_reason":
                            text_val = inner.get("value", "")

                    if dropdown_val == "others_modal":
                        final_reason = f"Other: {text_val}" if text_val else "Other"
                    else:
                        final_reason = dropdown_val
                        if text_val: final_reason += f" ({text_val})"
                except Exception as e:
                    logger.error(f"Failed to parse Delete Modal data: {e}")
                
                return await self.finalize_sub_removal(interaction, group_name, series_id, final_reason)

            if custom_id.startswith("modal_select_"):
                req_id = custom_id.split("_")[-1]
                from app.bot.common.view import UniversalDashboard
                view: UniversalDashboard | None = UniversalDashboard.active_views.get(req_id)
                if not view: return
                view.interaction = interaction
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
                        except Exception as e:
                            logger.error(f"[Dashboard] No New Releases error dispatch failed: {e}", exc_info=True)
                            return
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

    async def handle_platform_modal(self, interaction: discord.Interaction, custom_id: str):
        """Processes the platform URL submission modal."""
        logger.info(f"[Dashboard] 📝 Modal Submission: {custom_id} (User: {interaction.user.id})")
        # 🟢 THE FIX: DEFER IMMEDIATELY to prevent 3s timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except:
            pass

        platform = custom_id.replace("v2_modal_", "")
        
        # 🟢 S-GRADE: Pretty Names Mapping
        mapping = {"piccoma": "Piccoma", "mecha": "Mechacomic", "jumptoon": "Jumptoon"}
        platform_name = mapping.get(platform, platform.capitalize())
        raw_url, action = "", "download"
        
        for row in interaction.data.get("components", []):
            inner = row.get("component", {})
            if inner.get("custom_id") == "action_radio": action = inner.get("value", "download") 
            elif inner.get("custom_id") == "url_input": raw_url = inner.get("value", "").strip()
        
        # 🟢 URL EXTRACTION (REGEX BASED)
        # Handle cases like "junk https://piccoma.com/series/id" 
        url = raw_url
        if raw_url:
            match = re.search(r'(https?://[^\s\"\'\<\>]+)', raw_url)
            if match:
                url = match.group(1)
                logger.info(f"[Dashboard] 📍 URL Extracted: '{raw_url}' -> '{url}'")
        
        platform_domains = {
            "mecha": "mechacomic.jp", 
            "jumptoon": "jumptoon.com", 
            "kakao": "kakao.com", 
            "kuaikan": "kuaikanmanhua.com", 
            "piccoma": "piccoma.com", 
            "acqq": "ac.qq.com"
        }
        expected_domain = platform_domains.get(platform)
        
        if expected_domain and expected_domain not in url.lower():
            # Use followup since we deferred. MUST USE V2 structure for V2-deferred messages.
            embed_payload = {
                "flags": 32768, # V2 (Removing 64/Ephemeral to test stability)
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
                                    "custom_id": "v2Dash_Home"
                               }
                            ]
                        }
                    ]
                }]
            }
            logger.warning(f"[Dashboard] ⛔ Domain Mismatch: Expected {expected_domain} for platform {platform}, got {url}")
            await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=embed_payload)
            return

        if action == "subscribe":
            # 🟢 INSTANT CHECK: Extract series_id from URL and check local records
            # Jumptoon: /contents/JT00085 or /series/JT00085
            series_id = None
            if "jumptoon" in url.lower():
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
            
            is_universal_server = (interaction.guild_id == 1419393318147719170)
            
            # 🟢 SCENARIO A: Already Subscribed (Instant Response)
            if is_already_here or (occupation_series and not is_universal_server):
                logger.info(f"INSTANT MATCH: series={series_id}, same={is_already_here}")
                trigger_components = []
                
                if is_already_here:
                    content = f"✅ **Series Found but um...**\nLooks like this series is already subscribed to this channel. Would you want to move this series to new channel?"
                    
                    # 🟢 SCENARIO A: Tokenize IDs to prevent 100-char overflow
                    token = self._store_url(url)
                    yes_id = f"v2_btn_sub_move_yes_{platform}|{token}"
                    no_id = f"v2_btn_sub_move_no_{platform}|{token}"
                    
                    # Hard-guard against overflow (Discord limit)
                    assert len(yes_id) <= 100, f"SCENARIO A yes_id overflow: {len(yes_id)} chars"
                    assert len(no_id) <= 100, f"SCENARIO A no_id overflow: {len(no_id)} chars"

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
                    
                    # 🟢 SCENARIO A (Occupied): Tokenize ID
                    token = self._store_url(url)
                    channel_select_id = f"v2_select_sub_channel_{platform}|{token}"
                    assert len(channel_select_id) <= 100, f"SCENARIO A chan_select overflow: {len(channel_select_id)} chars"

                    trigger_components = [
                        {"type": 10, "content": content},
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 8, # CHANNEL_SELECT
                                    "custom_id": channel_select_id,
                                    "channel_types": [0],
                                    "placeholder": "Select Channel"
                                }
                            ]
                        }
                    ]

                trigger_payload = {
                    "flags": 32832,
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
                # Clean up no-op line
                
                # 🟢 S-GRADE: Subscription Block Rule (Mar 25 Request)
                if status_label in ["Oneshot", "Completed", "Novel"]:
                    error_payload = {
                        "flags": 32832,
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
                
                # 🟢 SCENARIO B: Tokenize IDs to prevent 100-char overflow
                token = self._store_url(url)
                yes_id = f"v2_btn_sub_confirm_yes_{platform}|{token}"
                no_id = f"v2_btn_sub_confirm_no_{platform}|{token}"
                
                # Hard-guard against overflow (Discord limit)
                assert len(yes_id) <= 100, f"SCENARIO B yes_id overflow: {len(yes_id)} chars"
                assert len(no_id) <= 100, f"SCENARIO B no_id overflow: {len(no_id)} chars"
                
                trigger_components = [
                    {"type": 10, "content": content},
                    {
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 3, "label": "Yes", "custom_id": yes_id},
                            {"type": 2, "style": 2, "label": "No (Use another channel)", "custom_id": no_id},
                            {"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}
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
                logger.error(f"Failed to analyze for subscription: {e}", exc_info=True)
                error_p = {
                    "flags": 32768, 
                    "components": [{
                        "type": 17, 
                        "components": [
                            {"type": 10, "content": f"Come <@1216284053049704600>. New Error"},
                            {
                                "type": 1,
                                "components": [
                                    {
                                        "type": 2, "style": 2, "label": "Back to Dashboard",
                                        "custom_id": "v2Dash_Home"
                                    }
                                ]
                            }
                        ]
                    }]
                }
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original'), json=error_p)
            return

        _dashboard_sent = False
        analyzing_payload = {
            "flags": 32768,
            "components": [{
                "type": 17,
                "components": [{
                    "type": 10,
                    "content": f"🔍 **Analyzing {platform_name} Link:**\n`{url}`\n*Fetching metadata, please wait...*"
                }]
            }]
        }
        
        try:
            route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
            await self.bot.http.request(route, json=analyzing_payload)
        except discord.HTTPException as e:
            if e.status == 429:
                logger.warning(f"[Dashboard] Rate limited on analyzing PATCH, continuing anyway...")
                # discord.py already retried — we can safely continue
            else:
                logger.error(f"[Dashboard] Interaction expired or PATCH failed: {e}", exc_info=True)
                return
        except Exception as e:
            logger.error(f"[Dashboard] Analyzing PATCH failed: {type(e).__name__}: {e}", exc_info=True)
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
            
            # 🛡️ Safety check (SY_002)
            if not scraper:
                logger.error(f"[{req_id}] ❌ Unsupported platform URL: {url}")
                raise MechaException(f"Unsupported platform URL: {url}", code="SY_002")

            # 🟢 Every Provider is now S-Grade Async
            logger.debug(f"[{req_id}] Calling scraper.get_series_info(url={url})")
            data = await scraper.get_series_info(url)
                
            # Diagnostic Log before unpack
            logger.debug(f"[{req_id}] get_series_info returned {len(data)} values: {[type(v).__name__ for v in data]}")
            logger.info(f"[{req_id}] ✅ Handoff: Metadata retrieved successfully.")
            
            try:
                title, total_chapters, chapter_list, image_url, series_id, \
                    release_day, release_time, status_label, genre_label = data
                logger.info(f"[{req_id}] ✅ Unpack OK: title='{title}', chapters={len(chapter_list)}")
            except Exception as e:
                logger.error(f"[{req_id}] ❌ UNPACK FAILED ({len(data)} values): {e}", exc_info=True)
                raise
                
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
            channel_id_origin = interaction.channel.id
            state = self.bot.app_state
            group_name = state.server_map.get(channel_id_origin) or state.server_map.get(guild_id)
            original_title = title # Keep the one from scraper
            if group_name:
                from app.services.group_manager import get_title_override
                custom_title = get_title_override(group_name, url)
                if custom_title:
                    logger.info(f"[{req_id}] 🏷️ Title Override: '{title}' → '{custom_title}' (Group: {group_name})")
                    title = custom_title
            
            try:
                ctx_data = {
                    'url': url, 
                    'title': title, 
                    'original_title': original_title,
                    'chapters': chapter_list, 
                    'total_chapters': total_chapters, 
                    'image_url': image_url, 
                    'series_id': series_id, 
                    'status_label': status_label,
                    'genre_label': genre_label,
                    'req_id': req_id, 
                    'user': interaction.user
                }
                service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
                
                view = UniversalDashboard(self.bot, ctx_data, service_type)
                view.interaction = interaction
                logger.info(f"[{req_id}] ✅ Dashboard view constructed OK")
            except Exception as e:
                logger.error(f"[{req_id}] ❌ VIEW CONSTRUCTION FAILED: {e}", exc_info=True)
                raise
            
            try:
                # 🟢 NO "content": "" here!
                payload_data = {"flags": 32768, "components": view.build_v2_payload()}
                logger.info(f"[{req_id}] ✅ Payload built OK, sending PATCH...")
                
                route = discord.http.Route('PATCH', f'/webhooks/{interaction.application_id}/{interaction.token}/messages/@original')
                response = await self.bot.http.request(route, json=payload_data)
                logger.info(f"[{req_id}] ✅ Dashboard sent successfully")
                _dashboard_sent = True
                
                # ─── Store the message ID for background updates ──────────────────
                try:
                    if isinstance(response, dict) and response.get("id"):
                        view.message_id = int(response["id"])
                        view.channel_id = interaction.channel_id
                        logger.info(f"[{req_id}] 📌 Message ID stored: {view.message_id}")
                except Exception as e:
                    logger.warning(f"[{req_id}] Could not store message ID: {e}")
            except Exception as e:
                logger.error(f"[{req_id}] ❌ PATCH FAILED: {e}", exc_info=True)
                raise
            
            if service_type == "mecha":
                browser = getattr(self.bot.task_queue, "browser_service", None)
                if browser:
                    asyncio.create_task(browser.start())
                
        except Exception as e:
            if _dashboard_sent:
                logger.warning(f"[{req_id}] ⚠️ Post-send exception (dashboard already live, ignoring): {type(e).__name__}: {e}")
                return

            logger.error(f"[{req_id}] ❌ Failed to fetch metadata: {e}", exc_info=True)
            
            # ─── Don't sentinel-report rate limit errors — they're transient ────────
            is_rate_limit = isinstance(e, discord.HTTPException) and e.status == 429
            if not is_rate_limit:
                # 🟢 S-GRADE: Dispatch to Admin Sentinel (Only for real fatal errors)
                await self.bot.dispatch_error(e, interaction=interaction)
            # ────────────────────────────────────────────────────────────────────────
            
            err_type = type(e).__name__
            err_msg = str(e).splitlines()[0] if str(e) else "Unknown Error"
            
            # 🟢 ALWAYS show the real error, not just a generic message
            if err_type in ["ScraperError", "MechaException"]:
                display_content = f"### ❌ Extraction Failed\n> {err_msg}"
            elif is_rate_limit:
                display_content = "### ⏳ Rate Limited\n> Discord is busy. Please try again in a moment."
            else:
                display_content = (
                    f"### ❌ Unexpected Error\n"
                    f"> `{err_type}: {err_msg}`\n"
                    f"-# Come <@1216284053049704600>. New Error"
                )

            error_payload = {
                "flags": 32768,
                "components": [{
                    "type": 17,
                    "components": [
                        {
                            "type": 10,
                            "content": display_content
                        },
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 2, "style": 2, "label": "Back to Dashboard",
                                    "custom_id": "v2Dash_Home"
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
            "flags": 32832,
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
                                    "custom_id": f"v2_select_sub_channel_{platform}|{self._store_url(url)}",
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
            
            # 🟢 S-GRADE: Tokenize IDs to prevent 100-char overflow
            token = self._store_url(url)
            modal_id = f"v2_modal_sub_channel_select_{platform}|{token}"
            select_id = f"v2_select_sub_channel_{platform}|{token}"
            
            assert len(modal_id) <= 100, f"launch_channel_select modal_id overflow: {len(modal_id)} chars"
            assert len(select_id) <= 100, f"launch_channel_select select_id overflow: {len(select_id)} chars"

            modal_payload = {
                "type": 9,
                "data": {
                    "custom_id": modal_id, 
                    "title": "Select Target Channel",
                    "components": [
                        {
                            "type": 18, # LABEL
                            "label": "Choose the auto-download channel",
                            "component": {
                                "type": 8, # CHANNEL_SELECT
                                "custom_id": select_id,
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
            token_or_url = parts[1] if len(parts) > 1 else ""
            url = self._resolve_url(token_or_url)
            
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
                    "flags": 32832,
                    "components": [{"type": 17, "components": [{"type": 10, "content": "📡 **Setting up Subscription...**"}]}]
                }
            }
            try: await self.bot.http.request(discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'), json=analyzing_payload)
            except: pass

        try:
            from app.services.group_manager import add_subscription, is_series_subscribed_for_group, get_title_override, get_series_by_channel
            
            # 1. Fetch Metadata (Fast Mode)
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(url)
            if not scraper:
                raise MechaException(f"Unsupported platform URL: {url}", code="SY_002")
            
            data = await scraper.get_series_info(url, fast=True)
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data

            # 2. Determine Group
            guild_id = interaction.guild.id if interaction.guild else 0
            origin_channel_id = interaction.channel.id if interaction.channel else 0
            state = self.bot.app_state
            group_name = state.server_map.get(origin_channel_id) or state.server_map.get(guild_id)

            if not group_name:
                error_payload = {
                    "flags": 32832,
                    "components": [
                        {
                            "type": 17,
                            "components": [{"type": 10, "content": "❌ **No Group Profile Linked**\nPlease link this server to a group via `/register-server` first."}]
                        },
                        {
                            "type": 1,
                            "components": [{"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}]
                        }
                    ]
                }
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=error_payload)
                return self._queue_auto_delete(interaction, 1800)

            # 3. Check Per-Group Singularity rule
            is_subbed = await is_series_subscribed_for_group(series_id, group_name)

            if is_subbed:
                error_payload = {
                    "flags": 32832,
                    "components": [{
                        "type": 17,
                        "components": [{
                            "type": 10,
                            "content": f"⚠️ **Subscription Rejected**\n**{title}** is already being tracked in **{group_name}**."
                        }]
                    }]
                }
                # Add Back button
                error_payload["components"].append({
                    "type": 1,
                    "components": [{"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}]
                })
                await self.bot.http.request(discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original'), json=error_payload)
                return self._queue_auto_delete(interaction, 1800)

            # 4. Check Channel Occupancy (Exclusive: One Series Per Channel)
            # 🟢 EXCEPTION: Server 1419393318147719170 allows unlimited series per channel
            occupied_by = get_series_by_channel(target_channel_id)
            is_universal_server = (interaction.guild_id == 1419393318147719170)
            
            if occupied_by and not is_universal_server:
                existing_group, existing_sub = occupied_by
                if existing_sub.get("series_id") != series_id:
                    # 🟢 S-GRADE: Check for Title Override for the OCCUPYING series
                    occ_title = get_title_override(existing_group, existing_sub.get("series_url")) or existing_sub.get("series_title", "Unknown Series")
                    
                    error_payload = {
                        "flags": 32832,
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
            await EventBus.emit("subscription_added", group_name, sub)

            # 🟢 S-GRADE: Auto-register alert on Mecha side
            if platform == "mecha":
                # scraper._last_soup was stored during get_series_info earlier in this method
                asyncio.create_task(scraper.toggle_alert(series_id, enable=True, soup=getattr(scraper, "_last_soup", None)))
                
                # 🟢 S-GRADE: Backfill release_day from alerts if not found in metadata
                if not release_day:
                    async def _backfill_release_day():
                        try:
                            alerts = await scraper.get_alerts_list()
                            match = next((a for a in alerts if a["series_id"] == series_id), None)
                            if match and match.get("release_day"):
                                from app.services.group_manager import update_release_day_by_id
                                update_release_day_by_id(group_name, series_id, match["release_day"])
                                logger.info(f"[Sub] Backfilled release_day={match['release_day']} for {title}")
                        except Exception as e:
                            logger.warning(f"[Sub] release_day backfill failed: {e}")
                    asyncio.create_task(_backfill_release_day())

            # 5. Success UI
            success_payload = {
                "flags": 32832,
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
            error_p = {
                "flags": 32832, 
                "components": [
                    {"type": 17, "components": [{"type": 10, "content": f"Come <@1216284053049704600>. New Error"}]},
                    {"type": 1, "components": [{"type": 2, "style": 2, "label": "Back to Dashboard", "custom_id": "v2Dash_Home"}]}
                ]
            }
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

    @commands.command(name="ui_sub")
    async def ui_sub(self, ctx: commands.Context):
        """Prefix command to show today's subscription highlights."""
        logger.info(f"Prefix Command $ui_sub triggered by {ctx.author} in {ctx.channel}")
        guild_id = ctx.guild.id if ctx.guild else 0
        channel_id = ctx.channel.id
        # Dynamic state lookup
        state = self.bot.app_state
        scan_name = state.server_map.get(channel_id) or state.server_map.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        # 1. Fetch Weeklies
        weeklies_section, action_row = await self.get_weeklies_section(scan_name)

        # 2. Header
        custom_emoji = get_group_emoji(scan_name)
        header_logo = f"{custom_emoji} " if custom_emoji else ""
        header_section = {
            "type": 10, "content": f"# {header_logo}{scan_name}'s Subscriptions"
        }

        # 3. Footer
        footer_section = {
            "type": 10, "content": f"-# CS-ID: {guild_id if guild_id else '0'}"
        }

        # 🟢 ASSEMBLE V2 PAYLOAD
        payload = {
            "flags": 32768,
            "components": [
                {
                    "type": 17,
                    "components": [
                        header_section,
                        {"type": 14, "divider": True, "spacing": 1},
                        weeklies_section,
                        action_row,
                        {"type": 14, "divider": True, "spacing": 1},
                        footer_section
                    ]
                }
            ]
        }

        # 🟢 Dispatch via Raw HTTP
        try:
            route = discord.http.Route('POST', f'/channels/{ctx.channel.id}/messages')
            await self.bot.http.request(route, json=payload)
        except Exception as e:
            logger.error(f"Failed to send $ui_sub")
            await self.bot.dispatch_error(e, ctx=ctx)
            await ctx.send(f"Come <@1216284053049704600>. New Error")

    @commands.command(name="ui_sub_test")
    async def ui_sub_test(self, ctx: commands.Context):
        """Debug command to show a mock UI with 30 dummy subscriptions."""
        logger.info(f"Debug Command $ui_sub_test triggered by {ctx.author}")
        
        # 1. Generate 30 dummy subscriptions
        dummy_subs = []
        platforms = [
            ("Piccoma", "<:Piccoma:1478368704164134912>", "https://piccoma.com/test"),
            ("Mecha Comic", "<:Mechacomic:1478369141957333083>", "https://mechacomic.jp/test"),
            ("Jumptoon", "<:Jumptoon:1478367963928068168>", "https://jumptoon.com/test")
        ]
        
        for i in range(1, 31):
            plat_name, plat_emoji, plat_url = platforms[i % 3]
            dummy_subs.append({
                "title": f"Test Series #{i:02d} ({plat_name})",
                "url": plat_url,
                "channel_id": ctx.channel.id,
                "emoji": plat_emoji,
                "release_day": random.choice(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
            })

        # 2. Build Weeklies Text (Top 3 for 'Today')
        weeklies_text = "## Today Weeklies (Mock)\n"
        for i, sub in enumerate(dummy_subs[:3], 1):
            # 🟢 NEW FORMAT: > i. <Emoji> Title: <#channel_id>
            weeklies_text += f"> {i}. {sub['emoji']} {sub['title']}: <#{sub['channel_id']}>\n"
        
        # 3. Build "All Subscriptions" list for the UI
        list_text = "## 📋 All Subscriptions (Mock)\n"
        for i, sub in enumerate(dummy_subs[:10], 1):
             list_text += f"{i}. {sub['emoji']} **{sub['title']}**\n"
        list_text += f"\n> *... and {len(dummy_subs) - 10} more series.*"

        # 🟢 ASSEMBLE V2 PAYLOAD
        payload = {
            "flags": 32768,
            "components": [
                {
                    "type": 17, # CONTAINER
                    "components": [
                        {"type": 10, "content": "### 🛠️ DEBUG MODE: UI Mockup\n# 🏮 Verzue Bot Debug Dashboard"},
                        {"type": 14, "divider": True, "spacing": 1},
                        {"type": 10, "content": weeklies_text},
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 2, "style": 2, "label": "View All 30 Subscriptions",
                                    "custom_id": "v2_btn_mock_view_all",
                                    "emoji": {"name": "📋"}
                                }
                            ]
                        },
                        {"type": 14, "divider": True, "spacing": 1},
                        {"type": 10, "content": list_text},
                        {
                            "type": 1, 
                            "components": [
                                {
                                    "type": 2, "style": 3, "label": "Refresh Data",
                                    "custom_id": "v2_btn_mock_refresh",
                                    "emoji": {"name": "🔄"}
                                },
                                {
                                    "type": 2, "style": 4, "label": "Close",
                                    "custom_id": "btn_close_main_dash",
                                    "emoji": {"name": "✖️"}
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        # 🟢 Dispatch via Raw HTTP
        try:
            route = discord.http.Route('POST', f'/channels/{ctx.channel.id}/messages')
            await self.bot.http.request(route, json=payload)
        except Exception as e:
            logger.error(f"Failed to send $ui_sub_test")
            await self.bot.dispatch_error(e, ctx=ctx)
            await ctx.send(f"Come <@1216284053049704600>. New Error")

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))