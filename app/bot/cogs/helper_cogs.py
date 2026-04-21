import discord
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from discord import app_commands
from discord.ext import commands
from config.settings import Settings
from app.services.group_manager import load_group, add_subscription, rename_group_profile, update_last_chapter, get_all_subscriptions, set_drive_folder_cache
from app.services.redis_manager import RedisManager
from app.services.session_service import SessionService
from app.core.events import EventBus
from app.services.gdrive.sync_service import sync_group_folder_name
from app.services.login_service import LoginService

import io
import json
import os
import urllib.parse
logger = logging.getLogger("HelperCogs")

try:
    import docx
except ImportError:
    import sys
    logger.error(f"❌ docx not found! sys.path: {sys.path}")
    docx = None

class HelperSlashCog(commands.Cog):
    """Slash commands mapped directly to the original Verzue Bot logic."""
    def __init__(self, bot):
        self.bot = bot # This can be MechaBot or HelperBot
        self.main_bot = getattr(bot, 'main_bot', bot)
        self.login_service = LoginService()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Global check for access control with automatic deferral."""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
            
        is_owner = interaction.user.id == 1216284053049704600
        is_allowed = interaction.user.id in self.bot.app_state.cdn_allowed_users
        if not (is_owner or is_allowed):
            msg = "❌ **Access Denied.** You are not authorized to use admin commands."
            # Since we always defer now, we always use followup
            await interaction.followup.send(msg, ephemeral=True)
            return False
        return True

    def _get_current_group(self, interaction: discord.Interaction) -> str:
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id_origin = interaction.channel.id
        # Use dynamic app_state instead of removed static Settings.SERVER_MAP
        state = self.bot.app_state
        return state.server_map.get(channel_id_origin) or state.server_map.get(guild_id)

    # --- 1. GROUP ADD ---
    @app_commands.command(name="add-group", description="[Admin] Register a new group profile.")
    @app_commands.describe(
        name="The name of the new group (e.g., Thunder Scans)", 
        website="The group's website link",
        emoji="The discord emoji to use for this group's dashboard (Optional)",
        servers="Optional: Comma-separated Server IDs to link (e.g. 123, 456)"
    )
    async def add_group(self, interaction: discord.Interaction, name: str, website: str, emoji: str | None = None, servers: str | None = None):
        if not await self.interaction_check(interaction):
            return

        if not name.strip():
            return await interaction.followup.send("❌ Cannot create an empty group name.", ephemeral=True)
            
        clean_name = name.strip()
        clean_website = website.strip()
        
        if clean_name in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"⚠️ Group **{clean_name}** already exists in the registry.", ephemeral=True)
            
        self.bot.app_state.group_profiles.add(clean_name)
        self.bot.app_state.save_group_registry()
        
        # Create profile JSON with website
        try:
            from app.services.group_manager import _group_filename
            import json
            filepath = Settings.GROUPS_DIR / _group_filename(clean_name)
            if not filepath.exists():
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump({
                        "subscriptions": [], 
                        "website": clean_website, 
                        "emoji": emoji.strip() if emoji else ""
                    }, f, indent=4)
                logger.info(f"[GroupManager] Created profile via Helper UI: {_group_filename(clean_name)}")
        except Exception as e:
            logger.error(f"Failed to create group profile JSON: {e}")
            
        # Optional: Register servers immediately
        linked_str = ""
        if servers:
            ids = [s.strip() for s in servers.split(",") if s.strip()]
            success_ids = []
            for sid in ids:
                try:
                    server_id = int(sid)
                    self.bot.app_state.server_map[server_id] = clean_name
                    success_ids.append(str(server_id))
                except ValueError:
                    continue
            
            if success_ids:
                self.bot.app_state.save_group_registry()
                linked_str = f"\n🔗 **Linked Servers:** {', '.join(f'`{s}`' for s in success_ids)}"

        await interaction.followup.send(f"✅ **Group Profile Created:** `{clean_name}`\n🌐 **Website:** <{clean_website}>{linked_str}\nYou can now use `/register-server` to assign more servers.")

    # --- AUTOCOMPLETE HELPER ---
    async def group_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Provides autocomplete suggestions from registered group profiles."""
        return [
            app_commands.Choice(name=g, value=g)
            for g in sorted(self.bot.app_state.group_profiles)
            if current.lower() in g.lower()
        ][:25]

    # --- 1.5 GROUP EDIT ---
    @app_commands.command(name="edit-group", description="[Admin] Update a group's website or add a note.")
    @app_commands.describe(
        name="The name of the group to edit", 
        website="New website link (optional)", 
        note="A note for the group (optional)",
        emoji="New custom emoji for the dashboard (optional)",
        new_name="Optional: A new name for the group (Requires Owner Confirmation)"
    )
    @app_commands.autocomplete(name=group_name_autocomplete)
    async def edit_group(self, interaction: discord.Interaction, name: str, website: str | None = None, note: str | None = None, emoji: str | None = None, new_name: str | None = None):
        if not await self.interaction_check(interaction):
            return

        if name not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{name}` is not a registered group profile.", ephemeral=True)

        if not website and not note and not emoji and not new_name:
            return await interaction.followup.send("⚠️ No changes provided. Please specify a new website, note, emoji, or new name.", ephemeral=True)

        try:
            from app.services.group_manager import save_group
            data = load_group(name)
            
            changes = []
            if website:
                data["website"] = website.strip()
                changes.append(f"🌐 Website updated to: <{data['website']}>")
            if note:
                data["note"] = note.strip()
                changes.append(f"📝 Note updated/added.")
            if emoji:
                data["emoji"] = emoji.strip()
                changes.append(f"✨ Custom emoji updated to: {data['emoji']}")

            save_group(name, data)
            
            msg = f"✅ **Group '{name}' Updated:**\n" + "\n".join(changes)
            
            if new_name and new_name.strip() and new_name.strip() != name:
                clean_new = new_name.strip()
                if clean_new in self.bot.app_state.group_profiles:
                    msg += f"\n⚠️ **Rename Failed:** `{clean_new}` already exists."
                else:
                    await self.initiate_group_rename(interaction, name, clean_new)
                    msg += f"\n📩 **Rename Request Sent:** rename `{name}` → `{clean_new}` (Awaiting owner approval)"

            await interaction.followup.send(msg)
            
            logger.info(f"[GroupManager] Updated profile via Helper UI: {name}")
        except Exception:
            logger.error(f"Failed to edit group profile")
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)

    async def initiate_group_rename(self, interaction: discord.Interaction, old_name: str, new_name: str):
        """Logic to send DM to owner for rename confirmation."""
        try:
            owner_id = 1216284053049704600
            owner = await self.bot.fetch_user(owner_id)
            if not owner:
                return logger.warning("Could not find owner to send rename confirmation DM.")

            view = GroupRenameConfirmationView(old_name=old_name, new_name=new_name, requester=interaction.user)
            embed = discord.Embed(
                title="⚠️ Group Rename Request",
                description=f"Admin **{interaction.user}** has requested to rename group **{old_name}** to **{new_name}**.\n\nDo you authorize this action?",
                color=0xf1c40f
            )
            await owner.send(embed=embed, view=view)
            logger.info(f"[HelperCogs] Rename request sent to owner: {old_name} -> {new_name}")
        except Exception as e:
            logger.error(f"Failed to initiate group rename: {e}")

    # --- 2. REGISTER SERVER ---
    @app_commands.command(name="register-server", description="[Admin] Link a server or specific channel to a Group Profile")
    @app_commands.describe(
        name="Select the group profile",
        server="The Server ID(s) to link (Comma-separated allowed)",
        channel="Optional: Specific Channel ID to link (Only 1419393318147719170 allowed)"
    )
    @app_commands.autocomplete(name=group_name_autocomplete)
    async def register_server(
        self, 
        interaction: discord.Interaction, 
        name: str, 
        server: str, 
        channel: str | None = None
    ):
        if not await self.interaction_check(interaction):
            return

        if name not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{name}` is not a registered group profile.", ephemeral=True)
            
        ids = [s.strip() for s in server.split(",") if s.strip()]
        success_ids = []
        target_display = ""

        if channel:
            # Channel case: restricted to one server, usually one ID
            if len(ids) > 1:
                return await interaction.followup.send("❌ **Error.** Channel-level mapping only supports one Server ID at a time.", ephemeral=True)
            
            try:
                target_server_id = int(ids[0])
                target_channel_id = int(channel.strip())
            except ValueError:
                return await interaction.followup.send("❌ **Invalid ID.** Please provide numeric IDs.", ephemeral=True)

            if target_server_id != 1419393318147719170:
                return await interaction.followup.send(
                    "❌ **Restriction.** Channel-level mapping is only permitted for Server `1419393318147719170`.", 
                    ephemeral=True
                )
            self.bot.app_state.server_map[target_channel_id] = name
            target_display = f"Channel `{target_channel_id}`"
            success_ids.append(str(target_channel_id))
        else:
            # Server case: multiple allowed
            for sid in ids:
                try:
                    target_server_id = int(sid)
                    self.bot.app_state.server_map[target_server_id] = name
                    success_ids.append(sid)
                except ValueError:
                    continue
            
            if not success_ids:
                return await interaction.followup.send("❌ **Invalid Server ID(s).** Please provide numeric IDs.", ephemeral=True)
            
            target_display = f"Server(s) {', '.join(f'`{s}`' for s in success_ids)}"
            
        self.bot.app_state.save_group_registry()
        
        embed = discord.Embed(
            title="✅ Registration Complete",
            description=f"{target_display} now linked to **{name}**.\nThe `/dashboard` will now identify as *Dashboard of {name}* in that scope.",
            color=0x2ecc21
        )
        await interaction.followup.send(embed=embed)
        logger.info(f"[HelperCogs] Registered {target_display} -> {name} via /register-server")

    # --- 2.5 GROUP LIST ---
    @app_commands.command(name="group-list", description="[Admin] List all registered group profiles and their linked IDs.")
    async def group_list(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return

        if not self.bot.app_state.group_profiles:
            return await interaction.followup.send("ℹ️ No Group Profiles have been registered yet.", ephemeral=True)

        embed = discord.Embed(
            title="📋 Registered Group Profiles",
            description="List of all groups and their active server/channel mappings.",
            color=0x3498db
        )

        for group_name in sorted(list(self.bot.app_state.group_profiles)):
            data = load_group(group_name)
            website = data.get("website", "*Not Set*")
            note = data.get("note")
            
            # Find linked IDs for this group
            links = [str(tid) for tid, name in self.bot.app_state.server_map.items() if name == group_name]
            links_str = ", ".join(f"`{l}`" for l in links) if links else "*None*"

            val = f"🌐 **Website:** {website}\n"
            val += f"🔗 **Mappings:** {links_str}"
            if note:
                val += f"\n📝 **Note:** {note}"
                
            embed.add_field(
                name=f"👥 {group_name}",
                value=val,
                inline=False
            )

        await interaction.followup.send(embed=embed)
    # --- 5. SUB LIST ---
    @app_commands.command(name="sub-list", description="[Admin] List all active subscriptions for a specific group")
    @app_commands.describe(group_name="The name of the group profile to list subscriptions for")
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def sub_list(self, interaction: discord.Interaction, group_name: str):
        if not await self.interaction_check(interaction):
            return

        if group_name not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{group_name}` is not a registered group profile.", ephemeral=True)

        data = load_group(group_name)
        subs = data.get("subscriptions", [])

        if not subs:
            return await interaction.followup.send(f"ℹ️ No active subscriptions for **{group_name}**.")

        desc = f"Active Subscriptions for **{group_name}**:\n\n"
        for sub in subs:
            day_str = sub.get('release_day') or '*Not Set*'
            desc += f"📚 **{sub['series_title']}** ({sub['platform']})\n"
            desc += f"├─ 🔗 **URL:** <{sub['series_url']}>\n"
            desc += f"├─ 📅 **Release Day:** {day_str}\n"
            desc += f"├─ 🔔 **Channel:** <#{sub['channel_id']}>\n"
            desc += f"└─ 🔖 **Tracking from:** Ch. {sub.get('last_known_chapter_id', 'Unknown')}\n\n"

        embed = discord.Embed(
            title="📡 Release Notification Subscriptions",
            description=desc,
            color=0x3498db
        )
        await interaction.followup.send(embed=embed)



    # --- 6. REMOVE SUBSCRIPTION ---
    @app_commands.command(name="remove-subscription", description="[Admin] Remove a series subscription from a group profile.")
    @app_commands.describe(
        series="The URL of the series to remove",
        group="The name of the group profile"
    )
    @app_commands.autocomplete(group=group_name_autocomplete)
    async def remove_subscription_cmd(self, interaction: discord.Interaction, series: str, group: str):
        if not await self.interaction_check(interaction):
            return

        if group not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{group}` is not a registered group profile.", ephemeral=True)

        clean_url = series.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]

        from app.services.group_manager import remove_subscription
        success = remove_subscription(group, clean_url)

        if success:
            await interaction.followup.send(f"✅ **Subscription Removed:** Series at <{clean_url}> has been removed from **{group}**.")
            logger.info(f"[HelperCogs] Removed subscription for {group}: {clean_url}")
        else:
            await interaction.followup.send(f"⚠️ **Subscription Not Found:** Series at <{clean_url}> was not found in the subscription list for **{group}**.", ephemeral=True)


    # --- 8. ADMIN MANAGEMENT ---

    @app_commands.command(name="add-admin", description="[Admin] Grant a user access to admin commands.")
    @app_commands.describe(user="The user to authorize")
    async def add_admin(self, interaction: discord.Interaction, user: discord.User):
        if not await self.interaction_check(interaction):
            return

        self.bot.app_state.cdn_allowed_users.add(user.id)
        self.bot.app_state.save_cdn_users()
        
        await interaction.followup.send(f"✅ **Access Granted:** <@{user.id}> can now use helper bot admin commands.", ephemeral=False)

    @app_commands.command(name="remove-admin", description="[Admin] Revoke a user's access to admin commands.")
    @app_commands.describe(user="The user to de-authorize")
    async def remove_admin(self, interaction: discord.Interaction, user: discord.User):
        if not await self.interaction_check(interaction):
            return

        if user.id in self.bot.app_state.cdn_allowed_users:
            self.bot.app_state.cdn_allowed_users.remove(user.id)
            self.bot.app_state.save_cdn_users()
            await interaction.followup.send(f"🗑️ **Access Revoked:** <@{user.id}> can no longer use admin commands.", ephemeral=False)
        else:
            await interaction.followup.send(f"⚠️ User <@{user.id}> was not in the admin list.", ephemeral=True)

    @app_commands.command(name="admin-list", description="[Admin] List all users with admin command access.")
    async def admin_list(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return

        if not self.bot.app_state.cdn_allowed_users:
            return await interaction.followup.send("ℹ️ No users are currently in the admin list.", ephemeral=True)

        desc = "## Authorized Admin Users\n"
        for user_id in self.bot.app_state.cdn_allowed_users:
            desc += f"> • <@{user_id}> (`{user_id}`)\n"

        embed = discord.Embed(
            title="🔐 Admin User Access",
            description=desc,
            color=0x3498db
        )
        await interaction.followup.send(embed=embed)

    # --- 7. CHANGE TITLE ---

    @app_commands.command(name="change-title", description="[Admin] Set a custom English title for a series within a group")
    @app_commands.describe(
        group_name="Select the group profile",
        series_link="Series URL (e.g. https://piccoma.com/web/product/...)",
        english_name="The custom English title to display"
    )
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def change_title(self, interaction: discord.Interaction, group_name: str, series_link: str, english_name: str):
        if not await self.interaction_check(interaction):
            return

        # Validate group exists
        if group_name not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{group_name}` is not a registered group profile.", ephemeral=True)

        # Validate URL against supported platforms
        supported_domains = ["mechacomic.jp", "jumptoon.com", "piccoma.com", "kakao.com", "kuaikanmanhua.com", "ac.qq.com"]
        clean_url = series_link.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]

        if not any(d in clean_url.lower() for d in supported_domains):
            return await interaction.followup.send(
                f"❌ **Unsupported Platform.**\nThe URL must be from one of: {', '.join(f'`{d}`' for d in supported_domains)}",
                ephemeral=True
            )

        clean_name = english_name.strip()
        if not clean_name:
            return await interaction.followup.send("❌ English name cannot be empty.", ephemeral=True)

        from app.services.group_manager import set_title_override, load_group, _clean_url
        set_title_override(group_name, clean_url, clean_name)
        
        # 🟢 S-GRADE: Retrieve original scraped title for background sync
        group_data = load_group(group_name)
        subs = group_data.get("subscriptions", [])
        clean_target = _clean_url(clean_url)
        original_title = next((s.get("series_title") for s in subs if _clean_url(s.get("series_url", "")) == clean_target), None)

        # 🟢 Sync Rename on Google Drive (Background)
        asyncio.create_task(sync_group_folder_name(
            self.bot, group_name, clean_url, 
            override_title=clean_name,
            original_title=original_title
        ))

        embed = discord.Embed(
            title="✅ Title Override Saved",
            description=(
                f"**Group:** `{group_name}`\n"
                f"**Series URL:** <{clean_url}>\n"
                f"**Custom Title:** `{clean_name}`\n\n"
                f"This title will now appear in the dashboard for channels linked to **{group_name}**.\n"
                f"🔄 *The folder on Google Drive will be renamed in the background.*"
            ),
            color=0x2ecc71
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="sync-drive-titles", description="[Admin] Sync all title overrides to Drive folder names")
    @app_commands.describe(
        group_name="Group to sync, or leave blank for all groups",
        fix_series_folders="If True, restores series folder names to original scraped title"
    )
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def sync_drive_titles(self, interaction: discord.Interaction, group_name: str | None = None, fix_series_folders: bool = False):
        """Backfill command to sync existing title overrides to GDrive folder names."""
        if not await self.interaction_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        from app.services.group_manager import load_group
        from app.services.gdrive.sync_service import sync_group_folder_name

        targets = [] # (group_name, url, override_title, original_title)
        
        groups_to_scan = []
        if group_name:
            if group_name not in self.bot.app_state.group_profiles:
                return await interaction.followup.send(f"❌ **Unknown Group:** `{group_name}`", ephemeral=True)
            groups_to_scan = [group_name]
        else:
            # All groups
            if not Settings.GROUPS_DIR.exists():
                return await interaction.followup.send("❌ No groups directory found.", ephemeral=True)
            
            for path in Settings.GROUPS_DIR.glob("*.json"):
                if path.name == "registry.json": continue
                groups_to_scan.append(path.stem.replace('_', ' '))

        for gname in groups_to_scan:
            data = load_group(gname)
            overrides = data.get("title_overrides", {})
            subs = data.get("subscriptions", [])
            
            # Map url -> original title
            from app.services.group_manager import _clean_url
            original_map = {
                _clean_url(s.get("series_url", "")): s.get("series_title")
                for s in subs if s.get("series_url")
            }
            
            for url, override in overrides.items():
                original = original_map.get(_clean_url(url))
                targets.append((gname, url, override, original))

        if not targets:
            return await interaction.followup.send("ℹ️ No title overrides found to sync.", ephemeral=True)

        await interaction.followup.send(f"🔄 **Processing {len(targets)} series** across {len(groups_to_scan)} group(s)...", ephemeral=True)

        success, failed = 0, 0
        for gname, url, override, original in targets:
            try:
                await sync_group_folder_name(
                    self.bot, gname, url, 
                    override_title=override,
                    original_title=original,
                    fix_series_folder=fix_series_folders
                )
                success += 1
            except Exception as e:
                logger.error(f"Sync failed for {gname} / {url}: {e}")
                failed += 1

        await interaction.followup.send(
            f"✅ **Sync complete:** {success} synced, {failed} failed.",
            ephemeral=True
        )
        
    @app_commands.command(name="reset-drive-cache", description="[Admin] Force fresh Drive folder resolution for a series")
    @app_commands.describe(
        group_name="The name of the group profile",
        series_link="The series URL to reset cache for"
    )
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def reset_drive_cache(self, interaction: discord.Interaction, group_name: str, series_link: str):
        if not await self.interaction_check(interaction):
            return

        if group_name not in self.bot.app_state.group_profiles:
            return await interaction.followup.send(f"❌ **Unknown Group:** `{group_name}` is not a registered group profile.", ephemeral=True)

        set_drive_folder_cache(group_name, series_link.strip(), {})
        
        await interaction.followup.send(
            f"✅ **Drive cache cleared** for `{series_link}` in `{group_name}`.\nNext download will perform a fresh re-resolution from Google Drive.",
            ephemeral=True
        )

    @app_commands.command(name="remove-group", description="[Admin] Completely remove a group profile (Requires Owner Confirmation)")
    @app_commands.describe(group_name="Select the group profile to delete")
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def remove_group(self, interaction: discord.Interaction, group_name: str):
        """Secure Group Removal: Sends a DM to the owner for confirmation."""
        if not await self.interaction_check(interaction):
            return

        # Use the common initiation logic
        await self.initiate_group_removal(interaction, group_name)

    async def initiate_group_removal(self, interaction: discord.Interaction, group_name: str):
        """Logic to send DM to owner for confirmation."""
        from app.services.group_manager import _group_filename
        if group_name not in self.bot.app_state.group_profiles or not _group_filename(group_name).exists():
            msg = f"❌ **Unknown Group:** `{group_name}` does not exist."
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            owner_id = 1216284053049704600
            owner = await self.bot.fetch_user(owner_id)
            if not owner:
                return await interaction.followup.send("❌ Could not find owner to send confirmation DM.", ephemeral=True)

            view = GroupRemovalConfirmationView(group_name=group_name, requester=interaction.user)
            embed = discord.Embed(
                title="⚠️ Group Removal Request",
                description=f"Admin **{interaction.user}** has requested to delete the group profile: **{group_name}**.\n\nDo you authorize this action?",
                color=0xf1c40f
            )
            await owner.send(embed=embed, view=view)
            
            # Followup or message depending on interaction state
            msg = f"📩 Confirmation request for **{group_name}** sent to **{owner.name}**."
            await interaction.followup.send(msg, ephemeral=True)
            
        except discord.Forbidden:
            msg = "❌ I cannot send DMs to the owner. Please ensure they have DMs enabled."
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to initiate group removal: {e}")
            msg = f"❌ Failed to initiate removal: {e}"
            await interaction.followup.send(msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch leftover V2 interactions if any."""
        # We no longer need special handling for v2_modal_remove_group or v2_select_remove_group
        # but we keep the structure for other future V2 helper components if needed.
        pass

    # --- 9. MANUAL SUBSCRIPTION ADD ---

    def _convert_to_utc(self, day: str, time_str: str, tz_name: str) -> tuple[str, str]:
        """
        Normalizes a (day, time, timezone) to (UTC Day, UTC Time).
        tz_name: 'JST' (+9), 'IST' (+5.5), 'UTC' (+0)
        """
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        day_idx = days.index(day.capitalize())
        
        try:
            h, m = map(int, time_str.split(':'))
        except:
            h, m = 0, 0
            
        # Create a dummy timestamp (Jan 1, 2024 is a Monday)
        base = datetime(2024, 1, 1, h, m) + timedelta(days=day_idx)
        
        # Subtract offset
        offset_map = {"JST": 9, "IST": 5.5, "UTC": 0}
        offset = offset_map.get(tz_name.upper(), 0)
        
        utc_dt = base - timedelta(hours=offset)
        
        utc_day = days[utc_dt.weekday()]
        utc_time = utc_dt.strftime("%H:%M")
        return utc_day, utc_time

    @app_commands.command(name="sub-add", description="[Admin] Manually add a subscription with specific schedule.")
    @app_commands.describe(
        url="The series URL",
        day="Release day in local timezone",
        timezone="Timezone for the day/time",
        time="Release time in local timezone (optional, defaults to 00:00)"
    )
    @app_commands.choices(day=[
        app_commands.Choice(name=d, value=d) for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ])
    @app_commands.choices(timezone=[
        app_commands.Choice(name="JST (UTC+9)", value="JST"),
        app_commands.Choice(name="IST (UTC+5.5)", value="IST"),
        app_commands.Choice(name="UTC", value="UTC"),
    ])
    async def sub_add(
        self, 
        interaction: discord.Interaction, 
        url: str, 
        day: str, 
        timezone: str, 
        time: str = "00:00"
    ):
        await interaction.response.defer()
        if not await self.interaction_check(interaction):
            return
        
        try:
            # 1. Extract Metadata (via main bot)
            scraper = self.main_bot.task_queue.provider_manager.get_provider_for_url(url)
            if not scraper:
                return await interaction.followup.send("❌ Error: No provider found for this URL.")
                
            data = await scraper.get_series_info(url, fast=True)
            title, total_chapters, chapter_list, image_url, series_id, _, _, _, _ = data
            
            # 2. Timezone Conversion
            utc_day, utc_time = self._convert_to_utc(day, time, timezone)
            
            # 3. Identification
            group_name = self._get_current_group(interaction)
            if not group_name:
                return await interaction.followup.send("❌ Channel/Server not registered to any group.")
                
            # 4. Build Sub Dict
            sub = {
                "series_id": series_id,
                "series_url": url,
                "platform": scraper.IDENTIFIER,
                "channel_id": interaction.channel.id,
                "last_known_chapter_id": str(chapter_list[-1]['id']) if chapter_list else "0",
                "release_day": utc_day,
                "release_time": utc_time,
                "added_at": datetime.now(timezone.utc).isoformat(),
                "added_by": interaction.user.id
            }
            
            # 5. Check if update vs new
            is_update = any(s["series_id"] == series_id for s in load_group(group_name)["subscriptions"])
            
            add_subscription(group_name, sub)
            await EventBus.emit("subscription_added", group_name, sub)
            
            action_str = "Updated" if is_update else "Added"
            embed = discord.Embed(
                title=f"✅ Manual Subscription {action_str}",
                description=f"**{title}** (`{series_id}`)\nGroup: **{group_name}**",
                color=0x2ecc71 if not is_update else 0x3498db
            )
            embed.add_field(name="Input Schedule", value=f"`{day} @ {time} {timezone}`", inline=True)
            embed.add_field(name="UTC Result", value=f"`{utc_day} @ {utc_time}`", inline=True)
            if image_url: embed.set_thumbnail(url=image_url)
            
            await interaction.followup.send(embed=embed)
                
        except Exception:
            logger.error(f"Manual Sub Add failed")
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error")

    # --- 10. SESSION MANAGEMENT ---

    @app_commands.command(name="reset-sessions", description="[Admin] Reset all sessions for a platform to HEALTHY status.")
    @app_commands.describe(platform="The platform to reset (e.g., mecha, jumptoon, piccoma)")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Mecha Comic", value="mecha"),
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Kakao", value="kakao"),
        app_commands.Choice(name="Kuaikan", value="kuaikan"),
        app_commands.Choice(name="ACQQ", value="acqq"),
    ])
    async def reset_sessions(self, interaction: discord.Interaction, platform: str):
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_check(interaction):
            return
        
        try:
            aids = await RedisManager().list_sessions(platform)
            if not aids:
                return await interaction.followup.send(f"⚠️ No sessions found for platform: `{platform}`", ephemeral=True)
            
            count = 0
            for aid in aids:
                session = await RedisManager().get_session(platform, aid)
                if session:
                    session["status"] = "HEALTHY"
                    session.pop("error_reason", None)
                    session.pop("last_refresh_attempt", None) # Clear retry throttle
                    await RedisManager().set_session(platform, aid, session)
                    count += 1
            
            await interaction.followup.send(f"✅ Successfully reset **{count}** sessions for **{platform}** to `HEALTHY`.", ephemeral=True)
            logger.info(f"[HelperCogs] Manual session reset for {platform} by {interaction.user}")
            
        except Exception:
            logger.error(f"Manual session reset failed")
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)
            
    @app_commands.command(name="list-sessions", description="[Admin] List all sessions and their current status for a platform.")
    @app_commands.describe(platform="The platform to list (e.g., mecha, jumptoon, piccoma)")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Mecha Comic", value="mecha"),
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Kakao", value="kakao"),
        app_commands.Choice(name="Kuaikan", value="kuaikan"),
        app_commands.Choice(name="ACQQ", value="acqq"),
    ])
    async def list_sessions(self, interaction: discord.Interaction, platform: str):
        await interaction.response.defer(ephemeral=True)
        if not await self.interaction_check(interaction): return
        
        try:
            aids = await RedisManager().list_sessions(platform)
            if not aids:
                return await interaction.followup.send(f"ℹ️ No sessions found for **{platform}**.", ephemeral=True)
            
            embed = discord.Embed(title=f"📋 Sessions: {platform.capitalize()}", color=0x3498db)
            for aid in sorted(aids):
                session = await RedisManager().get_session(platform, aid)
                if not session: continue
                
                status = session.get("status", "UNKNOWN")
                cookies_count = len(session.get("cookies", []))
                reason = session.get("error_reason", "None")
                
                status_emoji = "🟢" if status == "HEALTHY" else "🔴"
                val = f"**Status:** {status_emoji} `{status}`\n**Cookies:** `{cookies_count}`\n**Reason:** *{reason}*"
                embed.add_field(name=f"👤 ID: {aid}", value=val, inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception:
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)

    @app_commands.command(name="delete-session", description="[Admin] Permanently delete a session from Redis.")
    @app_commands.describe(platform="The platform", account_id="The specific Account ID to delete (e.g., primary, cookies)")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Mecha Comic", value="mecha"),
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Kakao", value="kakao"),
        app_commands.Choice(name="Kuaikan", value="kuaikan"),
        app_commands.Choice(name="ACQQ", value="acqq"),
    ])
    async def delete_session(self, interaction: discord.Interaction, platform: str, account_id: str):
        if not await self.interaction_check(interaction): return
        
        try:
            session = await RedisManager().get_session(platform, account_id)
            if not session:
                return await interaction.followup.send(f"⚠️ Session `{platform}:{account_id}` not found.", ephemeral=True)
            
            await RedisManager().delete_session(platform, account_id)
            await interaction.followup.send(f"✅ Successfully deleted session: `{platform}:{account_id}`", ephemeral=True)
            logger.info(f"[HelperCogs] Deleted session {platform}:{account_id} by {interaction.user}")
        except Exception:
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)

    # --- 11. COOKIE MANAGEMENT ---

    def _parse_cookie_content(self, content: str) -> list:
        """Parses string content into a list of cookies (EditThisCookie JSON or Header-style)."""
        clean_content = content.strip()
        if clean_content.startswith("```"):
            # Remove code block markers
            lines = clean_content.split("\n")
            if lines[0].startswith("```"): lines = lines[1:]
            if lines and lines[-1].strip() == "```": lines = lines[:-1]
            clean_content = "\n".join(lines).strip()

        # 1. Try JSON (EditThisCookie)
        try:
            data = json.loads(clean_content)
            if isinstance(data, list):
                if all('name' in c and 'value' in c for c in data[:2]):
                    return data
        except:
            pass

        # 2. Try Header-style (name=value; name2=value2)
        try:
            cookies = []
            parts = clean_content.split(";")
            for p in parts:
                if "=" in p:
                    kv = p.split("=", 1)
                    name = kv[0].strip()
                    value = kv[1].strip()
                    if name and value:
                        cookies.append({"name": name, "value": value})
            if cookies:
                return cookies
        except:
            pass

        return None

    def _read_docx_text(self, file_bytes: bytes) -> str:
        """Extracts text from a .docx file."""
        if docx is None:
            logger.error("❌ Cannot parse .docx: python-docx is not installed or failed to load.")
            return None
        try:
            from io import BytesIO
            doc = docx.Document(BytesIO(file_bytes))
            return "\n".join([para.text for para in doc.paragraphs])
        except Exception as e:
            logger.error(f"Failed to read docx: {e}")
            return None

    @app_commands.command(name="add-cookies", description="[Admin] Manually update session cookies for a platform.")
    @app_commands.describe(
        platform="The platform to update",
        cookies_text="Paste EditThisCookie JSON here",
        cookies_file="Upload .docx, .json, or .txt file",
        account_id="Optional: Specify which account to update (Default: primary)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Mecha Comic", value="mecha"),
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Kakao", value="kakao"),
        app_commands.Choice(name="Kuaikan", value="kuaikan"),
        app_commands.Choice(name="ACQQ", value="acqq"),
    ])
    async def add_cookies(
        self, 
        interaction: discord.Interaction, 
        platform: str, 
        cookies_text: str | None = None, 
        cookies_file: discord.Attachment | None = None,
        account_id: str = "primary"
    ):
        # Security check and deferral handled automatically by Cog-level interaction_check.
        
        target_account_id = account_id.strip().lower()
        if not target_account_id: target_account_id = "primary"
        
        raw_content = ""
        
        # 1. Extract raw text from input
        if cookies_file:
            try:
                file_bytes = await cookies_file.read()
                if cookies_file.filename.endswith(".docx"):
                    raw_content = self._read_docx_text(file_bytes)
                else:
                    raw_content = file_bytes.decode("utf-8", errors="ignore")
            except Exception as e:
                return await interaction.followup.send(f"❌ Failed to read uploaded file: `{e}`", ephemeral=True)
        
        if not raw_content and cookies_text:
            raw_content = cookies_text

        if not raw_content:
            return await interaction.followup.send("❌ Error: Could not extract any text from the provided inputs.", ephemeral=True)

        # 2. Parse Cookies
        cookies = self._parse_cookie_content(raw_content)
        if not cookies:
            return await interaction.followup.send(
                "❌ **Invalid Cookie Format.**\nPlease ensure you are using the **JSON Export** from the *EditThisCookie* extension.\n"
                "It should be a JSON array starting with `[` and ending with `]`.",
                ephemeral=True
            )

        # 3. Update Session
        try:
            service = SessionService()
            await service.update_session_cookies(platform, target_account_id, cookies)
            
            # --- ✅ New: Save as file if not exists (for persistence) ---
            try:
                # We also save to the local file system as a backup/persistence layer
                dir_path = os.path.join(os.getcwd(), "data", "secrets", platform)
                os.makedirs(dir_path, exist_ok=True)
                with open(os.path.join(dir_path, "cookies.json"), "w") as f:
                    json.dump(cookies, f, indent=4)
            except Exception as e:
                logger.error(f"Failed to sync cookies to disk: {e}")

            # --- 🔍 Diagnostics ---
            diagnostics = f"Successfully updated **{len(cookies)}** cookies for **{platform}** (Account: `{target_account_id}`)."
            
            # Platform Specific Health Check
            if platform == "piccoma":
                has_pksid = any(c.get('name') == 'pksid' and c.get('value') for c in cookies)
                if not has_pksid:
                    diagnostics += "\n\n⚠️ **WARNING:** `pksid` token was NOT found or is empty! Piccoma will likely treat this session as logged out."
            
            embed = discord.Embed(
                title="✅ Cookies Updated",
                description=diagnostics,
                color=0x2ecc71 if not (platform == "piccoma" and not has_pksid) else 0xf1c40f
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"[HelperCogs] Manual cookie update for {platform}:{target_account_id} by {interaction.user} (Count: {len(cookies)})")
            
        except Exception:
            logger.error(f"Manual cookie update failed")
            await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)

    # --- 12. AUTOMATED ACCOUNT MANAGEMENT ---

    @app_commands.command(name="add-account", description="[Admin] Register account credentials for automated headless login.")
    @app_commands.describe(
        platform="The platform (e.g. piccoma, mecha)",
        email="Account email/username",
        password="Account password",
        account_id="Optional: Target account ID (Default: primary)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Mecha Comic", value="mecha"),
    ])
    async def add_account(self, interaction: discord.Interaction, platform: str, email: str, password: str, account_id: str = "primary"):
        # Security check and deferral handled automatically by Cog-level interaction_check.
        
        success = await self.login_service.save_credentials(platform, email, password, account_id)
        if success:
            await interaction.followup.send(f"✅ **Account Registered:** Credentials for **{platform}** ({email}) saved for automated fallback.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Error:** Failed to save credentials. Check logs.", ephemeral=True)

    @app_commands.command(name="force-refresh", description="[Admin] Manually trigger the automated headless login to refresh cookies.")
    @app_commands.describe(
        platform="The platform to refresh",
        account_id="Optional: Target account ID (Default: primary)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="Mecha Comic", value="mecha"),
    ])
    async def force_refresh(self, interaction: discord.Interaction, platform: str, account_id: str = "primary"):
        """Manually trigger the headless login flow."""
        # Security check and deferral handled automatically by Cog-level interaction_check.
        
        await interaction.followup.send(f"🔄 **Refresh Initiated:** Attempting headless login for **{platform}**...", ephemeral=True)
        
        success = await self.login_service.auto_login(platform, account_id)
        if success:
            await interaction.followup.send(f"✅ **Refresh Successful:** Cookies for **{platform}** have been updated automatically.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ **Refresh Failed:** Automated login for **{platform}** could not be completed. Check logs.", ephemeral=True)

    # --- 13. CHECKPOINT SYNC ---

    @app_commands.command(
        name="sync-checkpoint",
        description="[Admin] Silently advance last-known chapter to current latest, preventing spam after downtime."
    )
    @app_commands.describe(
        group="The group profile name to sync",
        series="Series URL to sync (leave blank = sync ALL subscriptions in the group)"
    )
    @app_commands.autocomplete(group=group_name_autocomplete)
    async def sync_checkpoint(
        self,
        interaction: discord.Interaction,
        group: str,
        series: str = None,
    ):
        if not await self.interaction_check(interaction):
            return

        # Defer happens in interaction_check if not already done, but we want it ephemeral for this too
        # interaction_check defers if not done, so we are good.

        scraper_manager = self.main_bot.task_queue.provider_manager

        async def _sync_one(group_name: str, sub: dict) -> tuple[str, bool, str]:
            """Returns (title, success, new_id)"""
            try:
                scraper = scraper_manager.get_provider_for_url(sub["series_url"])
                if not scraper:
                    return sub.get("series_title", "?"), False, "No scraper found"
                
                data = await scraper.get_series_info(sub["series_url"])
                _, _, chapter_list, _, _, _, _, _, _ = data
                if not chapter_list:
                    return sub["series_title"], False, "no chapters"
                
                latest_id = str(chapter_list[-1]["id"])
                old_id = str(sub.get("last_known_chapter_id", "0"))
                
                if latest_id == old_id:
                    return sub["series_title"], True, f"already current ({latest_id})"
                
                update_last_chapter(group_name, sub["series_id"], latest_id)
                return sub["series_title"], True, f"{old_id} → {latest_id}"
            except Exception as e:
                return sub.get("series_title", "?"), False, str(e)

        results = []

        if series:
            # Single series mode
            series = series.strip("<>")
            all_subs = get_all_subscriptions()
            # Find the specific sub in the specified group, or search globally if group matches
            target = next(
                ((gn, s) for gn, s in all_subs if gn == group and s.get("series_url", "").rstrip("/") == series.rstrip("/")),
                None
            )
            
            if not target:
                # Try global search if not in specific group
                target = next(
                    ((gn, s) for gn, s in all_subs if s.get("series_url", "").rstrip("/") == series.rstrip("/")),
                    None
                )
            
            if not target:
                return await interaction.followup.send("❌ Series not found in subscriptions.", ephemeral=True)
            
            gn, sub = target
            title, ok, info = await _sync_one(gn, sub)
            results.append((title, ok, info))
        else:
            # Bulk mode — filter by group
            data = load_group(group)
            subs = data.get("subscriptions", [])
            if not subs:
                return await interaction.followup.send(f"❌ No subscriptions in `{group}`.", ephemeral=True)

            import asyncio
            sem = asyncio.Semaphore(5)  # don't hammer scrapers
            async def _guarded(sub):
                async with sem:
                    return await _sync_one(group, sub)

            results = await asyncio.gather(*[_guarded(s) for s in subs])

        # Build report
        lines = []
        for title, ok, info in results:
            icon = "✅" if ok else "❌"
            lines.append(f"{icon} **{title}** — `{info}`")

        report = "\n".join(lines) or "Nothing to report."
        
        # Split report into chunks if too long (Discord limit ~2000 chars)
        header = f"🔖 **Checkpoint Sync Complete** (`{group}`)\n\n"
        if len(header + report) > 1900:
            # Send in chunks
            await interaction.followup.send(header + "See below for details:", ephemeral=True)
            current_chunk = ""
            for line in lines:
                if len(current_chunk + line) > 1900:
                    await interaction.followup.send(current_chunk, ephemeral=True)
                    current_chunk = ""
                current_chunk += line + "\n"
            if current_chunk:
                await interaction.followup.send(current_chunk, ephemeral=True)
        else:
            await interaction.followup.send(header + report, ephemeral=True)

