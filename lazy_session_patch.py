"""
================================================================================
LAZY SESSION AUTHENTICATION PATCH
================================================================================

Three surgical changes to eliminate unnecessary Piccoma logins on bot restart.

Principle: Never login proactively. Load existing cookies → use them → only
re-authenticate when they actually fail during a real task.

================================================================================
PATCH 1: session_service.py — Disk write-through + disk fallback on cold Redis
================================================================================

Two additions to SessionService:

  A) update_session_cookies() — write-through to disk on every update
  B) seed_from_disk()         — called at boot to pre-populate Redis if empty
"""


# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1A: Replace `update_session_cookies` in app/services/session_service.py
# ─────────────────────────────────────────────────────────────────────────────
#
# OLD (current):
#
#     async def update_session_cookies(self, platform: str, account_id: str, cookies: list):
#         session = await self.redis.get_session(platform, account_id)
#         if not session:
#             session = {"account_id": account_id, "platform": platform}
#         session["cookies"] = cookies
#         session["status"] = "HEALTHY"
#         session.pop("error_reason", None)
#         await self.redis.set_session(platform, account_id, session)
#         logger.info(f"✅ Session updated and refreshed: {platform}:{account_id}")
#         await self._emit_status_change(platform)
#
# NEW (replace the entire method with this):

import json
import os
import time
import tempfile

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


# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1B: Add this NEW method to SessionService (below update_session_cookies)
# ─────────────────────────────────────────────────────────────────────────────

async def seed_from_disk(self):
    """
    Boot-time recovery: if Redis has no sessions for a platform, try to
    load the last-known-good session from disk.

    Call once during startup (in TaskQueue.boot or main.py) BEFORE any
    tasks or healers run.
    """
    import glob

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


"""
================================================================================
PATCH 2: tasks/manager.py — Call seed_from_disk() during boot
================================================================================

In TaskQueue.boot(), add the seed call BEFORE worker registration.
"""

# OLD:
#
#     async def boot(self):
#         await self.redis.queue.register_worker()
#         recovered = await self.redis.queue.recover_orphans()
#         if recovered:
#             logger.warning(f"🔄 Boot recovered {recovered} in-flight tasks from prior crash")
#
# NEW (replace the entire method):

async def boot(self):
    """One-shot startup sequence. Call exactly once before workers spin up."""
    # 0. Seed sessions from disk if Redis is cold
    #    This MUST happen before workers start, so the first task doesn't
    #    trigger an unnecessary login.
    try:
        from app.services.session_service import SessionService
        session_service = SessionService()
        await session_service.seed_from_disk()
    except Exception as e:
        logger.warning(f"⚠️ Disk seed failed (non-fatal): {e}")

    # 1. Register this process as an alive worker (starts heartbeat loop)
    await self.redis.queue.register_worker()
    # 2. Sweep dead workers' processing lists back to global queue
    recovered = await self.redis.queue.recover_orphans()
    if recovered:
        logger.warning(f"🔄 Boot recovered {recovered} in-flight tasks from prior crash")


"""
================================================================================
PATCH 3: piccoma/session.py — Lazy validation, no proactive login
================================================================================

Replace the top of _get_authenticated_session to use tiered validation
instead of the current binary "pksid exists or login" check.
"""

# In app/providers/platforms/piccoma/session.py
# Replace the _get_authenticated_session method with this:

