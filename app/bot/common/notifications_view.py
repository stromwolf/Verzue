import discord
from discord import ui
from app.services.settings_service import SettingsService, NOTIFY_LIMIT


class NotificationsView(ui.View):
    """Settings panel for managing ping targets."""

    def __init__(self, user_id: int, guild: discord.Guild | None = None, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.guild = guild
        self.settings = SettingsService()

    async def _build_embed(self) -> discord.Embed:
        targets = await self.settings.get_notify_targets(self.user_id)
        embed = discord.Embed(
            title="🔔 Notification Recipients",
            description="Who gets pinged when your chapters finish.",
            color=0x5865F2,
        )

        if not targets:
            lines = "*No targets set — only you will be pinged.*"
        else:
            lines = ""
            for t in targets:
                mention = f"<@{t['id']}>" if t["type"] == "user" else f"<@&{t['id']}>"
                name = ""
                if self.guild:
                    if t["type"] == "user":
                        member = self.guild.get_member(int(getattr(t["id"], "id", t["id"])))
                        if member: name = f" (`@{member.display_name}`)"
                    else:
                        role = self.guild.get_role(int(getattr(t["id"], "id", t["id"])))
                        if role: name = f" (`{role.name}`)"
                
                lines += f"• {mention}{name}\n"

        embed.add_field(name=f"Targets ({len(targets)}/{NOTIFY_LIMIT})", value=lines, inline=False)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        # Rebuild children based on current state
        self.clear_items()
        targets = await self.settings.get_notify_targets(self.user_id)
        at_limit = len(targets) >= NOTIFY_LIMIT

        self.add_item(AddUserSelect(self, disabled=at_limit))
        self.add_item(AddRoleSelect(self, disabled=at_limit))
        if targets:
            self.add_item(RemoveSelect(self, targets))

        embed = await self._build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Not your settings panel.", ephemeral=True
            )
            return False
        return True


class AddUserSelect(ui.UserSelect):
    def __init__(self, view_ref: NotificationsView, *, disabled: bool):
        super().__init__(
            placeholder="➕ Add a user…",
            min_values=1,
            max_values=1,
            disabled=disabled,
            row=0,
        )
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        target = self.values[0]
        ok, msg = await self.view_ref.settings.add_notify_target(
            self.view_ref.user_id, "user", target.id
        )
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
        await self.view_ref.refresh(interaction)


class AddRoleSelect(ui.RoleSelect):
    def __init__(self, view_ref: NotificationsView, *, disabled: bool):
        super().__init__(
            placeholder="➕ Add a role…",
            min_values=1,
            max_values=1,
            disabled=disabled,
            row=1,
        )
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        target = self.values[0]
        ok, msg = await self.view_ref.settings.add_notify_target(
            self.view_ref.user_id, "role", target.id
        )
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
        await self.view_ref.refresh(interaction)


