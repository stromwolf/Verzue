import re
import json
import logging
import asyncio
import threading
from bs4 import BeautifulSoup
from app.providers.curl_compat import AsyncSession, RequestsError, ProxyError

from app.providers.base import BaseProvider
from app.core.exceptions import ScraperError
from app.services.rate_limiter import PlatformRateLimiter
from config.settings import Settings

try:
    from app.lib.pycasso import Canvas
except ImportError:
    Canvas = None

from .helpers import PiccomaHelpers
from .session import PiccomaSession
from .drm import PiccomaDRM
from .purchase import PiccomaPurchase
from .discovery import PiccomaDiscovery

logger = logging.getLogger("PiccomaProvider")

class PiccomaProvider(BaseProvider):
    IDENTIFIER = "piccoma"
    BASE_URL = "https://piccoma.com"
    SERIES_PATH = "/web/product/"
    DEVELOPER_MODE = True
    
    # S-GRADE: Thread-safe lock to prevent pycasso's global state race condition
    _unscramble_lock = threading.Lock()

    def __init__(self):
        from app.services.session_service import SessionService
        from app.services.login_service import LoginService
        
        self.session_service = SessionService()
        self.login_service = LoginService()
        
        # S-Grade: Chrome 142 baseline headers (Server Compatibility)
        self.default_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        # Initialization order: Helpers first to provide headers
        self.helpers = PiccomaHelpers(self)
        self.default_headers = PiccomaHelpers.get_navigation_headers()
        
        self.default_headers = PiccomaHelpers.get_navigation_headers()
        
        # Initialize survivors
        self.session_manager = PiccomaSession(self)
        self.drm = PiccomaDRM(self)
        self.purchase = PiccomaPurchase(self)
        self.discovery = PiccomaDiscovery(self)

    # --- Delegated Methods ---
    
    def _is_fake_404(self, *args, **kwargs): return self.helpers._is_fake_404(*args, **kwargs)
    async def _safe_request(self, *args, **kwargs): return await self.helpers._safe_request(*args, **kwargs)
    async def run_ritual(self, *args, **kwargs): return await self.helpers.run_ritual(*args, **kwargs)
    def _dump_diagnostic_data(self, *args, **kwargs): return self.helpers._dump_diagnostic_data(*args, **kwargs)
    def _get_context_from_url(self, *args, **kwargs): return self.helpers._get_context_from_url(*args, **kwargs)
    def _format_poster_url(self, *args, **kwargs): return self.helpers._format_poster_url(*args, **kwargs)
    def _build_browser_headers(self, *args, **kwargs): return self.session_manager._build_browser_headers(*args, **kwargs)
    async def _get_authenticated_session(self, *args, **kwargs): return await self.session_manager._get_authenticated_session(*args, **kwargs)
    async def is_session_valid(self, *args, **kwargs): return await self.session_manager.is_session_valid(*args, **kwargs)
    def _extract_pdata(self, *args, **kwargs): return self.drm._extract_pdata(*args, **kwargs)
    def _extract_pdata_heuristic(self, *args, **kwargs): return self.drm._extract_pdata_heuristic(*args, **kwargs)
    async def _download_robust(self, *args, **kwargs): return await self.drm._download_robust(*args, **kwargs)
    def _calculate_seed(self, *args, **kwargs): return self.drm._calculate_seed(*args, **kwargs)
    def _dd_transform(self, *args, **kwargs): return self.drm._dd_transform(*args, **kwargs)
    async def fast_purchase(self, *args, **kwargs): return await self.purchase.fast_purchase(*args, **kwargs)
    def _calculate_security_hash(self, *args, **kwargs): return self.purchase._calculate_security_hash(*args, **kwargs)
    async def get_new_series_list(self, *args, **kwargs): return await self.discovery.get_new_series_list(*args, **kwargs)

    # --- Core Scraper Interface (Kept in main class for now) ---

    async def get_series_info(self, url: str, fast: bool = False):
        """S+ Refinement: Parallel Fetching and JSON-First extraction."""
        match = re.search(r'/web/product/(\d+)', url)
        if not match: raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        base_url, region, domain = self._get_context_from_url(url)
        
        active = await self.session_service.get_active_session("piccoma")
        account_id = active.get("account_id", "primary") if active else "primary"
        logger.info(f"[Piccoma] 🔍 Series Info Requested: {url} (AID: {account_id}, mode={'fast' if fast else 'full'})")
        auth_session = await self._get_authenticated_session(domain, account_id=account_id)
        
        # --- CSRF Handshake ---
        if 'csrftoken' not in auth_session.cookies:
            logger.info("🛡️ [Piccoma Identity] Seed CSRF cookie missing. Performing /web/ Handshake...")
            try:
                handshake_res = await auth_session.get(f"{base_url}/web/", timeout=10)
                if handshake_res.status_code == 200:
                    logger.info("✅ [Piccoma Identity] /web/ Handshake complete. Cookies primed.")
            except Exception as e:
                logger.warning(f"⚠️ [Piccoma Identity] /web/ Handshake ritual failed: {e}")

        # 1. Fetch Data
        product_url = f"{base_url}/web/product/{series_id}"
        episodes_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
        
        try:
            if fast:
                async with PlatformRateLimiter.get("piccoma").acquire():
                    res = await auth_session.get(product_url, timeout=20)
                ep_res = None
            else:
                async with PlatformRateLimiter.get("piccoma").acquire():
                    res = await auth_session.get(product_url, timeout=20)
                async with PlatformRateLimiter.get("piccoma").acquire():
                    ep_res = await auth_session.get(episodes_url, timeout=20)
                
            if res.status_code != 200: 
                raise ScraperError(f"Failed to fetch series: {res.status_code}")
        except (ProxyError, RequestsError) as e:
            logger.error(f"[Piccoma] Network Error (Proxy?): {e}")
            if "403" in str(e):
                 raise ScraperError("Scraping Proxy Denied Access (403). Check IP Whitelist in your Proxy Dashboard or Bandwidth limits.", code="PX_403")
            raise ScraperError(f"Piccoma Network Error: {e}")
        except Exception as e:
            if "ScraperError" in type(e).__name__: raise
            logger.error(f"[Piccoma] Request failed for {url}: {e}")
            raise ScraperError(f"Piccoma series fetch failed: {e}")
        
        logger.debug(f"[Piccoma] Main product response: {res.status_code}")
        if ep_res: logger.debug(f"[Piccoma] Episode list response: {ep_res.status_code}")
        
        # Geo-block detection
        if len(res.text) < 10000 and ("日本国内でのみ" in res.text or "only be used from Japan" in res.text):
            raise ScraperError("Piccoma geo-blocked: This service can only be accessed from Japan. Use a Japan VPN or proxy.")
        
        await self.session_service.record_session_success("piccoma")
        soup = BeautifulSoup(res.text, 'html.parser')
        
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        thumb_img = soup.select_one('.PCM-productThumb_img, .PCM-productThum_img, .PCM-productThumb img, .PCOM-productCover img')
        image_url = self._format_poster_url(thumb_img['src'] if thumb_img else None)

        # 2. Extract Metadata
        release_day, release_time = None, None
        day_map = {"日曜": "Saturday", "月曜": "Sunday", "火曜": "Monday", "水曜": "Tuesday", "木曜": "Wednesday", "金曜": "Thursday", "土曜": "Friday"}
        status_label = "Completed" if "完結" in res.text else None

        status_items = soup.select('ul.PCM-productStatus li')
        for li in status_items:
            text = li.get_text(strip=True)
            # Hiatus detection
            if "休載" in text:
                status_label = "Hiatus"
                continue
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day, release_time = en_day, "15:00"
                    break
        
        logger.info(f"[Piccoma] Metadata Parsed: Title='{title}', Status='{status_label}', Day='{release_day}'")

        # 3. Restriction Check
        tags = [a.get('data-gtm-label', '').upper() for a in soup.select('.PCM-productDesc_tagList a')]
        if not tags: tags = [a.get_text(strip=True).upper() for a in soup.select('.PCM-productDesc_tagList a')]
        
        is_smartoon = "SMARTOON" in tags
        if not is_smartoon:
            raise ScraperError("Currently, Piccoma Manga and Novels are not supported. Only Smartoon series are available.")
        
        task_viewer_prefix = f"{base_url}/web/viewer/s"
        logger.info(f"[Piccoma] Series '{title}' (ID: {series_id}) | Format: Smartoon")

        # 4. Chapter Extraction
        all_chapters = []
        ep_soup = soup if (fast or not ep_res) else BeautifulSoup(ep_res.text, 'html.parser')

        # ✅ S-GRADE: Authoritative total from Piccoma's own counter (全N話)
        # This is the platform-rendered ground truth, faster and more reliable than len(all_chapters)
        declared_total = 0
        length_wrap = ep_soup.select_one('.PCM-headListParts_lengthWrap')
        if length_wrap:
            span = length_wrap.select_one('span')
            if span:
                try:
                    declared_total = int(span.get_text(strip=True))
                    logger.info(f"[Piccoma] Declared total from 全N話 banner: {declared_total}")
                except ValueError:
                    pass

        # Heuristic A: NEXT_DATA
        next_data_script = ep_soup.select_one('script#__NEXT_DATA__')
        if next_data_script:
            try:
                data = json.loads(next_data_script.string)
                state = data.get('props', {}).get('pageProps', {}).get('initialState', {})
                ep_list = state.get('product', {}).get('episodeList', []) or state.get('viewer', {}).get('episodeList', [])
                for ep in ep_list:
                    cid = str(ep.get('id'))
                    c_title = ep.get('title', f"Episode {cid}")
                    
                    # Piccoma sometimes uses snake_case, sometimes camelCase in NEXT_DATA
                    p_is_free = ep.get('is_free', ep.get('isFree', False))
                    p_is_wait_free = ep.get('is_wait_free', ep.get('isWaitFree', False))
                    p_is_new = ep.get('is_new', ep.get('isNew', False))
                    p_is_up = ep.get('is_up', ep.get('isUp', False))

                    all_chapters.append({
                        'id': cid, 'title': c_title, 'notation': c_title,
                        'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                        'is_locked': not p_is_free and not p_is_wait_free,
                        'is_wait_free': bool(p_is_wait_free),
                        'is_new': bool(p_is_new) or bool(p_is_up),
                        'is_up': bool(p_is_up),
                    })
            except: pass

        # Heuristic B: HTML
        if not all_chapters:
            ep_items = ep_soup.select('ul.PCM-epList li, div.PCM-epList_item, li[class*="PCM-epList"]')
            for item in ep_items:
                link = item.select_one('a')
                if not link: continue
                href, cid = link.get('href', ''), link.get('data-episode_id')
                if not cid:
                    m = re.search(r'/web/viewer/(?:s/)?\d+/(\d+)', href)
                    cid = m.group(1) if m else None
                if not cid: continue
                
                title_tag = item.select_one('p.PCM-epList_title, span.PCM-epList_title, .PCM-epList_title')
                c_title = title_tag.get_text(strip=True) if title_tag else f"Episode {cid}"
                row_text = item.get_text()
                # S-Grade: Robust Wait-Free detection (Check classes + specific icon images)
                is_wait_free_row = bool(item.select_one('.PCM-icon_waitfree, .PCM-epList_status_waitfree')) or (
                    '待てば' in row_text and ('￥0' in row_text or '¥0' in row_text)
                )
                if not is_wait_free_row:
                    # Check alt/alt-text of images as a deep fallback
                    for img in item.select('img'):
                        alt = img.get('alt', '')
                        if '待てば' in alt and ('￥0' in alt or '¥0' in alt):
                            is_wait_free_row = True
                            break

                is_locked = bool(item.select_one('.PCM-epList_lock, .PCM-icon_lock, .PCM-icon_waitfree, .PCM-icon_clock, .PCM-epList_status_waitfree'))
                if not is_locked:
                    is_locked = any(kw in row_text for kw in ["待てば￥0", "¥0"]) is False and "無料" not in row_text

                # ✅ NEW: Detect Piccoma's UP icon via CSS class
                item_classes = item.get('class', [])
                is_up = 'PCM-stt_up' in item_classes

                all_chapters.append({
                    'id': cid, 'title': c_title, 'notation': c_title, 'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                    'is_locked': is_locked, 
                    'is_wait_free': is_wait_free_row, 
                    'is_new': "NEW" in row_text.upper() or is_up,
                    'is_up': is_up
                })

        try: all_chapters.sort(key=lambda x: int(x['id']))
        except: pass
        total = declared_total if declared_total > 0 else len(all_chapters)
        return title, total, all_chapters, image_url, str(series_id), release_day, release_time, status_label, None

    async def scrape_chapter(self, task, output_dir: str):
        """S+ Refinement: Stateless and Heuristic Extraction."""
        match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', task.url)
        if not match: raise ScraperError("Invalid Piccoma Viewer URL")
        
        series_id, chapter_id = match.groups()
        base_url, region, domain = self._get_context_from_url(task.url)
        
        active = await self.session_service.get_active_session("piccoma")
        account_id = active.get("account_id", "primary") if active else "primary"
        auth_session = await self._get_authenticated_session(domain, account_id=account_id)
        
        episodes_referer = f"{base_url}/web/product/{series_id}/episodes?etype=E"
        viewer_nav_headers = self._build_browser_headers(referer=episodes_referer)

        async def _fetch_viewer_with_trace(session):
            async with PlatformRateLimiter.get("piccoma").acquire():
                _res = await session.get(task.url, timeout=30, headers=viewer_nav_headers)
            _final_url = str(getattr(_res, "url", task.url))
            _redirect_chain = []
            for _h in getattr(_res, "history", []) or []:
                _redirect_chain.append({
                    "status": getattr(_h, "status_code", None),
                    "url": str(getattr(_h, "url", "")),
                    "location": _h.headers.get("Location") if hasattr(_h, "headers") else None
                })

            _signin_markers = self.helpers.piccoma_html_indicates_guest_shell(_final_url, _res.text)
            _has_next_data = 'id="__NEXT_DATA__"' in _res.text or "script#__NEXT_DATA__" in _res.text
            _has_purchase_form = "js_purchaseForm" in _res.text
            _has_charging = "チャージ中" in _res.text
            _has_points_read = "ポイントで読む" in _res.text

            _verdict = "OK (Authenticated)"
            if _signin_markers:
                _verdict = f"REJECTED (Kicked to Login) -> {_final_url}"
            elif _has_charging:
                _verdict = "LOCKED (Wait-Free Charging)"
            elif _has_points_read or _has_purchase_form:
                _verdict = "LOCKED (Coins/Points Required)"
            elif not _has_next_data and _res.status_code == 200:
                _verdict = "ERROR (Empty Shell/WAF Block)"

            return (
                _res,
                _final_url,
                _redirect_chain,
                _signin_markers,
                _has_next_data,
                _has_purchase_form,
                _has_charging,
                _has_points_read,
                _verdict
            )

        try:
            # DEV-TRACE: Log non-sensitive cookie identity for viewer request diagnostics.
            cookie_audit = []
            for c in getattr(auth_session.cookies, "jar", []):
                cookie_audit.append({
                    "name": getattr(c, "name", None),
                    "domain": getattr(c, "domain", None),
                    "path": getattr(c, "path", None),
                    "expires": getattr(c, "expires", None),
                    "has_value": bool(getattr(c, "value", None))
                })
            logger.debug(
                f"[Piccoma][DEV-TRACE] Viewer cookie audit for {chapter_id}: "
                f"{len(cookie_audit)} cookies loaded."
            )
            (
                res,
                final_url,
                redirect_chain,
                signin_markers,
                has_next_data,
                has_purchase_form,
                has_charging,
                has_points_read,
                verdict
            ) = await _fetch_viewer_with_trace(auth_session)
        except (ProxyError, RequestsError) as e:
            logger.error(f"[Piccoma] Proxy Error during chapter access: {e}")
            raise ScraperError("Proxy Access Denied (403). Ensure VPS IP is whitelisted.", code="PX_403")
        except Exception as e:
            raise ScraperError(f"Chapter access failed: {e}")

        logger.info(
            f"[Piccoma] Viewer Access Diagnostic for {chapter_id}: {verdict} "
            f"(Status: {res.status_code}, Len: {len(res.text)}, Redirects: {len(redirect_chain)})"
        )

        if signin_markers:
            logger.warning(
                f"[Piccoma] Viewer returned SIGNIN page for chapter {chapter_id} on first attempt. "
                "Forcing session heal before retry."
            )
            
            # 🔧 CRITICAL FIX: Report failure FIRST so the healer kicks in,
            # then AWAIT the healing synchronously before retrying.
            try:
                await self.session_service.report_session_failure(
                    "piccoma",
                    account_id,
                    reason="Viewer returned signin page (initial attempt)"
                )
            except Exception as e:
                logger.warning(f"[Piccoma][DEV-TRACE] Failed to record session failure: {e}")
            
            # 🔧 Force-await a fresh authenticated session
            try:
                # Trigger synchronous heal through session service
                fresh_obj = await self.session_service.get_authenticated_session(
                    "piccoma",
                    account_id=account_id,
                    force_refresh=True,
                    timeout=60
                )
                if not fresh_obj:
                    raise ScraperError("Piccoma auth failure: healer could not produce a valid session.")
            except Exception as e:
                raise ScraperError(f"Piccoma auth failure during forced heal: {e}")
            
            # Re-apply the FRESH session to our scraper
            auth_session = await self._get_authenticated_session(domain, account_id=account_id)
            
            # Re-run the warm-up ritual on the NEW session
            try:
                logger.info(f"[Piccoma] Running inline identity ritual on FRESHLY HEALED session for chapter {chapter_id}.")
                await self.run_ritual(auth_session, base_url)
            except Exception as e:
                logger.warning(f"[Piccoma][DEV-TRACE] Inline ritual failed after heal: {e}")
            
            # Now retry the viewer
            (
                res, final_url, redirect_chain, signin_markers,
                has_next_data, has_purchase_form, has_charging,
                has_points_read, verdict
            ) = await _fetch_viewer_with_trace(auth_session)
            
            logger.info(f"[Piccoma] Viewer Retry Result for {chapter_id} (after heal): {verdict}")
            
            if signin_markers:
                # Both attempts failed even with fresh login
                try:
                    await self.session_service.report_session_failure(
                        "piccoma",
                        account_id,
                        reason="Viewer rejected even after forced heal — account may be flagged"
                    )
                except Exception:
                    pass
                raise ScraperError(
                    f"Piccoma auth failure: viewer rejected chapter {chapter_id} even after forced re-login. "
                    "Account may be shadow-banned, geo-blocked, or require 2FA."
                )

        is_locked_ui = res.status_code == 200 and (
            "js_purchaseForm" in res.text
            or "チャージ中" in res.text
            or "ポイントで読む" in res.text
            or self.helpers.viewer_redirected_to_product_page(task.url, final_url)
        )

        if res.status_code != 200 or is_locked_ui:
            reason = f"HTTP {res.status_code}" if res.status_code != 200 else "Locked UI detected"
            logger.info(f"[Piccoma] Chapter {chapter_id} {reason}, attempting fast purchase/unlock.")
            if await self.fast_purchase(task):
                auth_session = await self._get_authenticated_session(domain, account_id=account_id)
                async with PlatformRateLimiter.get("piccoma").acquire():
                    res = await auth_session.get(task.url, timeout=30, headers=viewer_nav_headers)
                final_url = str(getattr(res, "url", task.url))
            else:
                logger.error(f"  ❌ [Piccoma] Fast purchase failed for {chapter_id}")
             
        if res.status_code != 200: 
            raise ScraperError(f"Access error: {res.status_code}")
        
        await self.session_service.record_session_success("piccoma")

        # Manifest discovery
        pdata = self._extract_pdata_heuristic(res.text)
        if not pdata:
            if self.helpers.viewer_redirected_to_product_page(task.url, final_url):
                raise ScraperError(
                    f"Piccoma: chapter {chapter_id} is not unlocked; viewer redirected to the series product page "
                    f"({final_url}). Coin/wait-free API unlock failed or this episode is not available for this account."
                )
            logger.error(f"[Piccoma] Manifest extraction FAILED for {chapter_id}.")
            self._dump_diagnostic_data(
                f"manifest_fail_{chapter_id}",
                res.text,
                metadata={
                    "request_url": task.url,
                    "final_url": final_url,
                    "status_code": res.status_code,
                    "response_length": len(res.text),
                    "content_type": res.headers.get("Content-Type"),
                    "signin_markers": signin_markers,
                    "has_next_data": has_next_data,
                    "has_purchase_form": has_purchase_form,
                    "has_charging_marker": has_charging,
                    "has_points_read_marker": has_points_read,
                    "redirect_chain": redirect_chain,
                    "cookie_audit": cookie_audit
                }
            )
            raise ScraperError(f"Could not extract chapter manifest for {chapter_id}.")

        images = pdata.get('img', pdata.get('contents', []))
        valid_images = [img for img in images if img.get('path')]
        if not valid_images: raise ScraperError("No accessible images found in manifest.")

        total = len(valid_images)
        from app.core.progress import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Piccoma", total, episode_id=task.episode_id)
        progress.update(0)

        async def process_one(img_data, i):
            async with PlatformRateLimiter.get("piccoma").acquire():
                await self._download_robust(auth_session, img_data, i+1, output_dir, region)
            progress.update(i + 1)

        await asyncio.gather(*(process_one(img, i) for i, img in enumerate(valid_images)))
        progress.finish()

        # Flush any cookies Piccoma rotated during the task back to Redis,
        # so the next task starts with a valid pksid and avoids a redundant login.
        try:
            session_cookies = []
            for c in auth_session.cookies.jar:
                name = getattr(c, "name", None)
                if name:
                    session_cookies.append({
                        "name": name,
                        "value": getattr(c, "value", ""),
                        "domain": getattr(c, "domain", ".piccoma.com"),
                        "path": getattr(c, "path", "/"),
                        "expires": getattr(c, "expires", None),
                    })
            has_pksid = any(c["name"] == "pksid" and c["value"] for c in session_cookies)
            
            # S+ Safety Guard: Check if the session landed on a sign-in or guest shell during the task.
            # We use the final response (res) from the task to check.
            is_logged_out = self.helpers.piccoma_html_indicates_guest_shell(str(getattr(res, "url", "")), res.text)
            
            if session_cookies and has_pksid and not is_logged_out:
                active = await self.session_service.get_active_session("piccoma")
                aid = active.get("account_id", "primary") if active else "primary"
                await self.session_service.update_session_cookies("piccoma", aid, session_cookies)
                logger.info(f"[Piccoma] Session cookies persisted after task ({len(session_cookies)} cookies, pksid present).")
            else:
                reason = "pksid missing" if not has_pksid else "session triggered auth-kick during task"
                logger.warning(f"[Piccoma] Skipping post-task cookie flush: {reason}.")
        except Exception as e:
            logger.warning(f"[Piccoma] Failed to persist session cookies after task: {e}")

        return output_dir