class GroupRemovalConfirmationView(discord.ui.View):
    def __init__(self, group_name, requester):
        super().__init__(timeout=3600) # 1 hour timeout
        self.group_name = group_name
        self.requester = requester

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from app.services.group_manager import delete_group
        from config.settings import Settings
        
        success = delete_group(self.group_name)
        
        if success:
            state = interaction.client.app_state # HelperBot inherits app_state
            # 1. Remove from Profiles Registry
            if self.group_name in state.group_profiles:
                state.group_profiles.remove(self.group_name)
                state.save_group_registry()
            
            # 2. Comprehensive Cleanup: SERVER_MAP
            # Remove any channel/guild mappings linked to this group
            to_remove = [k for k, v in state.server_map.items() if v == self.group_name]
            for k in to_remove:
                del state.server_map[k]
            
            if to_remove:
                state.save_group_registry()
                logger.info(f"[GroupRemoval] Cleaned up {len(to_remove)} mappings from SERVER_MAP for: {self.group_name}")

            await interaction.response.edit_message(
                content=f"✅ Group **{self.group_name}** has been successfully deleted.",
                embed=None, view=None
            )
            # Notify requester
            try:
                embed = discord.Embed(
                    title="✅ Group Removed",
                    description=f"The group **{self.group_name}** has been deleted after owner confirmation.",
                    color=0x2ecc71
                )
                await self.requester.send(embed=embed)
            except: pass
        else:
            await interaction.response.edit_message(content=f"❌ Failed to delete group **{self.group_name}**.", embed=None, view=None)
        
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"🚫 Removal of **{self.group_name}** has been cancelled.", embed=None, view=None)
        self.stop()


