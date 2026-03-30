import discord
from discord.ext import commands, tasks
import asyncio
import logging
import time
import json
from datetime import datetime
from app.services.session_service import SessionService
from app.services.redis_manager import RedisManager
from app.core.events import EventBus

logger = logging.getLogger("CookieStatus")

class CookieStatusCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session_service = SessionService()
        self.redis = RedisManager()
        
        # Performance: Use a lock to prevent concurrent UI updates
        self._ui_lock = asyncio.Lock()
        
        # Dashboard Config
        self.GUILD_ID = 1436068940584452109
        self.CHANNEL_ID = 1488185294392922242
        self.REDIS_MSG_KEY = "verzue:status:cookies_msg_id"
        
        # Start Heartbeat
        self.dashboard_loop.start()
        
        # Subscribe to Reactive Events
        EventBus.subscribe("session_status_changed", self.on_session_change)

    def cog_unload(self):
        self.dashboard_loop.cancel()

    async def on_session_change(self, platform: str):
        """Reactive trigger when a session is failed or updated."""
        logger.info(f"🔄 [StatusUI] Reactive update triggered by session change on: {platform}")
        # Small delay to ensure Redis is fully updated and to debounce rapid changes
        await asyncio.sleep(2)
        await self.update_dashboard()

    @tasks.loop(hours=1)
    async def dashboard_loop(self):
        """Periodic heartbeat update."""
        logger.info("🕒 [StatusUI] Hourly heartbeat update.")
        await self.update_dashboard()

    @dashboard_loop.before_loop
    async def before_dashboard_loop(self):
        await self.bot.wait_until_ready()

    async def update_dashboard(self):
        """Core logic to construct and send/edit the status message."""
        async with self._ui_lock:
            try:
                channel = self.bot.get_channel(self.CHANNEL_ID)
                if not channel:
                    # Fallback: try fetching (sometimes cache is stale on startup)
                    try:
                        channel = await self.bot.fetch_channel(self.CHANNEL_ID)
                    except:
                        logger.error(f"❌ [StatusUI] Could not find dashboard channel: {self.CHANNEL_ID}")
                        return

                # 1. Fetch Cookie Data
                platforms = ["piccoma", "mecha", "jumptoon"]
                statuses = []
                
                for idx, p_name in enumerate(platforms, 1):
                    session = await self.redis.get_session(p_name, "primary")
                    if not session:
                        # Try to find ANY session if primary doesn't exist
                        aids = await self.redis.list_sessions(p_name)
                        if aids:
                            session = await self.redis.get_session(p_name, aids[0])
                    
                    status_emoji = "🔴 Uh Oh"
                    expiry_text = "Has expired or no cookies found."
                    
                    if session and session.get("cookies"):
                        expiry_ts = self._get_earliest_expiry(session["cookies"])
                        if expiry_ts:
                            now = time.time()
                            days_left = (expiry_ts - now) / 86400
                            
                            dt_obj = datetime.fromtimestamp(expiry_ts)
                            # Formatting: "29th April"
                            day = dt_obj.day
                            suffix = 'th' if 11 <= day <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
                            date_str = dt_obj.strftime(f"{day}{suffix} %B")
                            
                            if days_left > 7:
                                status_emoji = "🟢 Good"
                                expiry_text = f"Will expire at {date_str}."
                            elif days_left > 0:
                                status_emoji = "🟠 Okay"
                                expiry_text = f"Will expire soon at {date_str}."
                            else:
                                status_emoji = "🟠 Uh Oh"
                                expiry_text = "Has expired."
                        
                        # Override if status is explicitly EXPIRED
                        if session.get("status") == "EXPIRED":
                            status_emoji = "🟠 Uh Oh"
                            expiry_text = "Session marked as Expired/Blocked."

                    statuses.append(f"{idx}. {p_name.capitalize()} - {status_emoji}\n- {expiry_text}")

                # 2. Build Message
                content = "# Verzue - Cookies Status\n"
                content += "------------------------------------\n"
                content += "### 📊Cookies Overview\n"
                content += "\n".join(statuses) + "\n"
                content += "------------------------------------\n"
                content += "### ⚙️Details\n"
                content += "Use `/add-cookies` to add new cookies using editthiscookies extension.\n\n"
                content += f"*Last Updated: <t:{int(time.time())}:R>*"

                # 3. Send or Edit
                msg_id = await self.redis.client.get(self.REDIS_MSG_KEY)
                message = None
                
                if msg_id:
                    try:
                        message = await channel.fetch_message(int(msg_id))
                    except (discord.NotFound, discord.HTTPException, ValueError):
                        message = None

                if message:
                    await message.edit(content=content)
                    logger.info(f"✅ [StatusUI] Dashboard updated (Msg: {msg_id})")
                else:
                    new_msg = await channel.send(content=content)
                    await self.redis.client.set(self.REDIS_MSG_KEY, str(new_msg.id))
                    logger.info(f"🆕 [StatusUI] Dashboard created (Msg: {new_msg.id})")

            except Exception as e:
                logger.error(f"❌ [StatusUI] Failed to update dashboard: {e}")

    def _get_earliest_expiry(self, cookies: list) -> float | None:
        """Parses cookies to find the earliest expiration timestamp."""
        expiries = []
        for c in cookies:
            exp = c.get("expiry") or c.get("expires")
            if exp:
                try:
                    val = float(exp)
                    if val > 10000000000: # Milliseconds fallback
                        val /= 1000
                    if val > 0:
                        expiries.append(val)
                except:
                    continue
        return min(expiries) if expiries else None

async def setup(bot):
    await bot.add_cog(CookieStatusCog(bot))
