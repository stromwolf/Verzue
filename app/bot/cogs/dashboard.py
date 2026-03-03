import discord
from discord import app_commands
from discord.ext import commands
import logging
from config.settings import Settings

logger = logging.getLogger("Dashboard")

class PlatformModal(discord.ui.Modal):
    def __init__(self, platform_name: str, bot):
        # 1. Dynamic Modal Title (e.g., "Kakao Extractor")
        super().__init__(title=f'{platform_name} Menu')
        self.bot = bot
        self.platform_name = platform_name

        # 2. "Radio Group" Alternative
        self.action_input = discord.ui.TextInput(
            label='Action (Download / Subscription)',
            style=discord.TextStyle.short,
            placeholder='Type "Download" or "Subscribe"',
            default='Download',
            required=True
        )
        self.add_item(self.action_input)

        # 3. Dynamic Label wrapping the Text Input
        self.url_input = discord.ui.TextInput(
            label=f'Add {platform_name} link here:',
            style=discord.TextStyle.short,
            placeholder=f'Paste the {platform_name} URL...',
            required=True
        )
        self.add_item(self.url_input)

    async def on_submit(self, interaction: discord.Interaction):
        url = self.url_input.value.strip()
        action = self.action_input.value.strip().lower()
        
        msg = f"✅ **{self.platform_name} Request Received!**\n🔹 **Action:** `{action.title()}`\n🔗 **URL:** `{url}`\n*(Processing integration coming in next phase!)*"
        await interaction.response.send_message(msg, ephemeral=True)


class PlatformSelect(discord.ui.Select):
    def __init__(self, bot):
        self.bot = bot
        # The String Select Structure for your platforms
        options = [
            discord.SelectOption(label="KakaoPage", description="Download from Kakao", emoji="🇰🇷"),
            discord.SelectOption(label="Mecha Comic", description="Download from Mecha", emoji="🇯🇵"),
            discord.SelectOption(label="Jumptoon", description="Download from Jumptoon", emoji="🇰🇷"),
            discord.SelectOption(label="Kuaikan", description="Download from Kuaikan", emoji="🇨🇳"),
            discord.SelectOption(label="Piccoma", description="Download from Piccoma", emoji="🇯🇵"),
            discord.SelectOption(label="AC.QQ", description="Download from ACQQ", emoji="🇨🇳"),
        ]
        super().__init__(placeholder="Select Platform", min_values=1, max_values=1, options=options, custom_id="platform_select")

    async def callback(self, interaction: discord.Interaction):
        # When clicked, instantly launch the Modal with the chosen platform's name
        selected_platform = self.values[0]
        modal = PlatformModal(selected_platform, self.bot)
        await interaction.response.send_modal(modal)


class DashboardView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        # Add the String Select to the View
        self.add_item(PlatformSelect(self.bot))


class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME
        
        # Exact Embed Layout requested
        embed_text = (
            "***\n"
            "## Platform Lists\n"
            "**Available Platforms**\n"
            "* KakaoPage\n"
            "* Mecha Comic\n"
            "* Jumptoon\n"
            "* Kuaikan Manhua\n"
            "* Piccoma\n"
            "* AC.QQ\n\n"
            "**Coming Soon Platforms**\n"
            "- Naver Webtoon\n"
            "- Line Manga\n"
            "***\n"
            "## Your Commands\n"
            "Use the dropdown below to select your platform and choose your action."
        )

        embed = discord.Embed(
            title=f"# Dashboard of {scan_name}",
            description=embed_text,
            color=0x2b2d31 # Discord Dark Theme Color
        )
        
        view = DashboardView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
