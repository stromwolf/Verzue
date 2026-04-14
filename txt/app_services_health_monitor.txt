import asyncio
import logging
import time
from app.services.redis_manager import RedisManager
from app.services.session_service import SessionService

logger = logging.getLogger("HealthMonitor")

class HealthMonitor:
    """
    S-Grade Background Proactive Health Watchdog (Phase 5).
    Scanning fleet health and triggering preemptive healing.
    """
    def __init__(self, session_service: SessionService):
        self.redis = RedisManager()
        self.session_service = session_service
        self._running = False
        self.scan_interval = 600 # 10 minutes

    async def start(self):
        """Main monitoring loop."""
        if self._running: return
        self._running = True
        logger.info("🩺 HealthMonitor started. Scanning fleet for degradation...")
        
        while self._running:
            try:
                await self.scan_all_platforms()
            except Exception as e:
                logger.error(f"🩺 Monitor Error: {e}")
            
            await asyncio.sleep(self.scan_interval)

    async def scan_all_platforms(self):
        """Scans all platforms supported by the ProviderManager."""
        from app.providers.manager import ProviderManager
        platforms = ProviderManager().list_providers()
        
        for platform in platforms:
            await self.check_platform_health(platform)

    async def check_platform_health(self, platform: str):
        """Analyzes sessions for a specific platform and takes action."""
        account_ids = await self.redis.list_sessions(platform)
        total = len(account_ids)
        healthy = 0
        
        for aid in account_ids:
            session = await self.redis.get_session(platform, aid)
            if not session: continue
            
            # 1. Check TTL / Aging
            last_ritual = session.get("last_ritual_at", 0)
            status = session.get("status", "UNKNOWN")
            
            # Preemptive Ritual if session hasn't been active
            if status == "HEALTHY" and (time.time() - last_ritual > 43200): # 12 hours
                logger.info(f"🕯️ Session {platform}:{aid} is aging. Sending message to the Admin Server.")
                await self.redis.publish_event("verzue:events:session", "run_ritual", {
                    "platform": platform, "account_id": aid
                })

            # 2. Check Success Rate (SSR)
            metrics = await self.redis.get_metrics(platform)
            stats = metrics.get("stats", {})
            success = int(stats.get("success_count", 0))
            total_req = int(stats.get("total_requests", 0))
            
            ssr = (success / total_req * 100) if total_req > 10 else 100
            
            if status == "HEALTHY":
                if ssr < 30: # Danger Zone
                    logger.warning(f"⚠️ [Platform:{platform}] High Failure Rate ({ssr:.1f}%). Marking as AT_RISK.")
                    session["status"] = "AT_RISK"
                    await self.redis.set_session(platform, aid, session)
                else:
                    healthy += 1
        
        logger.info(f"📊 Platform Report [{platform.upper()}]: {healthy}/{total} Healthy | Fleet SSR: {ssr if 'ssr' in locals() else 'N/A'}%")

    def stop(self):
        self._running = False
