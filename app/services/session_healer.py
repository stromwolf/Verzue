import logging
import asyncio
import json
import time
from app.services.redis_manager import RedisManager
from app.providers.manager import ProviderManager

logger = logging.getLogger("SessionHealer")

class SessionHealer:
    def __init__(self, session_service):
        self.redis = RedisManager()
        self.session_service = session_service
        self.provider_manager = ProviderManager()
        self._running = False

    async def start(self):
        """Starts the background event listener for session failures."""
        if self._running: return
        self._running = True
        logger.info("🏥 SessionHealer background listener started.")
        
        while self._running:
            try:
                subscriber = self.redis.get_subscriber()
                if not subscriber:
                    raise ConnectionError("Redis client not available.")

                await subscriber.subscribe("verzue:events:session")
                logger.info("🏥 [SessionHealer] Subscribed to session events.")
                
                # S-Grade Recovery: Check for already EXPIRED sessions upon each reconnection
                asyncio.create_task(self._heal_all_expired())
                
                while self._running:
                    message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message:
                        try:
                            payload = json.loads(message["data"])
                            event = payload.get("event")
                            data = payload.get("data")
                            
                            if event == "session_expired":
                                asyncio.create_task(self.heal_session(data["platform"], data["account_id"]))
                            elif event == "run_ritual":
                                asyncio.create_task(self.run_ritual_for_session(data["platform"], data["account_id"]))
                        except Exception as e:
                            logger.error(f"Error parsing session event: {e}")
                    
                    await asyncio.sleep(0.1)

            except (ConnectionError, TimeoutError) as e:
                logger.warning(f"🏥 [SessionHealer] Redis disconnected ({e}). Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"🏥 [SessionHealer] Unexpected error: {e}")
                await asyncio.sleep(5)
            finally:
                pass

        logger.info("🏥 SessionHealer background listener stopped.")

    async def _heal_all_expired(self):
        """Scans all platforms for EXPIRED sessions and triggers a heal."""
        platforms = ["mecha", "jumptoon", "piccoma", "kakao", "kuaikan", "acqq"]
        for p in platforms:
            aids = await self.redis.list_sessions(p)
            for aid in aids:
                session = await self.redis.get_session(p, aid)
                if session and session.get("status") == "EXPIRED":
                    logger.info(f"🏥 Startup Recovery: Queuing heal for {p}:{aid}")
                    asyncio.create_task(self.heal_session(p, aid))
    
    async def stop(self):
        self._running = False

    async def heal_session(self, platform: str, account_id: str):
        """Dispatches to the correct healing strategy based on the platform."""
        logger.info(f"💉 Healing session: {platform}:{account_id}")
        
        try:
            if platform in ["mecha"]:
                await self._refresh_via_token(platform, account_id)
            else:
                logger.warning(f"🤷 No healing strategy for platform: {platform}")
            
            # Post-Heal Ritual (Phase 7 Optimization)
            await self.run_ritual_for_session(platform, account_id)
            
        except Exception as e:
            logger.error(f"❌ Failed to heal {platform}:{account_id}: {e}")

    async def run_ritual_for_session(self, platform: str, account_id: str):
        """Executes the S-Grade behavioral ritual for a given session."""
        provider = self.provider_manager.get_provider(platform)
        if not provider: return

        session_obj = await self.redis.get_session(platform, account_id)
        if not session_obj: return

        # Use an AsyncSession to run the ritual
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome120") as session:
            # Inject cookies
            for c in session_obj.get("cookies", []):
                session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''))
            
            logger.info(f"🎭 Running S-Grade Ritual for {platform}:{account_id}...")
            await provider.run_ritual(session)
            
            # Update 'last_ritual_at' in Redis
            session_obj["last_ritual_at"] = time.time()
            await self.redis.set_session(platform, account_id, session_obj)

    async def _refresh_via_token(self, platform: str, account_id: str):
        """
        Placeholder for Strategy A: Token-Level Self-Healing (e.g. Firebase).
        """
        logger.info(f"🔄 Attempting Token-Level refresh for {platform}:{account_id}...")
        
        session_obj = await self.redis.get_session(platform, account_id)
        if not session_obj: return

        # Throttle check: Don't auto-reset more than once every 10 minutes
        now = time.time()
        last_attempt = session_obj.get("last_refresh_attempt", 0)
        if now - last_attempt < 600:
            logger.warning(f"⏳ Refresh throttled for {platform}:{account_id} (Wait {int(600 - (now - last_attempt))}s)")
            return

        # S-GRADE: Placeholder update - only reset to HEALTHY if we actually did something.
        # For now, this is a NO-OP.
        logger.warning(f"⚠️ Token-level refresh not implemented for {platform}. Leaving as EXPIRED.")
        session_obj["last_refresh_attempt"] = now
        await self.redis.set_session(platform, account_id, session_obj)

        await self.redis.set_session(platform, account_id, session_obj)
