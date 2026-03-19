import discord
from discord.ext import commands
import logging
from config.settings import Settings

class HelperBot(commands.Bot):
    """A streamlined bot instance dedicated solely to providing UI and Slash Commands."""
    def __init__(self, token: str, main_bot):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=[],
            intents=intents,
            help_command=None
        )
        self.token_str = token
        self.main_bot = main_bot # Pass a reference to the main bot for shared memory (e.g. task_queue)
        self.logger = logging.getLogger("HelperBot")

    async def setup_hook(self):
        """Loads extensions and syncs commands."""
        extension = "app.bot.cogs.helper_cogs"
        try:
            await self.load_extension(extension)
            self.logger.info(f"🧩 Helper Loaded: {extension}")
        except Exception as e:
            self.logger.error(f"❌ Failed to load Helper Cog {extension}: {e}")

        # Sync Slash Commands
        try:
            synced = await self.tree.sync()
            self.logger.info(f"⚡ Helper Synced {len(synced)} slash commands.")
        except Exception as e:
            self.logger.error(f"❌ Helper Command sync failed: {e}")

    async def start_bot(self):
        """Custom start method to handle login."""
        await self.start(self.token_str)
