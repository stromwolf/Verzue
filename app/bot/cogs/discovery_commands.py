import discord
from discord import app_commands
from discord.ext import commands
import logging
from app.core.logger import logger
from app.providers.manager import ProviderManager

class PremiereSyncView(discord.ui.View):
    def __init__(self, platform: str, series_data: list, main_bot):
        super().__init__(timeout=300)
        self.platform = platform
        self.series_data = series_data
        self.main_bot = main_bot

    @discord.ui.button(label="Mark All as Known (Seed Brain)", style=discord.ButtonStyle.green, emoji="🧠")
    async def mark_known(self, interaction: discord.Interaction, button: discord.ui.Button):
        redis_key = f"verzue:seen:{self.platform.lower()}_new_series"
        
        # Batch add to Redis using the main_bot's brain
        ids = [s["series_id"] for s in self.series_data]
        if ids:
            await self.main_bot.redis_brain.client.sadd(redis_key, *ids)
            
        await interaction.response.edit_message(
            content=f"✅ **Success!** Brain hydrated with {len(ids)} series for **{self.platform.capitalize()}**.\nThe poller will now ignore these and only notify you of truly new premieres.",
            embed=None,
            view=None
        )

class DiscoveryCommands(commands.Cog):
    """Cog for discovery-related slash commands, intended for the Helper Bot."""
    def __init__(self, bot):
        self.bot = bot
        self.pm = ProviderManager()
        # Ensure we have a reference to the main bot (passed from HelperBot)
        self.main_bot = getattr(bot, 'main_bot', bot)

    @app_commands.command(name="premiere-sync", description="Visualize and seed the Premiere Detection brain.")
    @app_commands.describe(platform="The platform to sync")
    @app_commands.choices(platform=[
        app_commands.Choice(name="Jumptoon", value="jumptoon"),
        app_commands.Choice(name="Piccoma", value="piccoma"),
        app_commands.Choice(name="MechaComic", value="mecha")
    ])
    async def premiere_sync(self, interaction: discord.Interaction, platform: app_commands.Choice[str]):
        platform_val = platform.value
        await interaction.response.defer(ephemeral=True)
        
        try:
            provider = self.pm.get_provider(platform_val)
            new_series = await provider.get_new_series_list()
            
            if not new_series:
                await interaction.followup.send(f"⚠️ No series found on the **{platform.name}** 'New' page.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"🧠 Premiere Brain Sync: {platform.name}",
                description=f"Found **{len(new_series)}** series on the 'New' page.\nClick the button below to mark all of these as 'Known'.",
                color=discord.Color.blue()
            )
            
            # List first 15 for preview
            preview = "\n".join([f"• **{s['title']}** (`{s['series_id']}`)" for s in new_series[:15]])
            if len(new_series) > 15:
                preview += f"\n*...and {len(new_series)-15} more*"
                
            embed.add_field(name="Current Series In Discovery List", value=preview, inline=False)
            
            view = PremiereSyncView(platform_val, new_series, self.main_bot)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Premiere Sync Error: {e}")
            await interaction.followup.send(f"❌ Error during sync: `{e}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(DiscoveryCommands(bot))
