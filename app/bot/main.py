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
            "app.bot.cogs.status",
            "app.bot.cogs.monitor_cog"
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

        # 4. Subscription Alerts
        EventBus.subscribe("subscription_added", self.handle_subscription_added)

        # 5. Waiter Alerts (Fan-out)
        EventBus.subscribe("notify_waiter", self.handle_waiter_notification)

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
            self.logger.info(f"Synced {len(synced)} slash commands.")
        except Exception as e:
            self.logger.error(f"Uh oh, command sync failed: {e}")

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
            # 🟢 S-GRADE: Extract Error Code if available
            error_code = getattr(error_message, "code", None) if not isinstance(error_message, str) else None
            err_text = str(error_message).split('\n')[0]
            
            # 1. Notify User Channel
            user_channel = self.get_channel(task.channel_id)
            
            embed = discord.Embed(
                title="Hmm... Error",
                description=f"<@1216284053049704600>, Check Pls",
                color=0xe74c3c
            )

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
                admin_embed.add_field(name="User ID", value=f"`{task.requester_id}`", inline=True)
                await admin_channel.send(embed=admin_embed)

        except Exception as e:
            self.logger.error(f"Failed to send extraction error alert: {e}")

    async def handle_subscription_added(self, group_name: str, sub: dict):
        """Dispatches a notification to the central logs when a new subscription is added."""
        try:
            channel_id = Settings.SUBSCRIPTION_LOG_CHANNEL_ID
            channel = self.get_channel(channel_id)
            if not channel:
                try: channel = await self.fetch_channel(channel_id)
                except: return self.logger.error(f"Failed to fetch subscription log channel: {channel_id}")

            title = sub.get("series_title", "Unknown")
            url = sub.get("series_url", "#")
            platform = sub.get("platform", "Unknown").capitalize()
            target_channel_id = sub.get("channel_id")
            user_id = sub.get("added_by")

            embed = discord.Embed(
                title="<a:done_subscription:1482425914456281108> New Subscription Added!",
                description=(
                    f"**Series:** [{title}]({url})\n"
                    f"**Platform:** `{platform}`\n"
                    f"**Group:** `{group_name}`\n"
                    f"**Dashboard Channel:** <#{target_channel_id}>\n"
                    f"**Added By:** <@{user_id}>"
                ),
                color=0x2ecc71,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Series ID: {sub.get('series_id')}")
            
            content = ""
            if "mecha" in sub.get("platform", "").lower():
                content = "<@&1488447662708625408>" # Ping role for Mecha series
                
            await channel.send(content=content, embed=embed)
            self.logger.info(f"📢 Notification sent: New subscription for '{title}' in {group_name}")
            
        except Exception as e:
            self.logger.error(f"Failed to send subscription notification: {e}")

        except Exception as e:
            self.logger.error(f"Failed to send extraction error alert: {e}")

    async def handle_waiter_notification(self, waiter: dict, task_dict: dict):
        """Notifies a secondary requester that their in-flight task is finished."""
        try:
            channel_id = waiter.get("channel_id")
            user_id = waiter.get("user_id")
            if not channel_id or not user_id: return

            channel = self.get_channel(channel_id)
            if not channel:
                try: channel = await self.fetch_channel(channel_id)
                except: return

            series_title = task_dict.get("series_title", "Unknown Series")
            title = task_dict.get("title", "Chapter")
            link = task_dict.get("share_link")

            msg = (
                f"<@{user_id}> ✅ **{series_title} — {title}** is ready!\n"
                f"🔗 {link}"
            )
            await channel.send(msg)
            self.logger.info(f"🔔 Notified waiter {user_id} in {channel_id} for {title}")
        except Exception as e:
            self.logger.error(f"Failed to notify waiter: {e}")

    # --- S-GRADE: GLOBAL CRASH SENTINEL ---
    async def dispatch_error(self, error: Exception, ctx: commands.Context = None, interaction: discord.Interaction = None, event: str = None, code: str = None):
        """Standardized crash reporter that dispatches reports to the admin legacy channel."""
        import traceback
        self.logger.error(f"Sentinel Dispatch [{event or 'Unknown'}]: {error}", exc_info=True)

        try:
            # 1. Resolve Admin Channel (Persistent)
            admin_channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if not admin_channel:
                try:
                    admin_channel = await self.fetch_channel(Settings.ADMIN_LOG_CHANNEL_ID)
                except:
                    self.logger.error("Sentinel failed to fetch admin channel!")
                    return

            # 🟢 S-GRADE: Extract Error Code
            error_code = code or getattr(error, "code", "SY_002") # Default to System Logic Crash if no code
            
            # 2. Build Report Embed
            title = "💥 Command Crash Detected" if (ctx or interaction) else "💀 Critical System Event Error"
            if event: title = f"💀 System Error: {event}"
            
            color = 0xc0392b if (ctx or interaction) else 0x000000
            
            embed = discord.Embed(
                title=title,
                description=f"Come <@1216284053049704600>. New Error",
                color=color,
                timestamp=discord.utils.utcnow()
            )
            
            embed.set_footer(text="Iron Sentinel System Active")
            
            await admin_channel.send(content="Come <@1216284053049704600>. New Error", embed=embed)
        except Exception as dispatch_e:
            self.logger.error(f"Sentinel failed to dispatch report: {dispatch_e}")

    async def on_command_error(self, ctx, error):
        """Global handler for all command-related failures."""
        if isinstance(error, commands.CommandNotFound):
            return
        await self.dispatch_error(error, ctx=ctx)

    async def on_error(self, event_method, *args, **kwargs):
        """Global handler for all non-command event crashes."""
        import sys
        _, exc, _ = sys.exc_info()
        await self.dispatch_error(exc or Exception("Event Loop Crash"), event=event_method)

    async def on_ready(self):
        self.logger.info(f"✅ Bot is ONLINE as {self.user} (ID: {self.user.id})")

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