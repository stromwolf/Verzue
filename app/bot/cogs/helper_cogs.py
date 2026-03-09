import discord
from discord import app_commands
from discord.ext import commands
import logging

from config.settings import Settings
from app.services.group_manager import set_release_day, load_group, remove_subscription, set_admin_settings

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
    @app_commands.command(name="group-add", description="[Admin] Register a new group profile.")
    @app_commands.describe(group_name="The name of the new group (e.g., Thunder Scans)")
    async def group_add(self, interaction: discord.Interaction, group_name: str):
        if not group_name.strip():
            return await interaction.response.send_message("❌ Cannot create an empty group name.", ephemeral=True)
            
        clean_name = group_name.strip()
        
        if clean_name in Settings.GROUP_PROFILES:
            return await interaction.response.send_message(f"⚠️ Group **{clean_name}** already exists in the registry.", ephemeral=True)
            
        Settings.GROUP_PROFILES.add(clean_name)
        Settings.save_group_profiles()
        
        # Create empty profile JSON
        try:
            from app.services.group_manager import _group_filename
            import json
            filepath = Settings.GROUPS_DIR / _group_filename(clean_name)
            if not filepath.exists():
                with open(filepath, 'w', encoding='utf-8') as f:
                    json.dump({"subscriptions": []}, f, indent=4)
                logger.info(f"[GroupManager] Created profile via Helper UI: {_group_filename(clean_name)}")
        except Exception as e:
            logger.error(f"Failed to create group profile JSON: {e}")
            
        await interaction.response.send_message(f"✅ **Group Profile Created:** `{clean_name}`\nYou can now use `/cdn-menu` to assign a server to this group.")

    # --- 2. CDN MENU ---
    @app_commands.command(name="cdn-menu", description="[Admin] Link this server/channel to a registered Group Profile")
    async def cdn_menu(self, interaction: discord.Interaction):
        if not Settings.GROUP_PROFILES:
            return await interaction.response.send_message("❌ No Group Profiles have been registered yet. Please use `/group-add` first.")
            
        options = [
            discord.SelectOption(label=group, description=f"Link to {group}") 
            for group in sorted(list(Settings.GROUP_PROFILES))[:25]
        ]
        
        select = discord.ui.Select(placeholder="Select a Group Profile...", options=options, custom_id="helper_cdn_select")
        
        async def select_callback(inter: discord.Interaction):
            group = select.values[0]
            
            class ConfirmView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=60)
                    self.target = None
                
                @discord.ui.button(label="Apply to Server", style=discord.ButtonStyle.primary, custom_id="helper_apply_server")
                async def btn_server(self, b_inter: discord.Interaction, button: discord.ui.Button):
                    self.target = "server"
                    self.stop()
                    await b_inter.response.defer()
                
                @discord.ui.button(label="Apply to Channel Only", style=discord.ButtonStyle.secondary, custom_id="helper_apply_channel")
                async def btn_channel(self, b_inter: discord.Interaction, button: discord.ui.Button):
                    self.target = "channel"
                    self.stop()
                    await b_inter.response.defer()
            
            view = ConfirmView()
            await inter.response.edit_message(content=f"You selected **{group}**. How should this mapping be applied?", view=view)
            await view.wait()
            
            if view.target == "server":
                gid = inter.guild.id if inter.guild else 0
                Settings.SERVER_MAP[gid] = group
                msg = f"✅ Server is now linked to **{group}**."
            elif view.target == "channel":
                cid = inter.channel.id
                Settings.SERVER_MAP[cid] = group
                msg = f"✅ This specific channel is now linked to **{group}**."
            else:
                return
                
            Settings.save_server_map()
            await inter.edit_original_response(content=msg, view=None)

        select.callback = select_callback
        view = discord.ui.View()
        view.add_item(select)
        
        await interaction.response.send_message("Select the Group Profile to assign:", view=view)

    # --- 3. ADMIN ALERTS ---
    @app_commands.command(name="set-admin", description="[Admin] Set the alert channel for new subscriptions")
    @app_commands.describe(channel="The channel where alerts should go", role="Optional role to ping when an alert happens")
    async def set_admin(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role = None):
        group_name = self._get_current_group(interaction)
        if not group_name:
            return await interaction.response.send_message("❌ This server is not mapped to any Group Profile. Use `/cdn-menu` first.", ephemeral=True)

        admin_channel_id = channel.id
        role_id = role.id if role else None

        set_admin_settings(group_name, admin_channel_id, role_id)
        
        msg = f"✅ New subscription alerts for **{group_name}** will be sent to <#{admin_channel_id}>."
        if role_id:
            msg += f"\nIt will tag <@&{role_id}>."
        await interaction.response.send_message(msg)

    # --- 4. SUB DAY ---
    @app_commands.command(name="sub-day", description="[Admin] Set the weekly check day for a series")
    @app_commands.describe(url="Series URL (e.g. https://jumptoon.com/...)", day="The day of the week to check")
    @app_commands.choices(day=[
        app_commands.Choice(name="Monday", value="Monday"),
        app_commands.Choice(name="Tuesday", value="Tuesday"),
        app_commands.Choice(name="Wednesday", value="Wednesday"),
        app_commands.Choice(name="Thursday", value="Thursday"),
        app_commands.Choice(name="Friday", value="Friday"),
        app_commands.Choice(name="Saturday", value="Saturday"),
        app_commands.Choice(name="Sunday", value="Sunday"),
    ])
    async def sub_day(self, interaction: discord.Interaction, url: str, day: app_commands.Choice[str]):
        group_name = self._get_current_group(interaction)
        if not group_name:
            return await interaction.response.send_message("❌ This server is not mapped to any Group Profile.", ephemeral=True)

        clean_url = url.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]
            
        updated = set_release_day(group_name, clean_url, day.value)
        if updated:
            embed = discord.Embed(
                title="✅ Release Day Updated",
                description=f"Subscription for `<{clean_url}>` will now be checked every **{day.value}**.",
                color=0x2ecc71
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"❌ Could not find a subscription matching that URL in the {group_name} profile.", ephemeral=True)

    # --- 5. SUB LIST ---
    @app_commands.command(name="sub-list", description="[Admin] List all active subscriptions")
    async def sub_list(self, interaction: discord.Interaction):
        group_name = self._get_current_group(interaction)
        if not group_name:
            return await interaction.response.send_message("❌ This server is not mapped to any Group Profile.", ephemeral=True)

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

    # --- 6. SUB REMOVE ---
    @app_commands.command(name="sub-remove", description="[Admin] Completely remove a subscription")
    @app_commands.describe(url="Series URL")
    async def sub_remove(self, interaction: discord.Interaction, url: str):
        group_name = self._get_current_group(interaction)
        if not group_name:
            return await interaction.response.send_message("❌ This server is not mapped to any Group Profile.", ephemeral=True)

        clean_url = url.strip()
        if clean_url.startswith("<") and clean_url.endswith(">"):
            clean_url = clean_url[1:-1]

        removed = remove_subscription(group_name, clean_url)
        if removed:
            embed = discord.Embed(
                title="🗑️ Subscription Removed",
                description=f"Stopped tracking URL for {group_name}.",
                color=0xe74c3c
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"❌ Could not find an active subscription for that URL.", ephemeral=True)

    # --- PREFIX COMMAND: SYNC ---
    @commands.command(name="sync")
    async def sync(self, ctx):
        """[Admin] Manually sync slash commands for the helper bot."""
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        if not (is_owner or is_allowed):
            return await ctx.send("❌ **Access Denied.** You are not authorized to use admin commands.")
            
        msg = await ctx.send("🔄 Clearing old commands and syncing helper bot...")
        try:
            # 1. Clear guild-specific commands (if any got stuck)
            if ctx.guild:
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                
            # 2. Sync global commands
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ Synced **{len(synced)}** slash commands globally. *(Note: Global sync can take up to an hour to show up in all servers)*")
            logger.info(f"⚡ Helper Synced {len(synced)} slash commands via $sync.")
        except Exception as e:
            await msg.edit(content=f"❌ Failed to sync: {e}")
            logger.error(f"❌ Helper Command sync failed: {e}")

async def setup(bot):
    await bot.add_cog(HelperSlashCog(bot))