class RemoveSelect(ui.Select):
    def __init__(self, view_ref: NotificationsView, targets: list):
        options = []
        for t in targets:
            label = ""
            if view_ref.guild:
                if t["type"] == "user":
                    member = view_ref.guild.get_member(int(getattr(t["id"], "id", t["id"])))
                    label = f"@{member.display_name}" if member else f"User: {t['id']}"
                    emoji = "👤"
                else:
                    role = view_ref.guild.get_role(int(getattr(t["id"], "id", t["id"])))
                    label = f"Role: {role.name}" if role else f"Role: {t['id']}"
                    emoji = "🎭"
            else:
                label = f"{t['type'].capitalize()}: {t['id']}"
                emoji = "👤" if t["type"] == "user" else "🎭"
                
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"{t['type']}:{t['id']}",
                    emoji=emoji,
                )
            )
        super().__init__(
            placeholder="➖ Remove a target…",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ttype, tid = self.values[0].split(":", 1)
        await self.view_ref.settings.remove_notify_target(self.view_ref.user_id, ttype, tid)
        await self.view_ref.refresh(interaction)


class SubscriptionToggleView(ui.View):
    """View to toggle subscription status (ON/OFF)."""

    def __init__(self, user_id: int, guild: discord.Guild | None = None, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.guild = guild
        self.settings = SettingsService()

    async def _build_embed(self) -> discord.Embed:
        from app.services.group_manager import get_user_subscriptions
        subs = get_user_subscriptions(self.user_id)
        
        embed = discord.Embed(
            title="📋 Subscription Management",
            description="Toggle your subscriptions ON/OFF. Disabled subscriptions won't be polled for updates.",
            color=0x2ECC71,
        )

        if not subs:
            embed.description = "*You haven't added any subscriptions yet.*"
        else:
            lines = []
            for gn, sub in subs:
                s_id = sub["series_id"]
                s_settings = await self.settings.get_subscription_settings(self.user_id, s_id)
                status = "🟢 ON" if s_settings.get("enabled", True) else "🔴 OFF"
                title = s_settings.get("custom_title") or sub["series_title"]
                lines.append(f"**{title}** — {status}\n-# Group: {gn} | S-ID: {s_id}")
            
            embed.add_field(name="Your Subscriptions", value="\n".join(lines)[:1024], inline=False)
            self.clear_items()
            self.add_item(SubscriptionSelect(self, subs))

        return embed

    async def refresh(self, interaction: discord.Interaction):
        embed = await self._build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id


class SubscriptionSelect(ui.Select):
    def __init__(self, view_ref: SubscriptionToggleView, subs: list):
        options = []
        for gn, sub in subs:
            options.append(
                discord.SelectOption(
                    label=sub["series_title"][:100],
                    value=sub["series_id"],
                    description=f"Group: {gn}",
                    emoji="📖"
                )
            )
        super().__init__(placeholder="Select a series to toggle...", options=options)
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        s_id = self.values[0]
        settings = await self.view_ref.settings.get_subscription_settings(self.view_ref.user_id, s_id)
        new_state = not settings.get("enabled", True)
        await self.view_ref.settings.update_subscription_settings(self.view_ref.user_id, s_id, {"enabled": new_state})
        await self.view_ref.refresh(interaction)


class SeriesTitleRenameView(ui.View):
    """View to rename series display titles."""

    def __init__(self, user_id: int, guild: discord.Guild | None = None, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.guild = guild
        self.settings = SettingsService()

    async def _build_embed(self) -> discord.Embed:
        from app.services.group_manager import get_user_subscriptions
        subs = get_user_subscriptions(self.user_id)
        
        embed = discord.Embed(
            title="✏️ Series Title Management",
            description="Rename your series for better display in the dashboard and pings.",
            color=0xF1C40F,
        )

        if not subs:
            embed.description = "*You haven't added any subscriptions yet.*"
        else:
            lines = []
            for gn, sub in subs:
                s_id = sub["series_id"]
                s_settings = await self.settings.get_subscription_settings(self.user_id, s_id)
                custom = s_settings.get("custom_title")
                title = f"**{custom}** (was: {sub['series_title']})" if custom else f"**{sub['series_title']}**"
                lines.append(f"{title}\n-# Group: {gn} | S-ID: {s_id}")
            
            embed.add_field(name="Current Titles", value="\n".join(lines)[:1024], inline=False)
            self.clear_items()
            self.add_item(SeriesTitleSelect(self, subs))

        return embed

    async def refresh(self, interaction: discord.Interaction):
        embed = await self._build_embed()
        await interaction.edit_original_response(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id


class SeriesTitleSelect(ui.Select):
    def __init__(self, view_ref: SeriesTitleRenameView, subs: list):
        options = []
        for gn, sub in subs:
            options.append(
                discord.SelectOption(
                    label=sub["series_title"][:100],
                    value=sub["series_id"],
                    description=f"Group: {gn}",
                    emoji="✏️"
                )
            )
        super().__init__(placeholder="Select a series to rename...", options=options)
        self.view_ref = view_ref

    async def callback(self, interaction: discord.Interaction):
        s_id = self.values[0]
        # Find the sub to get the current title
        from app.services.group_manager import get_user_subscriptions
        subs = get_user_subscriptions(self.view_ref.user_id)
        # Fix: correctly unpack the tuple
        match = next(((gn, s) for gn, s in subs if s["series_id"] == s_id), None)
        
        if not match:
            return await interaction.response.send_message("Series not found.", ephemeral=True)

        gn, sub = match
        modal = SeriesRenameModal(self.view_ref, s_id, sub["series_title"])
        await interaction.response.send_modal(modal)


class SeriesRenameModal(ui.Modal, title="Rename Series"):
    new_title = ui.TextInput(
        label="New Display Title",
        placeholder="Enter a custom name...",
        min_length=1,
        max_length=100,
        required=True
    )

    def __init__(self, view_ref: SeriesTitleRenameView, series_id: str, current_title: str):
        super().__init__()
        self.view_ref = view_ref
        self.series_id = series_id
        self.new_title.default = current_title

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view_ref.settings.update_subscription_settings(
            self.view_ref.user_id, 
            self.series_id, 
            {"custom_title": self.new_title.value}
        )
        await self.view_ref.refresh(interaction)
