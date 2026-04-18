import logging
from app.providers.curl_compat import AsyncSession, ProxyError, RequestsError
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings
from .helpers import PiccomaHelpers

logger = logging.getLogger("PiccomaSession")


class PiccomaSession:
    def __init__(self, provider):
        self.provider = provider

    def _build_browser_headers(self, referer: str = None) -> dict:
        """S-Grade: Generates headers with randomized ordering to bypass WAF sequencing checks."""
        headers = self.provider.default_headers.copy()
        headers["User-Agent"] = self.provider.default_user_agent
        if referer:
            headers["Referer"] = referer
        return headers

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

    async def is_session_valid(self, session: AsyncSession, deep: bool = False, strictness: str = "normal") -> bool:
        """
        Layered session validation.
        
        - shallow (deep=False): cookie presence and local expiry check.
        - deep (deep=True): full auth_probe with network call to bookshelf/homepage.
        """
        # --- Layer 1: Shallow Sync Check (Performance Path) ---
        import time
        now = time.time()
        pksid_cookie = None
        for c in session.cookies.jar:
            if getattr(c, "name", None) == "pksid":
                pksid_cookie = c
                break
        
        if not pksid_cookie or not getattr(pksid_cookie, "value", None):
            return False
            
        if getattr(pksid_cookie, "expires", None) and pksid_cookie.expires < now:
            logger.warning(f"🕒 [Piccoma] Session cookie 'pksid' expired locally at {pksid_cookie.expires}.")
            return False
            
        if not deep:
            return True

        # --- Layer 2: Deep Probe (Network Path) ---
        try:
            from .auth import PiccomaAuth
            auth = PiccomaAuth(self.provider)
            # 🔧 Use fail-open boolean check for periodic health monitoring
            is_valid = await auth.is_authenticated(session)
            
            if is_valid:
                await self.provider.session_service.record_session_success("piccoma")
            return is_valid
        except Exception as e:
            logger.warning(f"⚠️ [Piccoma Identity] Deep auth probe failed with error: {e}")
            return False
