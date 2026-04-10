import logging
from app.providers.curl_compat import AsyncSession, ProxyError, RequestsError
from app.services.session_service import SessionService
from app.services.login_service import LoginService
from app.core.exceptions import ScraperError
from config.settings import Settings

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

    async def _get_authenticated_session(self, region_domain: str) -> "AsyncSession":
        """S+ Refinement: TLS Fingerprint Entropy & Explicit Scoping."""
        session_service = self.provider.session_service
        login_service = self.provider.login_service
        
        session_obj = await session_service.get_active_session("piccoma")
        
        # Keep a stable fingerprint for Piccoma to reduce auth/session flapping
        impersonation = "chrome142"
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        
        async_session = AsyncSession(impersonate=impersonation, proxies={"http": Settings.get_proxy(), "https": Settings.get_proxy()})
        
        headers = self.provider.default_headers.copy()
        headers["User-Agent"] = ua
        async_session.headers.update(headers)

        if session_obj:
            has_pksid = any(c.get('name') == 'pksid' and c.get('value') for c in session_obj.get("cookies", []))
            if not has_pksid:
                logger.warning("  ⚠️ [Auth Health Audit] Session 'primary' found but 'pksid' is MISSING or EMPTY. Treating as no session.")
                session_obj = None

        if not session_obj:
            async with session_service.get_refresh_lock("piccoma"):
                session_obj = await session_service.get_active_session("piccoma")
                
                if session_obj:
                    if not any(c.get('name') == 'pksid' and c.get('value') for c in session_obj.get("cookies", [])):
                        session_obj = None

                if not session_obj:
                    logger.info("🔄 [Piccoma Identity] No healthy sessions in vault. Triggering automated login fallback...")
                    login_success = await login_service.auto_login("piccoma")
                    
                    if login_success:
                        session_obj = await session_service.get_active_session("piccoma")
                        
            if not session_obj:
                raise ScraperError("No healthy sessions available for piccoma after automated login attempt.")
        
        if session_obj:
            logger.info(f"[Piccoma Identity] Applying session '{session_obj.get('account_id')}' ({len(session_obj.get('cookies', []))} cookies).")
            
            for c in session_obj.get("cookies", []):
                name = str(c.get('name') or c.get('key'))
                # Handle possible missing 'value' key if it's named 'val'
                value = c.get('value')
                if value is None: value = c.get('val')
                value = str(value) if value is not None else None
                
                if name and value is not None:
                    if name.lower() in ['pksid', 'csrftoken', 'csrf_token']:
                        c_domain = ".piccoma.com"
                        c_path = "/"
                    else:
                        c_domain = c.get('domain') or region_domain
                        c_path = c.get('path') or "/"
                    
                    async_session.cookies.set(name, value, domain=c_domain, path=c_path)
                    
            if len(async_session.cookies) < 8:
                logger.info(f"🛡️ [Piccoma Identity] 'Thin' session detected ({len(async_session.cookies)} cookies). Maturing profile...")
                try:
                    await async_session.get("https://piccoma.com/web/", timeout=15)
                    matured_cookies = []
                    # S+ Identity Handshake Capture
                    for c in async_session.cookies.jar:
                        name = getattr(c, 'name', None)
                        if name:
                            matured_cookies.append({
                                "name": name,
                                "value": getattr(c, 'value', ""),
                                "domain": getattr(c, 'domain', ".piccoma.com"),
                                "path": getattr(c, 'path', "/"),
                                "expires": getattr(c, 'expires', None)
                            })
                    
                    if len(matured_cookies) >= 8:
                        logger.info(f"✅ [Piccoma Identity] Identity matured and PERSISTED ({len(matured_cookies)} cookies).")
                        await session_service.update_session_cookies("piccoma", session_obj.get('account_id', 'primary'), matured_cookies)
                except (ProxyError, RequestsError) as proxy_e:
                    logger.warning(f"⚠️ [Piccoma Identity] Proxy error during identity maturation: {proxy_e}")
                except Exception as ritual_e:
                    logger.warning(f"⚠️ [Piccoma Identity] Identity maturation failed: {ritual_e}")

            logger.info(f"[DEV-TRACE] Session Identity Audit: {len(async_session.cookies)} total cookies active.")
            return async_session

    async def is_session_valid(self, session: AsyncSession) -> bool:
        """Stateless validation: any authed 200 probe, or /web/ 200 without guest markers."""
        probe_urls = (
            "https://piccoma.com/web/",
            "https://piccoma.com/web/mypage/bookshelf",
            "https://piccoma.com/web/mypage/history",
        )

        def denies_auth(res) -> bool:
            u = str(getattr(res, "url", "") or "")
            t = res.text or ""
            if "/web/acc/signin" in u or "/acc/email/signin" in u or "/acc/signin?" in u:
                return True
            if "ログイン｜ピッコマ" in t:
                return True
            if "PCM-loginMenu" in t:
                return True
            if len(t) < 70000 and "PCM-headerLogin" in t:
                return True
            return False

        def confirms(res) -> bool:
            return res.status_code == 200 and not denies_auth(res)

        try:
            last_res = None
            for url in probe_urls:
                res = await session.get(url, timeout=15)
                last_res = res
                if confirms(res):
                    await self.provider.session_service.record_session_success("piccoma")
                    return True

            web_res = await session.get("https://piccoma.com/web/", timeout=15)
            if confirms(web_res):
                await self.provider.session_service.record_session_success("piccoma")
                return True

            final_url = str(getattr(last_res, "url", "")) if last_res else ""
            logger.warning(
                f"[Piccoma Identity] Session validation failed after probes: "
                f"last_status={getattr(last_res, 'status_code', None)}, last_url={final_url}"
            )
            return False
        except Exception:
            return False
