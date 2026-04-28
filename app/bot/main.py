import discord
import os
import asyncio
from discord.ext import commands
import logging
from app.core.events import EventBus
from config.settings import Settings
from app.services.settings_service import SettingsService

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
        # 🟢 S-GRADE: Bind bot to worker for app_state/notification access
        if self.task_queue and hasattr(self.task_queue, "worker"):
            self.task_queue.worker.bot = self
        
        self.redis_brain = redis_brain
        self.logger = logging.getLogger("Bot")

    async def setup_hook(self):
        """Loads extensions and subscribes to events."""
        # Determine identity
        if self.token_str == Settings.ADMIN_BOT_TOKEN:
            self.identity = "Admin"
        elif self.token_str == Settings.TESTING_BOT_TOKEN:
            self.identity = "Testing"
        else:
            self.identity = "Main"

        self.logger.info(f"🤖 Identity resolved: {self.identity}")

        # Cog routing optimization (Identity-based filtering)
        all_extensions = [
            ("app.bot.cogs.dashboard", ["Main", "Testing"]),
            ("app.bot.cogs.subscriptions", ["Main", "Admin", "Testing"]),
            ("app.bot.cogs.status", ["Main"]),
            ("app.bot.cogs.admin", ["Admin"]),
            ("app.bot.cogs.discovery", ["Admin"]),
            ("app.bot.cogs.discovery_commands", ["Admin"]),
            ("app.bot.cogs.monitor_cog", ["Admin"]),
            ("app.bot.cogs.helper_cogs", ["Admin"]),
        ]

        for ext, allowed_identities in all_extensions:
            if self.identity in allowed_identities:
                try:
                    await self.load_extension(ext)
                    self.logger.info(f"🧩 Loaded: {ext} [{self.identity}]")
                except Exception as e:
                    self.logger.error(f"❌ Failed to load {ext}: {e}")

        # --- EVENT SUBSCRIPTIONS ---
        if self.identity == "Main":
            EventBus.subscribe("upload_zip_to_discord", self.handle_zip_upload)
            EventBus.subscribe("send_direct_link", self.handle_direct_link)
            EventBus.subscribe("task_failed", self.handle_task_failure)
            EventBus.subscribe("subscription_added", self.handle_subscription_added)
            EventBus.subscribe("notify_waiter", self.handle_waiter_notification)
            EventBus.subscribe("task_completed", self.handle_task_completed)
            # Redis connectivity alerts
            EventBus.subscribe("redis_lost", self.handle_redis_lost)
            EventBus.subscribe("redis_connected", self.handle_redis_connected)

        # 🟢 FIX: Initialize Poller EARLY — before worker/boot calls that might fail
        from app.tasks.poller import AutoDownloadPoller
        self.auto_poller = AutoDownloadPoller(self)

        # Initialize core services for all identities to ensure command robustness
        from app.services.ui_manager import UIManager
        await UIManager().start()

        await self.task_queue.boot()
        asyncio.create_task(self.task_queue.start_worker(num_workers=2))

        # Sync Slash Commands
        try:
            synced = await self.tree.sync()
            self.logger.info(f"⚡ Synced {len(synced)} slash commands [{self.identity}].")
        except Exception as e:
            self.logger.error(f"❌ Sync failed [{self.identity}]: {e}")

    async def start_bot(self):
        """Custom start method to handle login."""
        await self.start(self.token_str)

    async def close(self):
        """Graceful shutdown hook."""
        if hasattr(self, 'task_queue'):
            await self.task_queue.shutdown()  # 🟢 NEW: drains & deregisters
        await super().close()

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

    async def handle_task_completed(self, task_data):
        """Notifies the original requester when a chapter is ready."""
        try:
            # Handle both object and dict
            if hasattr(task_data, "to_dict"):
                task_dict = task_data.to_dict()
            else:
                task_dict = task_data

            # 🟢 Dashboard/subscription handle their own UI — no plain msg needed
            source = task_dict.get("source", "standalone")
            if source in ("dashboard", "subscription"):
                return

            requester_id = task_dict.get("requester_id")
            channel_id = task_dict.get("channel_id")
            if not requester_id or not channel_id: return

            channel = self.get_channel(channel_id)
            if not channel:
                try: channel = await self.fetch_channel(channel_id)
                except: return

            series_title = task_dict.get("series_title", "Unknown Series")
            title = task_dict.get("title", "Chapter")
            link = task_dict.get("share_link")

            # 🟢 S-GRADE: Use SettingsService for mentions
            settings = SettingsService()
            targets = await settings.get_notify_targets(requester_id)
            mentions = SettingsService.format_mentions(targets) or f"<@{requester_id}>"

            msg = (
                f"{mentions} ✅ **{series_title} — {title}** is ready!\n"
                f"🔗 {link}"
            )
            await channel.send(msg)
            self.logger.info(f"🔔 Notified requester {requester_id} in {channel_id} for {title}")
        except Exception as e:
            self.logger.error(f"Failed to notify requester: {e}")

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

            # 🟢 S-GRADE: Use SettingsService for mentions
            settings = SettingsService()
            targets = await settings.get_notify_targets(user_id)
            mentions = SettingsService.format_mentions(targets) or f"<@{user_id}>"

            msg = (
                f"{mentions} ✅ **{series_title} — {title}** is ready!\n"
                f"🔗 {link}"
            )
            await channel.send(msg)
            self.logger.info(f"🔔 Notified waiter {user_id} in {channel_id} for {title}")
        except Exception as e:
            self.logger.error(f"Failed to notify waiter: {e}")

    # --- S-GRADE: GLOBAL CRASH SENTINEL ---
    async def dispatch_error(self, error: Exception, ctx: commands.Context = None, 
                              interaction: discord.Interaction = None, 
                              event: str = None, code: str = None):
        """Standardized crash reporter with full diagnostic context."""
        import traceback
        self.logger.error(f"Sentinel Dispatch [{event or 'Unknown'}]: {error}", exc_info=True)

        try:
            admin_channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if not admin_channel:
                try:
                    admin_channel = await self.fetch_channel(Settings.ADMIN_LOG_CHANNEL_ID)
                except:
                    self.logger.error("Sentinel failed to fetch admin channel!")
                    return

            # ─── Classify the error ────────────────────────────────────────────
            error_type = type(error).__name__
            error_code = code or getattr(error, "code", None)
            error_msg = str(error).splitlines()[0] if str(error) else "No message"
            full_tb = traceback.format_exc()
            # Trim traceback to last 900 chars to fit Discord embed
            tb_snippet = full_tb[-900:] if len(full_tb) > 900 else full_tb
            if tb_snippet == "NoneType: None\n":
                tb_snippet = f"{error_type}: {error_msg}"

            # ─── Resolve source context ────────────────────────────────────────
            source_lines = []
            if interaction:
                source_lines.append(f"**User:** {interaction.user} (`{interaction.user.id}`)")
                source_lines.append(f"**Channel:** <#{interaction.channel_id}>")
                if hasattr(interaction, 'data') and interaction.data:
                    cmd = interaction.data.get('name') or interaction.data.get('custom_id', '')
                    if cmd:
                        source_lines.append(f"**Command/Action:** `{cmd}`")
            if ctx:
                source_lines.append(f"**User:** {ctx.author} (`{ctx.author.id}`)")
                source_lines.append(f"**Channel:** <#{ctx.channel.id}>")
                source_lines.append(f"**Message:** `{ctx.message.content[:100]}`")
            if event:
                source_lines.append(f"**Event:** `{event}`")
            if error_code:
                source_lines.append(f"**Error Code:** `{error_code}`")

            # ─── Build embed ───────────────────────────────────────────────────
            # Color: red for user-triggered, black for system
            color = 0xe74c3c if (ctx or interaction) else 0x2c2f33

            embed = discord.Embed(
                title=f"💥 `{error_type}` — {error_msg[:100]}",
                color=color,
                timestamp=discord.utils.utcnow()
            )

            if source_lines:
                embed.add_field(
                    name="📍 Source",
                    value="\n".join(source_lines),
                    inline=False
                )

            embed.add_field(
                name="🔍 Traceback",
                value=f"```python\n{tb_snippet}\n```",
                inline=False
            )

            embed.set_footer(text=f"Iron Sentinel  •  Bot: {self.user}  •  Identity: {self.identity}")

            await admin_channel.send(
                content=f"<@1216284053049704600> 🚨 New error in **{self.identity}** bot",
                embed=embed
            )

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
        
        # 🟢 GDrive Health Check
        if self.identity == "Main":
            if hasattr(self.task_queue, "uploader") and self.task_queue.uploader.is_disabled:
                self.logger.critical("🚨 GDrive is DISABLED (Auth Failed). Sending alert...")
                await self.dispatch_gdrive_alert()

    async def dispatch_gdrive_alert(self):
        """Sends a critical alert to the admin channel about GDrive failure."""
        try:
            channel = self.get_channel(Settings.ADMIN_LOG_CHANNEL_ID)
            if not channel:
                try: channel = await self.fetch_channel(Settings.ADMIN_LOG_CHANNEL_ID)
                except: return
                
            embed = discord.Embed(
                title="🚨 GDrive Authentication Failed",
                description=(
                    "The bot is running in **Degraded Mode**.\n\n"
                    "**Reason:** No valid GDrive token found and interactive login is disabled on VPS.\n"
                    "**Action Required:** Run `generate_token.py` locally, update the vault, and restart."
                ),
                color=0xe74c3c,
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text="Iron Mask Security Sentinel")
            await channel.send(content="<@1216284053049704600> 🚨 **Critical: GDrive Offline**", embed=embed)
        except Exception as e:
            self.logger.error(f"Failed to send GDrive alert: {e}")

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