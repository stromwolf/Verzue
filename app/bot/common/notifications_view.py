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
                        member = self.guild.get_member(int(t["id"]))
                        if member: name = f" (`@{member.display_name}`)"
                    else:
                        role = self.guild.get_role(int(t["id"]))
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
    def __init__(self, parent: NotificationsView, *, disabled: bool):
        super().__init__(
            placeholder="➕ Add a user…",
            min_values=1,
            max_values=1,
            disabled=disabled,
            row=0,
        )
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        target = self.values[0]
        ok, msg = await self.parent.settings.add_notify_target(
            self.parent.user_id, "user", target.id
        )
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
        await self.parent.refresh(interaction)


class AddRoleSelect(ui.RoleSelect):
    def __init__(self, parent: NotificationsView, *, disabled: bool):
        super().__init__(
            placeholder="➕ Add a role…",
            min_values=1,
            max_values=1,
            disabled=disabled,
            row=1,
        )
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        target = self.values[0]
        ok, msg = await self.parent.settings.add_notify_target(
            self.parent.user_id, "role", target.id
        )
        if not ok:
            await interaction.followup.send(msg, ephemeral=True)
        await self.parent.refresh(interaction)


class RemoveSelect(ui.Select):
    def __init__(self, parent: NotificationsView, targets: list):
        options = []
        for t in targets:
            label = ""
            if parent.guild:
                if t["type"] == "user":
                    member = parent.guild.get_member(int(t["id"]))
                    label = f"@{member.display_name}" if member else f"User: {t['id']}"
                    emoji = "👤"
                else:
                    role = parent.guild.get_role(int(t["id"]))
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
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ttype, tid = self.values[0].split(":", 1)
        await self.parent.settings.remove_notify_target(self.parent.user_id, ttype, tid)
        await self.parent.refresh(interaction)