async def _get_authenticated_session(self, region_domain: str, account_id: str = "primary") -> "AsyncSession":
    """
    S+ Lazy Authentication: Only login when cookies are genuinely dead.
    
    Tier 0: Load from Redis (or disk fallback via SessionService)
    Tier 1: Shallow validate — pksid present & not locally expired
    Tier 2: On actual task failure (handled by caller) — then force refresh
    
    We NEVER proactively probe Piccoma's servers here. Trust the cookies
    until they actually fail during a real request.
    """
    session_service = self.provider.session_service

    # ── Build the HTTP session with stable fingerprint ──────────────
    impersonation = "chrome142"
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"

    async_session = AsyncSession(
        impersonate=impersonation,
        proxies={"http": Settings.get_proxy(), "https": Settings.get_proxy()},
    )

    headers = self.provider.default_headers.copy()
    headers["User-Agent"] = ua
    async_session.headers.update(headers)

    # ── Tier 0: Load session from Redis ─────────────────────────────
    session_obj = await session_service.redis.get_session("piccoma", account_id)

    # ── Tier 1: Shallow validation (no network calls) ───────────────
    if session_obj and session_obj.get("status") == "HEALTHY":
        cookies = session_obj.get("cookies", [])
        pksid_entry = next(
            (c for c in cookies if c.get("name") == "pksid" and c.get("value")),
            None,
        )

        if pksid_entry:
            # Check local expiry if available
            import time
            expiry = pksid_entry.get("expirationDate") or pksid_entry.get("expires")
            if expiry:
                try:
                    exp_val = float(expiry)
                    if exp_val > 10_000_000_000:  # milliseconds
                        exp_val /= 1000
                    if exp_val < time.time():
                        logger.warning(
                            f"🕒 [Piccoma] pksid expired locally at {exp_val}. Will re-login."
                        )
                        pksid_entry = None  # fall through to login
                except (ValueError, TypeError):
                    pass  # no parseable expiry — trust the cookie

        if pksid_entry:
            # Session looks good — inject cookies and return immediately.
            # No network probe. No maturation. Just use what we have.
            logger.info(
                f"[Piccoma Identity] Applying cached session '{account_id}' "
                f"({len(cookies)} cookies). No login needed."
            )
            for c in cookies:
                name = str(c.get("name") or c.get("key"))
                value = c.get("value")
                if value is None:
                    value = c.get("val")
                value = str(value) if value is not None else None

                if name and value is not None:
                    if name.lower() in ["pksid"]:
                        c_domain = ".piccoma.com"
                        c_path = "/"
                    elif name.lower() in ["csrftoken", "csrf_token", "snexid"]:
                        c_domain = ".piccoma.com"
                        c_path = "/"
                    else:
                        c_domain = c.get("domain") or region_domain
                        c_path = c.get("path") or "/"

                    async_session.cookies.set(name, value, domain=c_domain, path=c_path)

            # ── Thin session maturation (safe, non-blocking) ────────
            if len(async_session.cookies) < 8:
                logger.info(
                    f"🛡️ [Piccoma Identity] 'Thin' session ({len(async_session.cookies)} cookies). "
                    f"Maturing profile..."
                )
                try:
                    nav_headers = PiccomaHelpers.get_navigation_headers()
                    maturation_res = await async_session.get(
                        "https://piccoma.com/web/", headers=nav_headers, timeout=15
                    )

                    # Safety: if maturation triggers an auth kick, discard results
                    if self.provider.helpers.piccoma_html_indicates_guest_shell(
                        str(maturation_res.url), maturation_res.text
                    ):
                        logger.warning(
                            "⚠️ [Piccoma Identity] Maturation triggered Auth Kick. "
                            "Discarding maturation results."
                        )
                        return async_session

                    # Persist matured cookies
                    matured_cookies = []
                    for c in async_session.cookies.jar:
                        name = getattr(c, "name", None)
                        if name:
                            matured_cookies.append({
                                "name": name,
                                "value": getattr(c, "value", ""),
                                "domain": getattr(c, "domain", ".piccoma.com"),
                                "path": getattr(c, "path", "/"),
                                "expires": getattr(c, "expires", None),
                            })

                    if len(matured_cookies) >= 8:
                        await session_service.update_session_cookies(
                            "piccoma",
                            session_obj.get("account_id", "primary"),
                            matured_cookies,
                        )
                        logger.info(
                            f"✅ [Piccoma Identity] Matured & persisted ({len(matured_cookies)} cookies)."
                        )
                except (ProxyError, RequestsError) as proxy_e:
                    logger.warning(f"⚠️ Proxy error during maturation: {proxy_e}")
                except Exception as ritual_e:
                    logger.warning(f"⚠️ Maturation failed: {ritual_e}")

            logger.info(
                f"[DEV-TRACE] Session Identity Audit: "
                f"{len(async_session.cookies)} total cookies active."
            )
            return async_session

    # ── Tier 2: No valid session — must login ───────────────────────
    # Only reaches here if:
    #   - Redis had no session AND disk had no session (seed_from_disk already ran)
    #   - OR session existed but pksid was missing/expired
    #   - OR session status was not HEALTHY (explicitly marked EXPIRED by a real failure)
    logger.info(
        f"🔄 [Piccoma Identity] Session '{account_id}' invalid or absent. "
        f"Triggering forced refresh..."
    )
    session_obj = await session_service.get_authenticated_session(
        "piccoma",
        account_id=account_id,
        force_refresh=True,
    )

    if not session_obj:
        raise ScraperError(
            f"No healthy sessions available for piccoma account '{account_id}' "
            f"after automated login attempt."
        )

    # Apply the freshly obtained cookies
    logger.info(
        f"[Piccoma Identity] Applying fresh session '{session_obj.get('account_id')}' "
        f"({len(session_obj.get('cookies', []))} cookies)."
    )
    for c in session_obj.get("cookies", []):
        name = str(c.get("name") or c.get("key"))
        value = c.get("value")
        if value is None:
            value = c.get("val")
        value = str(value) if value is not None else None

        if name and value is not None:
            if name.lower() in ["pksid"]:
                c_domain = ".piccoma.com"
                c_path = "/"
            elif name.lower() in ["csrftoken", "csrf_token", "snexid"]:
                c_domain = ".piccoma.com"
                c_path = "/"
            else:
                c_domain = c.get("domain") or region_domain
                c_path = c.get("path") or "/"

            async_session.cookies.set(name, value, domain=c_domain, path=c_path)

    return async_session
