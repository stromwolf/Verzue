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
            "app.bot.cogs.discovery",
            "app.bot.cogs.status"
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
        """Alerts the user AND the admin channel when a task crashes."""
        try:
            # 1. Notify User Channel
            user_channel = self.get_channel(task.channel_id)
            err_text = str(error_message).split('\n')[0]
            
            embed = discord.Embed(
                title="❌ Extraction Failed",
                description=f"**Chapter:** `{task.title}`\n**Reason:** {err_text}",
                color=0xe74c3c
            )
            embed.set_footer(text=f"Task ID: {task.id}")

            if user_channel:
                await user_channel.send(embed=embed)

            # 2. Notify Admin Audit Channel
            admin_channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if not admin_channel:
                try:
                    admin_channel = await self.fetch_channel(Settings.ADMIN_LOG_CHANNEL_ID)
                except: pass
            
            if admin_channel and admin_channel.id != task.channel_id:
                admin_embed = embed.copy()
                admin_embed.title = "🚨 [ADMIN AUDIT] Task Failed"
                admin_embed.add_field(name="Group", value=f"`{task.scan_group}`", inline=True)
                admin_embed.add_field(name="User ID", value=f"`{task.user_id}`", inline=True)
                await admin_channel.send(embed=admin_embed)

        except Exception as e:
            self.logger.error(f"Failed to send extraction error alert: {e}")

    # --- S-GRADE: GLOBAL CRASH SENTINEL ---
    async def dispatch_error(self, error: Exception, ctx: commands.Context = None, interaction: discord.Interaction = None, event: str = None):
        """Standardized crash reporter that dispatches reports to the admin legacy channel."""
        import traceback
        self.logger.error(f"Sentinel Dispatch: {error}")

        try:
            # 1. Resolve Admin Channel (Persistent)
            admin_channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if not admin_channel:
                try:
                    admin_channel = await self.fetch_channel(Settings.ADMIN_LOG_CHANNEL_ID)
                except:
                    self.logger.error("Sentinel failed to fetch admin channel!")
                    return

            # 2. Build Report Embed
            title = "💥 Command Crash Detected" if (ctx or interaction) else "💀 Critical System Event Error"
            if event: title = f"💀 System Error: {event}"
            
            color = 0xc0392b if (ctx or interaction) else 0x000000
            
            embed = discord.Embed(
                title=title,
                description=f"**Error:** `{str(error)}`",
                color=color,
                timestamp=discord.utils.utcnow()
            )
            
            if ctx:
                embed.add_field(name="Command", value=f"`{ctx.command}`", inline=True)
                embed.add_field(name="User", value=f"{ctx.author} (`{ctx.author.id}`)", inline=True)
                embed.add_field(name="Channel", value=f"<#{ctx.channel.id}>", inline=True)
            elif interaction:
                cmd_name = interaction.data.get('name', 'Component/Interaction') if interaction.data else 'Interaction'
                embed.add_field(name="Interaction", value=f"`{cmd_name}`", inline=True)
                embed.add_field(name="User", value=f"{interaction.user} (`{interaction.user.id}`)", inline=True)
                embed.add_field(name="Channel", value=f"<#{interaction.channel_id}>", inline=True)
            elif event:
                embed.add_field(name="Event", value=f"`{event}`", inline=True)
            
            # Add Traceback Summary
            tb = traceback.format_exc()
            if tb and tb != "NoneType: None\n":
                summary = tb.splitlines()[-1] if tb.splitlines() else "No Traceback"
                embed.add_field(name="Trace Summary", value=f"```py\n{summary[:1000]}```", inline=False)

            embed.set_footer(text="Iron Sentinel System Active")
            
            await admin_channel.send(embed=embed)
        except Exception as dispatch_e:
            self.logger.error(f"Sentinel failed to dispatch report: {dispatch_e}")

    async def on_command_error(self, ctx, error):
        """Global handler for all command-related failures."""
        if isinstance(error, commands.CommandNotFound):
            return
        await self.dispatch_error(error, ctx=ctx)

    async def on_error(self, event_method, *args, **kwargs):
        """Global handler for all non-command event crashes."""
        await self.dispatch_error(Exception("Event Loop Crash"), event=event_method)

    # --- S-GRADE: ADMIN CONNECTIVITY DISPATCHERS ---

    async def handle_redis_lost(self, _):
        """Alerts the admin channel that Redis is offline and services are hibernating."""
        try:
            channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="🚨 Redis Connection Lost",
                    description="**Status:** System Hibernating 😴\n**Impact:** Task Queue & Listeners are on standby.\n\n*The bot will automatically resume once the connection is restored.*",
                    color=0xe67e22, # Orange (Warning)
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text="Iron Mask Protection Active")
                await channel.send(embed=embed)
        except Exception as e:
            self.logger.error(f"Failed to send redis_lost alert: {e}")

    async def handle_redis_connected(self, _):
        """Alerts the admin channel that Redis is back online."""
        try:
            channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="✨ Redis Connection Restored",
                    description="**Status:** System Online 🚀\n**Action:** Resuming all hibernated tasks and background listeners.",
                    color=0x2ecc71, # Green (Success)
                    timestamp=discord.utils.utcnow()
                )
                embed.set_footer(text="Stability Patch V2.1")
                await channel.send(embed=embed)
        except Exception as e:
            self.logger.error(f"Failed to send redis_connected alert: {e}")