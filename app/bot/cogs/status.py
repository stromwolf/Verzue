import discord
from discord.ext import commands, tasks
import asyncio
import logging
import time
import json
from datetime import datetime, time as datetime_time, timezone, timedelta
from app.services.session_service import SessionService
from app.services.redis_manager import RedisManager
from app.core.events import EventBus

logger = logging.getLogger("CookieStatus")

# Define IST (UTC+5:30) for the daily 8:00 PM ping
IST = timezone(timedelta(hours=5, minutes=30))
DAILY_PING_TIME = datetime_time(hour=20, minute=0, tzinfo=IST)

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
        self.PING_ROLE_ID = 1488447662708625408
        
        # Start loops
        self.dashboard_loop.start()
        self.daily_ping_loop.start()
        
        # ─── Only subscribe ONCE — use a class-level guard ─────────────────────
        if not getattr(CookieStatusCog, "_event_bound", False):
            EventBus.subscribe("session_status_changed", self.on_session_change)
            CookieStatusCog._event_bound = True
        else:
            logger.info("[StatusUI] EventBus already bound (another instance); skipping subscribe.")

    def cog_unload(self):
        self.dashboard_loop.cancel()
        self.daily_ping_loop.cancel()

    async def on_session_change(self, platform: str):
        """Reactive trigger when a session is failed or updated."""
        logger.info(f"🔄 [StatusUI] Reactive update triggered by session change on: {platform}")
        # Small delay to ensure Redis is fully updated and to debounce rapid changes
        await asyncio.sleep(1)
        await self.update_dashboard()

    @tasks.loop(hours=1)
    async def dashboard_loop(self):
        """Periodic heartbeat update."""
        logger.info("🕒 [StatusUI] Hourly heartbeat update.")
        await self.update_dashboard()

    @dashboard_loop.before_loop
    async def before_dashboard_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=DAILY_PING_TIME)
    async def daily_ping_loop(self):
        """Daily 8:00 PM reminder to check cookie health."""
        logger.info(f"🔔 [StatusUI] Daily cookie health check ping triggered at 20:00 IST.")
        try:
            channel = self.bot.get_channel(self.CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(self.CHANNEL_ID)
            
            if channel:
                message = f"🔔 <@&{self.PING_ROLE_ID}> — **Daily Cookie Health Check!**\nPlease check the cookies if they're available or not."
                await channel.send(content=message)
                logger.info("✅ [StatusUI] Daily ping sent successfully.")
            else:
                logger.error(f"❌ [StatusUI] Could not find channel {self.CHANNEL_ID} for daily ping.")
        except Exception as e:
            logger.error(f"❌ [StatusUI] Failed to send daily ping: {e}")

    # Re-wrap the loop with the correct time parameter
    @daily_ping_loop.error
    async def on_daily_ping_error(self, error):
        logger.error(f"❌ [StatusUI] Daily ping loop error: {error}")

    @daily_ping_loop.before_loop
    async def before_daily_ping_loop(self):
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
                    # Prioritize finding a HEALTHY session first
                    aids = await self.redis.list_sessions(p_name)
                    session = None
                    
                    if aids:
                        # Check primary first
                        if "primary" in aids:
                            session = await self.redis.get_session(p_name, "primary")
                        
                        # If primary is not healthy or not found, look for any healthy one
                        if not session or session.get("status") != "HEALTHY":
                            for aid in aids:
                                if aid == "primary": continue
                                s = await self.redis.get_session(p_name, aid)
                                if s and s.get("status") == "HEALTHY":
                                    session = s
                                    break
                        
                        # Fallback to just the first one if we still don't have a session
                        if not session:
                            session = await self.redis.get_session(p_name, aids[0])
                    
                    status_emoji = "🔴 Uh Oh"
                    details_text = "> Status: No active session found."
                    
                    if session and session.get("cookies"):
                        last_refreshed = session.get("last_ritual_at") or session.get("updated_at") or time.time()
                        expiry_ts = self._get_earliest_expiry(session["cookies"])
                        
                        if expiry_ts:
                            now = time.time()
                            days_left = (expiry_ts - now) / 86400
                            status_emoji = "🟢 Good" if days_left > 7 else ("🟠 Okay" if days_left > 0 else "🟠 Uh Oh")
                            details_text = f"> Last Renewed: <t:{int(last_refreshed)}:F>\n> Expiry: <t:{int(expiry_ts)}:F>"
                        else:
                            # 🟢 S-GRADE: Handle session with cookies but NO expiry (session cookies)
                            status_emoji = "🟢 Good"
                            # For session cookies, we show a 'Service-level' expiry (usually 24h for the next check)
                            projected_expiry = last_refreshed + 86400 
                            details_text = f"> Last Renewed: <t:{int(last_refreshed)}:F>\n> Expiry: <t:{int(projected_expiry)}:F>"
                        
                        # Override if status is explicitly EXPIRED
                        if session.get("status") == "EXPIRED":
                            status_emoji = "🟠 Uh Oh"
                            details_text = f"> Status: Expired/Blocked\n> Last Action: <t:{int(last_refreshed)}:R>"
                    
                    statuses.append(f"{idx}. {p_name.capitalize()} - {status_emoji}\n{details_text}")

                # 2. Build V2 Component Payload
                inner = []
                
                # Header
                inner.append({"type": 10, "content": "# Verzue — Cookies Status"})
                inner.append({"type": 14, "divider": True, "spacing": 1})
                
                # Overview Title
                inner.append({"type": 10, "content": "### 📊 Cookies Overview"})
                
                # Platform List
                for s_text in statuses:
                    inner.append({"type": 10, "content": s_text})
                    inner.append({"type": 14, "divider": True, "spacing": 1})
                
                # Details & Instructions
                inner.append({"type": 10, "content": "### ⚙️ Details\nUse `/add-cookies` to add new cookies using **EditThisCookie** extension."})
                inner.append({"type": 14, "divider": True, "spacing": 1})
                
                # Footer with timestamp
                inner.append({"type": 10, "content": f"-# Last Updated: <t:{int(time.time())}:R>"})

                # Wrap in Container (Type 17)
                payload = {
                    "flags": 32768,
                    "components": [{
                        "type": 17,
                        "accent_color": 0x5865f2,
                        "components": inner
                    }]
                }

                # 3. Send or Edit via Raw HTTP (Required for V2 Components)
                msg_id_raw = await self.redis.client.get(self.REDIS_MSG_KEY)
                msg_id = None
                
                if msg_id_raw:
                    msg_id = msg_id_raw.decode() if isinstance(msg_id_raw, bytes) else str(msg_id_raw)
                else:
                    # 🟢 S-GRADE: Re-attachment logic (Search history if Redis ID is missing)
                    msg = await self._find_existing_dashboard(channel)
                    if msg:
                        msg_id = str(msg.id)
                        await self.redis.client.set(self.REDIS_MSG_KEY, msg_id)
                        logger.info(f"🔗 [StatusUI] Re-attached to existing dashboard in history: {msg_id}")

                if msg_id:
                    try:
                        # Attempt to edit existing
                        route = discord.http.Route(
                            'PATCH',
                            '/channels/{channel_id}/messages/{message_id}',
                            channel_id=self.CHANNEL_ID,
                            message_id=int(msg_id)
                        )
                        await self.bot.http.request(route, json=payload)
                        logger.info(f"✅ [StatusUI] Dashboard V2 updated (Msg: {msg_id})")
                        return
                    except (discord.NotFound, discord.HTTPException) as e:
                        logger.warning(f"⚠️ [StatusUI] Message {msg_id} failed to update ({e}). Attempting fallback search...")
                        # One more try: search history if the Redis ID was stale
                        msg = await self._find_existing_dashboard(channel)
                        if msg and str(msg.id) != str(msg_id):
                            msg_id = str(msg.id)
                            await self.redis.client.set(self.REDIS_MSG_KEY, msg_id)
                            logger.info(f"🔗 [StatusUI] Re-attached to NEW message found in history: {msg_id}")
                            # Recursive call to try editing the new ID (guarded by lock)
                            route = discord.http.Route(
                                'PATCH',
                                '/channels/{channel_id}/messages/{message_id}',
                                channel_id=self.CHANNEL_ID,
                                message_id=int(msg_id)
                            )
                            try:
                                await self.bot.http.request(route, json=payload)
                                logger.info(f"✅ [StatusUI] Dashboard V2 updated via re-attached ID (Msg: {msg_id})")
                                return
                            except: pass
                        
                        logger.warning(f"⚠️ [StatusUI] No valid dashboard found, creating new one.")

                # Create new message if none exists or fetch failed
                route = discord.http.Route(
                    'POST',
                    '/channels/{channel_id}/messages',
                    channel_id=self.CHANNEL_ID
                )
                response = await self.bot.http.request(route, json=payload)
                new_id = response.get("id")
                if new_id:
                    await self.redis.client.set(self.REDIS_MSG_KEY, str(new_id))
                    logger.info(f"🆕 [StatusUI] Dashboard V2 created (Msg: {new_id})")

            except Exception as e:
                logger.error(f"❌ [StatusUI] Failed to update dashboard: {e}")

    async def _find_existing_dashboard(self, channel):
        """Searches history for an existing Cookies Status dashboard."""
        try:
            # Dedicated channel usually, so we search 20 messages (more than enough)
            async for message in channel.history(limit=20):
                if message.author.id == self.bot.user.id:
                    # V2 Components (Type 17) often don't have 'content' or 'embeds' visible to legacy parsers
                    # But if we found ANY message from this bot, it's almost certainly the dashboard
                    return message
        except Exception as e:
            logger.error(f"🔍 [StatusUI] History search failed: {e}")
        return None

    def _get_earliest_expiry(self, cookies: list) -> float | None:
        """Parses cookies to find the earliest expiration timestamp."""
        expiries = []
        for c in cookies:
            # EditThisCookie uses expirationDate. Others use expiry or expires.
            exp = c.get("expirationDate") or c.get("expiry") or c.get("expires")
            if exp:
                try:
                    val = float(exp)
                    if val > 10000000000: # Milliseconds fallback
                        val /= 1000
                    if val > 1000000000: # Sanity check for Unix timestamp
                        expiries.append(val)
                except:
                    continue
        
        if not expiries:
            logger.debug(f"[StatusUI] No expiration dates found in {len(cookies)} cookies.")
            return None
            
        earliest = min(expiries)
        logger.debug(f"[StatusUI] Earliest expiry found: {datetime.fromtimestamp(earliest)} ({earliest})")
        return earliest

async def setup(bot):
    await bot.add_cog(CookieStatusCog(bot))
