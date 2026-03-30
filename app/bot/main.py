import discord
import os
import asyncio
from discord.ext import commands
import logging
from app.core.events import EventBus
from config.settings import Settings

class MechaBot(commands.Bot):
    def __init__(self, token: str, task_queue, redis_brain=None):
        # Enable message content intent
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix=["!", "$"], 
            intents=intents,
            help_command=None
        )
        
        self.token_str = token
        self.task_queue = task_queue
        self.redis_brain = redis_brain
        self.logger = logging.getLogger("Bot")

    async def setup_hook(self):
        """Loads extensions and subscribes to events."""
        # Removed all individual site cogs!
        extensions = [
            "app.bot.cogs.admin",
            "app.bot.cogs.dashboard",
            "app.bot.cogs.subscriptions",
            "app.bot.cogs.discovery"
        ]
        
        for ext in extensions:
            try:
                await self.load_extension(ext)
                self.logger.info(f"🧩 Loaded: {ext}")
            except Exception as e:
                self.logger.error(f"❌ Failed to load {ext}: {e}")

        # --- EVENT SUBSCRIPTIONS ---
        # 1. Zip Uploads
        EventBus.subscribe("upload_zip_to_discord", self.handle_zip_upload)
        
        # 2. Direct Links
        EventBus.subscribe("send_direct_link", self.handle_direct_link)
        
        # 3. Error Alerts
        EventBus.subscribe("task_failed", self.handle_task_failure)

        # 4. Start UI Dispatcher
        from app.services.ui_manager import UIManager
        await UIManager().start()

        # 5. Start the internal worker loops in the background!
        asyncio.create_task(self.task_queue.start_worker(num_workers=2))

        # 6. Start the Auto-Download Poller
        from app.tasks.poller import AutoDownloadPoller
        self.auto_poller = AutoDownloadPoller(self)

        # Sync Slash Commands
        try:
            synced = await self.tree.sync()
            self.logger.info(f"⚡ Synced {len(synced)} slash commands.")
        except Exception as e:
            self.logger.error(f"❌ Command sync failed: {e}")

    async def start_bot(self):
        """Custom start method to handle login."""
        await self.start(self.token_str)

    # --- EVENT HANDLERS (Must be inside the MechaBot class) ---

    async def handle_zip_upload(self, task, zip_path):
        """Uploads a local ZIP file to Discord."""
        try:
            channel = self.get_channel(task.channel_id)
            if not channel: return

            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if size_mb > 24.0: 
                await channel.send(f"⚠️ **File Too Large ({size_mb:.1f}MB)**. Use the Drive link.")
            else:
                await channel.send(
                    content=f"📦 **Direct Download:** `{task.folder_name}`",
                    file=discord.File(zip_path, filename=f"{task.folder_name}.zip")
                )
        except Exception as e:
            self.logger.error(f"Failed to upload ZIP: {e}")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    async def handle_direct_link(self, task, link):
        """Sends a Google Drive Direct Download link."""
        try:
            channel = self.get_channel(task.channel_id)
            if channel:
                await channel.send(f"📦 **Direct Download Ready:** `{task.folder_name}`\n⬇️ {link}")
        except Exception as e:
            self.logger.error(f"Failed to send link: {e}")

    async def handle_task_failure(self, task, error_message):
        """Alerts the user when a task crashes."""
        try:
            channel = self.get_channel(task.channel_id)
            if channel:
                # Clean error message logic (No backslashes inside f-string)
                err_text = str(error_message).split('\n')[0]
                
                embed = discord.Embed(
                    title="❌ Extraction Failed",
                    description=f"**Chapter:** `{task.title}`\n**Reason:** {err_text}",
                    color=0xe74c3c
                )
                embed.set_footer(text=f"Task ID: {task.id}")
                await channel.send(embed=embed)
        except Exception as e:
            self.logger.error(f"Failed to send error alert: {e}")