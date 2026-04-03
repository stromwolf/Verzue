import logging
import random
import time
import asyncio
from app.services.redis_manager import RedisManager
from app.core.events import EventBus

logger = logging.getLogger("SessionService")

class SessionService:
    # S-GRADE: Global Async Locks to prevent concurrent auto-login attempts
    _refresh_locks: dict[str, asyncio.Lock] = {}

    def __init__(self):
        self.redis = RedisManager()
        self._last_emit = {} # platform -> timestamp

    def get_refresh_lock(self, platform: str) -> asyncio.Lock:
        """Returns or creates an asyncio.Lock for the specific platform."""
        if platform not in self._refresh_locks:
            self._refresh_locks[platform] = asyncio.Lock()
        return self._refresh_locks[platform]

    async def get_active_session(self, platform: str):
        """
        Retrieves a healthy session for the given platform.
        Implements simple random rotation among healthy sessions.
        """
        account_ids = await self.redis.list_sessions(platform)
        if not account_ids:
            logger.warning(f"⚠️ No sessions found for platform: {platform}")
            return None

        # S-Grade: Batch retrieval to avoid O(N) database latency
        sessions = await self.redis.get_sessions_batch(platform, account_ids)
        healthy_sessions = [s for s in sessions if s and s.get("status") == "HEALTHY"]

        if not healthy_sessions:
            logger.error(f"❌ No HEALTHY sessions available for {platform}!")
            return None

        # Random rotation
        chosen = random.choice(healthy_sessions)
        logger.debug(f"🔄 Selected session '{chosen['account_id']}' for {platform}")
        return chosen

    async def _emit_status_change(self, platform: str):
        """Emits session status change with a small debounce to protect Discord API."""
        now = time.time()
        last = self._last_emit.get(platform, 0)
        
        # 5 second debounce per platform
        if now - last < 5:
            return
            
        self._last_emit[platform] = now
        await EventBus.emit("session_status_changed", platform)

    async def report_session_failure(self, platform: str, account_id: str, reason: str = "Unknown"):
        """
        Marks a session as EXPIRED and records failure telemetry.
        Transitions the platform to a 'High Risk' state if failure rate is high.
        """
        session = await self.redis.get_session(platform, account_id)
        if not session:
            return

        logger.warning(f"🚨 Session Failure Reported: {platform}:{account_id} | Reason: {reason}")
        session["status"] = "EXPIRED"
        session["error_reason"] = reason
        await self.redis.set_session(platform, account_id, session)

        # Telemetry: Identify WAF vs Auth
        error_type = "WAF_BLOCK" if any(x in reason.lower() for x in ["403", "cloudflare", "captcha", "forbidden"]) else "AUTH_EXPIRED"
        await self.redis.record_request(platform, success=False, error_type=error_type)

        # Trigger refresh event (Phase 3)
        await self.redis.publish_event("verzue:events:session", "session_expired", {
            "platform": platform,
            "account_id": account_id
        })
        await self._emit_status_change(platform)

    async def record_session_success(self, platform: str):
        """Records a successful request for telemetry tracking."""
        await self.redis.record_request(platform, success=True)

    async def update_session_cookies(self, platform: str, account_id: str, cookies: list):
        """
        Updates a session's cookies and resets status to HEALTHY.
        """
        session = await self.redis.get_session(platform, account_id)
        if not session:
            # Create a basic session object if it doesn't exist
            session = {
                "account_id": account_id,
                "platform": platform
            }

        session["cookies"] = cookies
        session["status"] = "HEALTHY"
        session.pop("error_reason", None)
        await self.redis.set_session(platform, account_id, session)
        logger.info(f"✅ Session updated and refreshed: {platform}:{account_id}")
        await self._emit_status_change(platform)