class GroupRenameConfirmationView(discord.ui.View):
    def __init__(self, old_name: str, new_name: str, requester):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.old_name = old_name
        self.new_name = new_name
        self.requester = requester

    @discord.ui.button(label="Confirm Rename", style=discord.ButtonStyle.success, emoji="📝")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        from app.services.group_manager import rename_group_profile
        from config.settings import Settings
        
        # 1. Rename the physical file
        if rename_group_profile(self.old_name, self.new_name):
            state = interaction.client.app_state
            # 2. Update Settings.GROUP_PROFILES
            if self.old_name in state.group_profiles:
                state.group_profiles.remove(self.old_name)
                state.group_profiles.add(self.new_name)
                state.save_group_registry()
            
            # 3. Update Settings.SERVER_MAP
            # Re-map any ID currently pointing to old_name to new_name
            to_update = [k for k, v in state.server_map.items() if v == self.old_name]
            for k in to_update:
                state.server_map[k] = self.new_name
            
            if to_update:
                state.save_group_registry()
                logger.info(f"[GroupRename] Updated {len(to_update)} mappings for: {self.old_name} → {self.new_name}")

            await interaction.response.edit_message(
                content=f"✅ Group **{self.old_name}** has been successfully renamed to **{self.new_name}**.",
                embed=None, view=None
            )
            
            # Notify requester
            try:
                embed = discord.Embed(
                    title="✅ Group Renamed",
                    description=f"The group **{self.old_name}** has been renamed to **{self.new_name}** after owner confirmation.",
                    color=0x2ecc71
                )
                await self.requester.send(embed=embed)
            except: pass
        else:
            await interaction.response.edit_message(content=f"❌ Failed to rename group **{self.old_name}** to **{self.new_name}**.", embed=None, view=None)
        
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content=f"🚫 Rename of **{self.old_name}** has been cancelled.", embed=None, view=None)
        self.stop()

async def setup(bot):
    await bot.add_cog(HelperSlashCog(bot))
