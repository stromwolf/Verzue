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
        """S+ Refinement: TLS Fingerprint Entropy & Explicit Scoping."""
        session_service = self.provider.session_service
        
        # S-Grade: Use the new centralized authenticated session retrieval
        session_obj = await session_service.redis.get_session("piccoma", account_id)

        # Keep a stable fingerprint for Piccoma to reduce auth/session flapping
        impersonation = "chrome142"
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"

        async_session = AsyncSession(
            impersonate=impersonation,
            proxies={"http": Settings.get_proxy(), "https": Settings.get_proxy()},
        )

        headers = self.provider.default_headers.copy()
        headers["User-Agent"] = ua
        async_session.headers.update(headers)

        if session_obj:
            has_pksid = any(
                c.get("name") == "pksid" and c.get("value")
                for c in session_obj.get("cookies", [])
            )
            if not has_pksid or session_obj.get("status") != "HEALTHY":
                logger.warning(
                    f"  ⚠️ [Auth Health Audit] Session '{account_id}' found but its status is {session_obj.get('status')} or pksid is MISSING. Triggering refresh."
                )
                session_obj = None

        if not session_obj:
            # Trigger synchronous heal through session service
            logger.info(f"🔄 [Piccoma Identity] Session '{account_id}' invalid. Triggering forced refresh...")
            session_obj = await session_service.get_authenticated_session(
                "piccoma", 
                account_id=account_id, 
                force_refresh=True
            )

            if not session_obj:
                raise ScraperError(
                    f"No healthy sessions available for piccoma account '{account_id}' after automated login attempt."
                )

        if session_obj:
            logger.info(
                f"[Piccoma Identity] Applying session '{session_obj.get('account_id')}' ({len(session_obj.get('cookies', []))} cookies)."
            )

            for c in session_obj.get("cookies", []):
                name = str(c.get("name") or c.get("key"))
                # Handle possible missing 'value' key if it's named 'val'
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

            if len(async_session.cookies) < 8:
                logger.info(
                    f"🛡️ [Piccoma Identity] 'Thin' session detected ({len(async_session.cookies)} cookies). Maturing profile..."
                )
                try:
                    nav_headers = PiccomaHelpers.get_navigation_headers()
                    maturation_res = await async_session.get("https://piccoma.com/web/", headers=nav_headers, timeout=15)
                    
                    # S+ Safety Guard: If Piccoma pushes the maturation request to sign-in,
                    # DO NOT extract or persist the resulting guest cookies.
                    if self.provider.helpers.piccoma_html_indicates_guest_shell(str(maturation_res.url), maturation_res.text):
                        logger.warning(
                            "⚠️ [Piccoma Identity] Maturation visit triggered an Auth Kick. Discarding maturation results to protect session."
                        )
                        return async_session

                    matured_cookies = []
                    # S+ Identity Handshake Capture
                    for c in async_session.cookies.jar:
                        name = getattr(c, "name", None)
                        if name:
                            matured_cookies.append(
                                {
                                    "name": name,
                                    "value": getattr(c, "value", ""),
                                    "domain": getattr(c, "domain", ".piccoma.com"),
                                    "path": getattr(c, "path", "/"),
                                    "expires": getattr(c, "expires", None),
                                }
                            )

                    if len(matured_cookies) >= 8:
                        logger.info(
                            f"✅ [Piccoma Identity] Identity matured and PERSISTED ({len(matured_cookies)} cookies)."
                        )
                        await session_service.update_session_cookies(
                            "piccoma",
                            session_obj.get("account_id", "primary"),
                            matured_cookies,
                        )
                except (ProxyError, RequestsError) as proxy_e:
                    logger.warning(
                        f"⚠️ [Piccoma Identity] Proxy error during identity maturation: {proxy_e}"
                    )
                except Exception as ritual_e:
                    logger.warning(
                        f"⚠️ [Piccoma Identity] Identity maturation failed: {ritual_e}"
                    )

            logger.info(
                f"[DEV-TRACE] Session Identity Audit: {len(async_session.cookies)} total cookies active."
            )
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
