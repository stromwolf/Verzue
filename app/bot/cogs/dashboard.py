import discord
from discord import app_commands
from discord.ext import commands
import logging
from config.settings import Settings

logger = logging.getLogger("Dashboard")

class DownloadModal(discord.ui.Modal, title='Universal Extractor'):
    url_input = discord.ui.TextInput(
        label='Manga / Webtoon URL',
        style=discord.TextStyle.short,
        placeholder='Paste the chapter or series link here...',
        required=True
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        url = self.url_input.value.strip()
        
        # We will bridge this to your scraper logic in the next phase!
        # For now, we just acknowledge receipt to test the UI.
        await interaction.response.send_message(f"🔗 **URL Received:** `{url}`
*(Processing integration coming in next phase!)*", ephemeral=True)


class DashboardView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None) # Timeout None ensures the button never expires
        self.bot = bot

    @discord.ui.button(label="📥 Submit URL", style=discord.ButtonStyle.primary, custom_id="dashboard_submit_btn")
    async def extract_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Triggers the Modal popup
        await interaction.response.send_modal(DownloadModal(self.bot))


class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        # Look for a Channel ID override first, then fallback to Guild ID, then Default
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME
        
        embed = discord.Embed(
            title=f"Menu of {scan_name}",
            description="Welcome to the extraction dashboard.
Click the button below to process a new link.",
            color=0x2b2d31 # Dark Discord UI theme color
        )
        
        view = DashboardView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
