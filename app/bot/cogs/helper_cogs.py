import discord
import logging
import asyncio
from discord import app_commands
from discord.ext import commands

from datetime import datetime, timedelta
from config.settings import Settings
from app.services.group_manager import load_group, add_subscription
from app.services.redis_manager import RedisManager
from app.services.gdrive.sync_service import sync_group_folder_name

logger = logging.getLogger("HelperCogs")

class HelperSlashCog(commands.Cog):
    """Slash commands mapped directly to the original Verzue Bot logic."""
    def __init__(self, bot):
        self.bot = bot # This is the HelperBot instance
        self.main_bot = bot.main_bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Global check to ensure only the owner or allowed CDN users can use these."""
        is_owner = interaction.user.id == 1216284053049704600
        is_allowed = interaction.user.id in Settings.CDN_ALLOWED_USERS
        if not (is_owner or is_allowed):
            await interaction.response.send_message("❌ **Access Denied.** You are not authorized to use admin commands.", ephemeral=True)
            return False
        return True

    def _get_current_group(self, interaction: discord.Interaction) -> str:
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id_origin = interaction.channel.id
        # We reuse the global dict!
        return Settings.SERVER_MAP.get(channel_id_origin) or Settings.SERVER_MAP.get(guild_id)

    # --- 1. GROUP ADD ---
    @app_commands.command(name="add-group", description="[Admin] Register a new group profile.")
    @app_commands.describe(
        name="The name of the new group (e.g., Thunder Scans)", 
        website="The group's website link",
        emoji="The discord emoji to use for this group's dashboard (Optional)"
    )
    async def add_group(self, interaction: discord.Interaction, name: str, website: str, emoji: str = None):
        if not name.strip():
            return await interaction.response.send_message("❌ Cannot create an empty group name.", ephemeral=True)
            
        clean_name = name.strip()
        clean_website = website.strip()
        
        if clean_name in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"⚠️ Group **{clean_name}** already exists in the registry.", ephemeral=True)
            
        Settings.GROUP_PROFILES.add(clean_name)
        Settings.save_group_profiles()
        
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
            
        await interaction.response.send_message(f"✅ **Group Profile Created:** `{clean_name}`\n🌐 **Website:** <{clean_website}>\nYou can now use `/register-server` to assign a server to this group.")

    # --- AUTOCOMPLETE HELPER ---
    async def group_name_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Provides autocomplete suggestions from registered group profiles."""
        return [
            app_commands.Choice(name=g, value=g)
            for g in sorted(Settings.GROUP_PROFILES)
            if current.lower() in g.lower()
        ][:25]

    # --- 1.5 GROUP EDIT ---
    @app_commands.command(name="edit-group", description="[Admin] Update a group's website or add a note.")
    @app_commands.describe(
        name="The name of the group to edit", 
        website="New website link (optional)", 
        note="A note for the group (optional)",
        emoji="New custom emoji for the dashboard (optional)"
    )
    @app_commands.autocomplete(name=group_name_autocomplete)
    async def edit_group(self, interaction: discord.Interaction, name: str, website: str = None, note: str = None, emoji: str = None):
        if not await self.interaction_check(interaction):
            return

        if name not in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"❌ **Unknown Group:** `{name}` is not a registered group profile.", ephemeral=True)

        if not website and not note and not emoji:
            return await interaction.response.send_message("⚠️ No changes provided. Please specify a new website, note, or emoji.", ephemeral=True)

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
            await interaction.response.send_message(msg)
            logger.info(f"[GroupManager] Updated profile via Helper UI: {name}")
        except Exception as e:
            logger.error(f"Failed to edit group profile: {e}")
            await interaction.response.send_message(f"❌ **Failed to update group:** {e}", ephemeral=True)

    # --- 2. REGISTER SERVER ---
    @app_commands.command(name="register-server", description="[Admin] Link a server or specific channel to a Group Profile")
    @app_commands.describe(
        name="Select the group profile",
        server="The Server ID to link",
        channel="Optional: Specific Channel ID to link (Only 1419393318147719170 allowed)"
    )
    @app_commands.autocomplete(name=group_name_autocomplete)
    async def register_server(
        self, 
        interaction: discord.Interaction, 
        name: str, 
        server: str, 
        channel: str = None
    ):
        if not await self.interaction_check(interaction):
            return

        if name not in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"❌ **Unknown Group:** `{name}` is not a registered group profile.", ephemeral=True)
            
        try:
            target_server_id = int(server.strip())
        except ValueError:
            return await interaction.response.send_message("❌ **Invalid Server ID.** Please provide a numeric ID.", ephemeral=True)

        if channel:
            try:
                target_channel_id = int(channel.strip())
            except ValueError:
                return await interaction.response.send_message("❌ **Invalid Channel ID.** Please provide a numeric ID.", ephemeral=True)
            
            # Specific validation: Channel-level mapping only allowed for server 1419393318147719170
            if target_server_id != 1419393318147719170:
                return await interaction.response.send_message(
                    "❌ **Restriction.** Channel-level mapping is only permitted for Server `1419393318147719170`.", 
                    ephemeral=True
                )
            
            Settings.SERVER_MAP[target_channel_id] = name
            target_display = f"Channel `{target_channel_id}`"
        else:
            Settings.SERVER_MAP[target_server_id] = name
            target_display = f"Server `{target_server_id}`"
            
        Settings.save_server_map()
        
        embed = discord.Embed(
            title="✅ Registration Complete",
            description=f"{target_display} is now linked to **{name}**.\nThe `/dashboard` will now identify as *Dashboard of {name}* in that scope.",
            color=0x2ecc21
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"[HelperCogs] Registered {target_display} -> {name} via /register-server")

    # --- 2.5 GROUP LIST ---
    @app_commands.command(name="group-list", description="[Admin] List all registered group profiles and their linked IDs.")
    async def group_list(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return

        if not Settings.GROUP_PROFILES:
            return await interaction.response.send_message("ℹ️ No Group Profiles have been registered yet.", ephemeral=True)

        embed = discord.Embed(
            title="📋 Registered Group Profiles",
            description="List of all groups and their active server/channel mappings.",
            color=0x3498db
        )

        for group_name in sorted(list(Settings.GROUP_PROFILES)):
            data = load_group(group_name)
            website = data.get("website", "*Not Set*")
            note = data.get("note")
            
            # Find linked IDs for this group
            links = [str(tid) for tid, name in Settings.SERVER_MAP.items() if name == group_name]
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

        await interaction.response.send_message(embed=embed)
    # --- 5. SUB LIST ---
    @app_commands.command(name="sub-list", description="[Admin] List all active subscriptions for a specific group")
    @app_commands.describe(group_name="The name of the group profile to list subscriptions for")
    @app_commands.autocomplete(group_name=group_name_autocomplete)
    async def sub_list(self, interaction: discord.Interaction, group_name: str):
        if group_name not in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"❌ **Unknown Group:** `{group_name}` is not a registered group profile.", ephemeral=True)

        data = load_group(group_name)
        subs = data.get("subscriptions", [])

        if not subs:
            return await interaction.response.send_message(f"ℹ️ No active subscriptions for **{group_name}**.")

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
        await interaction.response.send_message(embed=embed)



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

        if group not in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"❌ **Unknown Group:** `{group}` is not a registered group profile.", ephemeral=True)

        clean_url = series.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]

        from app.services.group_manager import remove_subscription
        success = remove_subscription(group, clean_url)

        if success:
            await interaction.response.send_message(f"✅ **Subscription Removed:** Series at <{clean_url}> has been removed from **{group}**.")
            logger.info(f"[HelperCogs] Removed subscription for {group}: {clean_url}")
        else:
            await interaction.response.send_message(f"⚠️ **Subscription Not Found:** Series at <{clean_url}> was not found in the subscription list for **{group}**.", ephemeral=True)


    # --- 8. ADMIN MANAGEMENT ---

    @app_commands.command(name="add-admin", description="[Admin] Grant a user access to admin commands.")
    @app_commands.describe(user="The user to authorize")
    async def add_admin(self, interaction: discord.Interaction, user: discord.User):
        if not await self.interaction_check(interaction):
            return

        Settings.CDN_ALLOWED_USERS.add(user.id)
        Settings.save_cdn_users()
        
        await interaction.response.send_message(f"✅ **Access Granted:** <@{user.id}> can now use helper bot admin commands.", ephemeral=False)

    @app_commands.command(name="remove-admin", description="[Admin] Revoke a user's access to admin commands.")
    @app_commands.describe(user="The user to de-authorize")
    async def remove_admin(self, interaction: discord.Interaction, user: discord.User):
        if not await self.interaction_check(interaction):
            return

        if user.id in Settings.CDN_ALLOWED_USERS:
            Settings.CDN_ALLOWED_USERS.remove(user.id)
            Settings.save_cdn_users()
            await interaction.response.send_message(f"🗑️ **Access Revoked:** <@{user.id}> can no longer use admin commands.", ephemeral=False)
        else:
            await interaction.response.send_message(f"⚠️ User <@{user.id}> was not in the admin list.", ephemeral=True)

    @app_commands.command(name="admin-list", description="[Admin] List all users with admin command access.")
    async def admin_list(self, interaction: discord.Interaction):
        if not await self.interaction_check(interaction):
            return

        if not Settings.CDN_ALLOWED_USERS:
            return await interaction.response.send_message("ℹ️ No users are currently in the admin list.", ephemeral=True)

        desc = "## Authorized Admin Users\n"
        for user_id in Settings.CDN_ALLOWED_USERS:
            desc += f"> • <@{user_id}> (`{user_id}`)\n"

        embed = discord.Embed(
            title="🔐 Admin User Access",
            description=desc,
            color=0x3498db
        )
        await interaction.response.send_message(embed=embed)

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
        if group_name not in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"❌ **Unknown Group:** `{group_name}` is not a registered group profile.", ephemeral=True)

        # Validate URL against supported platforms
        supported_domains = ["mechacomic.jp", "jumptoon.com", "piccoma.com", "kakao.com", "kuaikanmanhua.com", "ac.qq.com"]
        clean_url = series_link.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]

        if not any(d in clean_url.lower() for d in supported_domains):
            return await interaction.response.send_message(
                f"❌ **Unsupported Platform.**\nThe URL must be from one of: {', '.join(f'`{d}`' for d in supported_domains)}",
                ephemeral=True
            )

        clean_name = english_name.strip()
        if not clean_name:
            return await interaction.response.send_message("❌ English name cannot be empty.", ephemeral=True)

        from app.services.group_manager import set_title_override
        set_title_override(group_name, clean_url, clean_name)

        # 🟢 Sync Rename on Google Drive (Background)
        asyncio.create_task(sync_group_folder_name(self.bot, group_name, clean_url, clean_name))

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
        await interaction.response.send_message(embed=embed)

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
        if group_name not in Settings.GROUP_PROFILES or not _group_filename(group_name).exists():
            msg = f"❌ **Unknown Group:** `{group_name}` does not exist."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
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
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            
        except discord.Forbidden:
            msg = "❌ I cannot send DMs to the owner. Please ensure they have DMs enabled."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to initiate group removal: {e}")
            msg = f"❌ Failed to initiate removal: {e}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch leftover V2 interactions if any."""
        # We no longer need special handling for v2_modal_remove_group or v2_select_remove_group
        # but we keep the structure for other future V2 helper components if needed.
        pass

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
            # 1. Remove from Profiles Registry
            if self.group_name in Settings.GROUP_PROFILES:
                Settings.GROUP_PROFILES.remove(self.group_name)
                Settings.save_group_profiles()
            
            # 2. Comprehensive Cleanup: SERVER_MAP
            # Remove any channel/guild mappings linked to this group
            to_remove = [k for k, v in Settings.SERVER_MAP.items() if v == self.group_name]
            for k in to_remove:
                del Settings.SERVER_MAP[k]
            
            if to_remove:
                Settings.save_server_map()
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
        if not await self.interaction_check(interaction):
            return
            
        await interaction.response.defer()
        
        try:
            # 1. Extract Metadata (via main bot)
            scraper = self.main_bot.task_queue.provider_manager.get_provider_for_url(url)
            if not scraper:
                return await interaction.followup.send("❌ Error: No provider found for this URL.")
                
            data = await scraper.get_series_info(url)
            title, total_chapters, chapter_list, image_url, series_id, _, _ = data
            
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
                "release_time": utc_time
            }
            
            # 5. Save
            success = add_subscription(group_name, sub)
            if success:
                embed = discord.Embed(
                    title="✅ Manual Subscription Added",
                    description=f"**{title}** (`{series_id}`)\nGroup: **{group_name}**",
                    color=0x2ecc71
                )
                embed.add_field(name="Input Schedule", value=f"`{day} @ {time} {timezone}`", inline=True)
                embed.add_field(name="UTC Result", value=f"`{utc_day} @ {utc_time}`", inline=True)
                if image_url: embed.set_thumbnail(url=image_url)
                
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(f"❌ Series `{series_id}` is already subscribed in **{group_name}**.")
                
        except Exception as e:
            logger.error(f"Manual Sub Add failed: {e}")
            await interaction.followup.send(f"❌ Failed to add subscription: `{e}`")

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
        if not await self.interaction_check(interaction):
            return
            
        await interaction.response.defer(ephemeral=True)
        
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
                    await RedisManager().set_session(platform, aid, session)
                    count += 1
            
            await interaction.followup.send(f"✅ Successfully reset **{count}** sessions for **{platform}** to `HEALTHY`.", ephemeral=True)
            logger.info(f"[HelperCogs] Manual session reset for {platform} by {interaction.user}")
            
        except Exception as e:
            logger.error(f"Manual session reset failed: {e}")
            await interaction.followup.send(f"❌ Failed to reset sessions: `{e}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(HelperSlashCog(bot))
