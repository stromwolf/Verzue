import logging
import asyncio
import json
import time
from app.services.redis_manager import RedisManager
from app.services.browser.driver import BrowserService
from app.providers.manager import ProviderManager

logger = logging.getLogger("SessionHealer")

class SessionHealer:
    def __init__(self, session_service):
        self.redis = RedisManager()
        self.session_service = session_service
        self.browser = BrowserService()
        self.provider_manager = ProviderManager()
        self._running = False

    async def start(self):
        """Starts the background event listener for session failures."""
        if self._running: return
        self._running = True
        logger.info("🏥 SessionHealer background listener started.")
        
        subscriber = self.redis.get_subscriber()
        if not subscriber:
            logger.error("❌ Redis client not available for SessionHealer.")
            return

        await subscriber.subscribe("verzue:events:session")
        
        # S-Grade Recovery: Check for already EXPIRED sessions on boot
        asyncio.create_task(self._heal_all_expired())
        
        try:
            while self._running:
                message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    payload = json.loads(message["data"])
                    event = payload.get("event")
                    data = payload.get("data")
                    
                    if event == "session_expired":
                        asyncio.create_task(self.heal_session(data["platform"], data["account_id"]))
                    elif event == "run_ritual":
                        asyncio.create_task(self.run_ritual_for_session(data["platform"], data["account_id"]))
                
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"🏥 SessionHealer Listener Error: {e}")
        finally:
            self._running = False

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
            if platform in ["jumptoon", "mecha"]:
                await self._refresh_via_token(platform, account_id)
            elif platform == "piccoma":
                await self._refresh_via_handshake(platform, account_id)
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
        Placeholder for Strategy A: Token-Level Self-Healing.
        In a real scenario, this would exchange the refreshToken for a new session.
        For now, we mark it as HEALTHY to allow the bot to retry.
        """
        logger.info(f"🔄 Attempting Token-Level refresh for {platform}:{account_id}...")
        
        session_obj = await self.redis.get_session(platform, account_id)
        if not session_obj: return

        # TODO: Implement actual token exchange logic for Jumptoon/Mecha Firebase tokens
        await asyncio.sleep(1) 
        
        # S-Grade Fallback: Reset status to HEALTHY so the bot can try again with existing cookies
        # This prevents sessions from being permanently 'dead' until manual intervention.
        await self.session_service.update_session_cookies(platform, account_id, session_obj.get("cookies", []))
        logger.info(f"✅ Session health reset for {platform}:{account_id} (Retry Mode)")

    async def _refresh_via_handshake(self, platform: str, account_id: str):
        """
        Strategy B: The 'Restorative Handshake' via Playwright.
        """
        logger.info(f"🎭 Attempting Browser Handshake refresh for {platform}:{account_id}...")
        
        session = await self.redis.get_session(platform, account_id)
        if not session: return

        # Target specific login/home URLs
        urls = {
            "piccoma": "https://piccoma.com/web/"
        }
        
        target_url = urls.get(platform)
        if not target_url: return

        # Piccoma selectors for verifying login state
        selectors = [".PCM-gnb_user"] # Check if user icon exists

        new_cookies, _ = await self.browser.run_isolated_handshake(
            target_url, session.get("cookies", []), selectors
        )
        
        if new_cookies:
            # Convert Playwright cookies to consistent format if needed
            # Actually run_isolated_handshake returns them as dicts
            await self.session_service.update_session_cookies(platform, account_id, new_cookies)
            logger.info(f"✨ Handshake refresh successful for {platform}:{account_id}")
        else:
            logger.error(f"💀 Handshake refresh FAILED for {platform}:{account_id}")
