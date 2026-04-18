import logging
import random
import time
import asyncio
import json
import os
import tempfile
import glob
from app.services.redis_manager import RedisManager
from app.core.events import EventBus

logger = logging.getLogger("SessionService")

class SessionService:
    _instance = None
    # S-GRADE: Global Async Locks to prevent concurrent auto-login attempts
    _refresh_locks: dict[str, asyncio.Lock] = {}

    def __new__(cls):
        # ─── Enforce singleton pattern ─────────────────────────────────────
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.redis = RedisManager()
        self._last_emit = {} # platform -> timestamp
        self._initialized = True

    def get_refresh_lock(self, platform: str, account_id: str = "primary") -> asyncio.Lock:
        """Returns or creates an asyncio.Lock for the specific (platform, account) pair."""
        lock_key = f"{platform}:{account_id}"
        if lock_key not in self._refresh_locks:
            self._refresh_locks[lock_key] = asyncio.Lock()
        return self._refresh_locks[lock_key]

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
        Write-through: persists to disk so sessions survive cold Redis.
        """
        session = await self.redis.get_session(platform, account_id)
        if not session:
            session = {
                "account_id": account_id,
                "platform": platform,
            }

        session["cookies"] = cookies
        session["status"] = "HEALTHY"
        session["updated_at"] = time.time()
        session.pop("error_reason", None)
        await self.redis.set_session(platform, account_id, session)
        logger.info(f"✅ Session updated and refreshed: {platform}:{account_id}")

        # ── Write-through to disk (atomic) ──────────────────────────────
        try:
            disk_dir = os.path.join(os.getcwd(), "data", "secrets", platform)
            os.makedirs(disk_dir, exist_ok=True)
            disk_path = os.path.join(disk_dir, f"session_{account_id}.json")

            # Atomic write: tmp → rename prevents partial reads on crash
            fd, tmp_path = tempfile.mkstemp(dir=disk_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(session, f, indent=2)
                os.replace(tmp_path, disk_path)  # atomic on POSIX
            except Exception:
                # Clean up temp file if rename failed
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            logger.debug(f"💾 Session write-through to disk: {disk_path}")
        except Exception as e:
            # Disk failure is non-fatal — Redis is the primary store
            logger.warning(f"⚠️ Disk write-through failed for {platform}:{account_id}: {e}")

        await self._emit_status_change(platform)

    async def seed_from_disk(self):
        """
        Boot-time recovery: if Redis has no sessions for a platform, try to
        load the last-known-good session from disk.

        Call once during startup (in TaskQueue.boot or main.py) BEFORE any
        tasks or healers run.
        """
        base = os.path.join(os.getcwd(), "data", "secrets")
        if not os.path.isdir(base):
            return

        platforms_seeded = []

        for platform_dir in os.listdir(base):
            platform_path = os.path.join(base, platform_dir)
            if not os.path.isdir(platform_path):
                continue

            # Check if Redis already has sessions for this platform
            existing = await self.redis.list_sessions(platform_dir)
            if existing:
                continue  # Redis is warm — nothing to do

            # Look for session_*.json files (new format) or cookies.json (legacy)
            session_files = glob.glob(os.path.join(platform_path, "session_*.json"))
            legacy_file = os.path.join(platform_path, "cookies.json")

            loaded = False

            for sf in session_files:
                try:
                    with open(sf, "r") as f:
                        session_data = json.load(f)

                    account_id = session_data.get("account_id", "primary")
                    cookies = session_data.get("cookies", [])

                    if not cookies:
                        continue

                    # Don't blindly trust the status from disk — mark as HEALTHY
                    # and let the first actual task validate via shallow check.
                    session_data["status"] = "HEALTHY"
                    session_data["seeded_from_disk"] = True
                    await self.redis.set_session(platform_dir, account_id, session_data)
                    logger.info(
                        f"🌱 Seeded {platform_dir}:{account_id} from disk "
                        f"({len(cookies)} cookies)"
                    )
                    loaded = True
                except Exception as e:
                    logger.warning(f"⚠️ Failed to seed from {sf}: {e}")

            # Legacy fallback: old cookies.json (flat cookie list, no session wrapper)
            if not loaded and os.path.exists(legacy_file):
                try:
                    with open(legacy_file, "r") as f:
                        cookies = json.load(f)

                    if isinstance(cookies, list) and cookies:
                        session_data = {
                            "account_id": "primary",
                            "platform": platform_dir,
                            "cookies": cookies,
                            "status": "HEALTHY",
                            "seeded_from_disk": True,
                        }
                        await self.redis.set_session(platform_dir, "primary", session_data)
                        logger.info(
                            f"🌱 Seeded {platform_dir}:primary from legacy cookies.json "
                            f"({len(cookies)} cookies)"
                        )
                        loaded = True
                except Exception as e:
                    logger.warning(f"⚠️ Failed to seed from legacy {legacy_file}: {e}")

            if loaded:
                platforms_seeded.append(platform_dir)

        if platforms_seeded:
            logger.info(f"🌱 Disk seed complete. Platforms recovered: {', '.join(platforms_seeded)}")
        else:
            logger.debug("🌱 Disk seed: all platforms already warm in Redis (or no disk backups found).")

    async def get_authenticated_session(
        self,
        provider: str,
        account_id: str | None = None,
        force_refresh: bool = False,
        timeout: int = 60
    ) -> dict | None:
        """
        Get an authenticated session for the provider.
        
        Args:
            provider: Platform identifier (e.g. 'piccoma')
            account_id: Specific account to retrieve. If None, resolves from active session.
            force_refresh: If True, mark current session expired and trigger 
                           a synchronous heal, awaiting the result.
            timeout: Max seconds to wait for healing.
        """
        # Resolve which account to use
        if account_id is None:
            active = await self.get_active_session(provider)
            account_id = active.get("account_id") if active else "primary"
        
        if force_refresh:
            await self._mark_session_expired(provider, account_id)
            
            logger.info(f"💉 [Forced Refresh] Healing {provider}:{account_id} synchronously...")
            try:
                healed = await asyncio.wait_for(
                    self._heal_session_now(provider, account_id),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.error(f"❌ Forced refresh TIMEOUT after {timeout}s for {provider}:{account_id}")
                return None
            
            if not healed:
                return None
            
            # Return the freshly healed session
            return await self.redis.get_session(provider, account_id)
        
        # Normal path - get existing and trigger background heal if needed (handled by caller or healer)
        return await self.redis.get_session(provider, account_id)

    async def _heal_session_now(self, provider: str, account_id: str = "primary") -> bool:
        """Synchronous healing — uses the canonical refresh lock for the (provider, account) pair."""
        lock = self.get_refresh_lock(provider, account_id)
        
        # S-Grade: Try-lock pattern to prevent thundering herd
        if lock.locked():
            logger.info(
                f"💉 [Heal] Refresh already in progress for {provider}:{account_id}. "
                f"Awaiting existing heal instead of starting a new one."
            )
            async with lock:  # Will block until in-flight heal releases
                pass
            # Check if the in-flight heal actually succeeded
            session = await self.redis.get_session(provider, account_id)
            return session is not None and session.get("status") == "HEALTHY"
        
        async with lock:
            try:
                # Re-check inside lock: maybe someone just finished healing it
                session = await self.redis.get_session(provider, account_id)
                if session and session.get("status") == "HEALTHY":
                    return True

                return await self._perform_login(provider, account_id)
            except Exception as e:
                logger.error(f"❌ Synchronous heal failed for {provider}:{account_id}: {e}")
                return False

    async def _perform_login(self, provider: str, account_id: str) -> bool:
        """Trigger automated login via LoginService (Lazy Import to avoid circularity)."""
        try:
            from app.services.login_service import LoginService
            login_service = LoginService()
            return await login_service.auto_login(provider, account_id)
        except Exception as e:
            logger.error(f"Error during synchronous login for {provider}:{account_id}: {e}")
            return False

    async def _mark_session_expired(self, platform: str, account_id: str):
        """Internal helper to mark a session as EXPIRED in Redis."""
        session = await self.redis.get_session(platform, account_id)
        if session:
            session["status"] = "EXPIRED"
            await self.redis.set_session(platform, account_id, session)
            logger.debug(f"Session {platform}:{account_id} marked as EXPIRED.")
