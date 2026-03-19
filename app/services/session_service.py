import logging
import random
from app.services.redis_manager import RedisManager

logger = logging.getLogger("SessionService")

class SessionService:
    def __init__(self):
        self.redis = RedisManager()

    async def get_active_session(self, platform: str):
        """
        Retrieves a healthy session for the given platform.
        Implements simple random rotation among healthy sessions.
        """
        account_ids = await self.redis.list_sessions(platform)
        if not account_ids:
            logger.warning(f"⚠️ No sessions found for platform: {platform}")
            return None

        # Filter for healthy sessions (Phase 2 simple check)
        healthy_sessions = []
        for aid in account_ids:
            session = await self.redis.get_session(platform, aid)
            if session and session.get("status") == "HEALTHY":
                healthy_sessions.append(session)

        if not healthy_sessions:
            logger.error(f"❌ No HEALTHY sessions available for {platform}!")
            return None

        # Random rotation
        chosen = random.choice(healthy_sessions)
        logger.debug(f"🔄 Selected session '{chosen['account_id']}' for {platform}")
        return chosen

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
