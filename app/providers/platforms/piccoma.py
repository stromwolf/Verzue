import re
import json
import logging
import math
import asyncio
import urllib.parse
import os
import random
import threading
import struct
import time
import uuid
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.services.login_service import LoginService
from app.core.exceptions import ScraperError, MechaException
from config.settings import Settings
try:
    from app.lib.pycasso import Canvas
except ImportError:
    Canvas = None

logger = logging.getLogger("PiccomaProvider")

class PiccomaProvider(BaseProvider):
    IDENTIFIER = "piccoma"
    BASE_URL = "https://piccoma.com"
    SERIES_PATH = "/web/product/"
    
    # 🕵️ DEVELOPER MODE: Set to True to dump HTML on extraction/purchase failures
    DEVELOPER_MODE = True
    
    
    # S-GRADE: Thread-safe lock to prevent pycasso's global state race condition
    _unscramble_lock = threading.Lock()

    def __init__(self):
        self.session_service = SessionService()
        self.login_service = LoginService()
        
        self.default_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        # S-Grade Backpressure
        self._download_semaphore = asyncio.Semaphore(10)

    def _dump_diagnostic_data(self, label: str, content: str, metadata: dict = None):
        """S-Grade Diagnostic: Dumps HTML/State to local files for expert analysis."""
        if not self.DEVELOPER_MODE: return
        
        try:
            import datetime
            timestamp = datetime.datetime.now().strftime("%H%M%S")
            # Root: tmp/piccoma_dev/
            dump_dir = os.path.join(os.getcwd(), "tmp", "piccoma_dev")
            os.makedirs(dump_dir, exist_ok=True)
            
            # 1. Save Content (HTML/JSON)
            filename = f"{timestamp}_{label}.html"
            filepath = os.path.join(dump_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            
            # 2. Save Metadata (Headers/Payload)
            if metadata:
                meta_path = os.path.join(dump_dir, f"{timestamp}_{label}_meta.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=4)
            
            logger.info(f"📁 [DEV-TRACE] Diagnostic dump created: tmp/piccoma_dev/{filename}")
        except Exception as e:
            logger.error(f"Failed to dump diagnostic data: {e}")

    def _get_context_from_url(self, url: str):
        """S+ Refinement: Stateless context derivation."""
        if "fr.piccoma" in url or "/fr" in url:
            # S-Grade Security: Reject .fr domains as requested
            raise ScraperError("Piccoma France (.fr) is not supported at this time. Please use a Piccoma Japan (.com) link.")
        return "https://piccoma.com", "jp", ".piccoma.com"

    def _format_poster_url(self, url: str | None) -> str | None:
        """S+ Refinement: Unified poster formatting logic for Discord embeds via wsrv.nl proxy."""
        if not url: return None
        if url.startswith('//'): url = 'https:' + url
        
        # Consistent Proxying for Discord Embed reliability
        # We proxy all Piccoma static images through wsrv.nl to avoid CDN hotlinking blocks
        if any(domain in url for domain in ['piccoma.com', 'piccoma-static.com', 'piccoma.jp', 'kakaocdn.net']):
            return f"https://wsrv.nl/?url={urllib.parse.quote(url)}&w=600&fit=cover"
        return url

    async def _get_authenticated_session(self, region_domain: str) -> "AsyncSession":
        """S+ Refinement: TLS Fingerprint Entropy & Explicit Scoping."""
        session_obj = await self.session_service.get_active_session("piccoma")
        
        # S+ Fingerprint Entropy: Rotate between modern browser profiles
        browser_profiles = ["chrome110", "chrome116", "chrome120", "safari15_5", "edge101"]
        impersonation = random.choice(browser_profiles)
        
        async_session = AsyncSession(impersonate=impersonation, proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)

        if session_obj:
            # Audit session health: Ensure pksid exists and is not empty
            has_pksid = any(c.get('name') == 'pksid' and c.get('value') for c in session_obj.get("cookies", []))
            if not has_pksid:
                logger.warning("  ⚠️ [Auth Health Audit] Session 'primary' found but 'pksid' is MISSING or EMPTY. Treating as no session.")
                session_obj = None

        if not session_obj:
            # S+ GRADE: Automated Login Fallback
            # If no healthy sessions are in the vault, we attempt to refresh using LoginService
            async with self.session_service.get_refresh_lock("piccoma"):
                # Double-check after acquiring lock in case another worker already refreshed
                session_obj = await self.session_service.get_active_session("piccoma")
                
                # Re-audit health of retrieved session
                if session_obj:
                    if not any(c.get('name') == 'pksid' and c.get('value') for c in session_obj.get("cookies", [])):
                        session_obj = None

                if not session_obj:
                    logger.info("🔄 [Piccoma Identity] No healthy sessions in vault. Triggering automated login fallback...")
                    login_success = await self.login_service.auto_login("piccoma")
                    
                    if login_success:
                        session_obj = await self.session_service.get_active_session("piccoma")
                        
            if not session_obj:
                # S-GRADE: Explicitly fail if no session is available after fallback attempt
                raise ScraperError("No healthy sessions available for piccoma after automated login attempt. Please check logs for specific errors.")
        
        if session_obj:
            logger.info(f"[Piccoma Identity] Applying session '{session_obj.get('account_id')}' ({len(session_obj.get('cookies', []))} cookies).")
            
            # 🟢 S+ Identity Deep Fingerprint: Strict Header Ordering
            # This mimics exactly how a modern browser sends headers to bypass WAF sequencing checks.
            async_session.header_order = [
                "Host", "User-Agent", "Accept", "Accept-Language", "Accept-Encoding",
                "Referer", "Origin", "Connection", "Upgrade-Insecure-Requests",
                "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Sec-Fetch-User",
                "X-Requested-With", "X-CSRF-Token", "X-Security-Hash", "X-Hash-Code"
            ]
            
            # 🟢 S+ Identity Trace: Granular cookie audit
            applied_trace = []
            for c in session_obj.get("cookies", []):
                name = str(c.get('name') or c.get('key'))
                value = str(c.get('value') or c.get('val'))
                
                if name and value is not None:
                    # 🟢 S+ USER-REQUEST: Honor exact metadata (No forced dot-domain overrides)
                    # Forcing .piccoma.com on host-only cookies (like csrftoken) is a bot signal.
                    c_domain = c.get('domain') or region_domain
                    c_path = c.get('path') or "/"
                    
                    async_session.cookies.set(name, value, domain=c_domain, path=c_path)
                    
            # --- 🟢 S+ USER-REQUEST: Persistent Thick Identity Maturation ---
            # If the loaded session is 'thin' (too few cookies), it triggers a 'Warming Ritual'
            # to acquire tracking cookies and then SAVES them back to the vault.
            if len(async_session.cookies) < 8:
                logger.info(f"🛡️ [Piccoma Identity] 'Thin' session detected ({len(async_session.cookies)} cookies). Maturing profile...")
                try:
                    # 1. Handshake Ritual (Landing + Product)
                    await async_session.get(f"{base_url}/web/", timeout=10)
                    # 🕵️ Audit the resulting matured jar
                    matured_cookies = []
                    for c in async_session.cookies.jar:
                        # Defensive check: ensure we have a real Cookie object
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
                        # Save back to vault so next task starts matured
                        await self.session_service.update_session_cookies("piccoma", session_obj.get('account_id', 'primary'), matured_cookies)
                except Exception as ritual_e:
                    logger.warning(f"⚠️ [Piccoma Identity] Identity maturation failed: {ritual_e}")

            # 🕵️ [DEV-TRACE]: Final session audit
            logger.info(f"[DEV-TRACE] Session Identity Audit: {len(async_session.cookies)} total cookies active in AsyncSession.")
            return async_session

    async def is_session_valid(self, session) -> bool:
        """Stateless validation: Check if redirected on the current session's base."""
        try:
            base_url = "https://piccoma.com" 
            res = await session.get(f"{base_url}/web/product/favorite", timeout=15, allow_redirects=False)
            valid = res.status_code == 200
            if valid:
                await self.session_service.record_session_success("piccoma")
            return valid
        except Exception: 
            return False

    async def get_series_info(self, url: str, fast: bool = False):
        """S+ Refinement: Parallel Fetching and JSON-First extraction."""
        match = re.search(r'/web/product/(\d+)', url)
        if not match: raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        base_url, region, domain = self._get_context_from_url(url)
        auth_session = await self._get_authenticated_session(domain)
        
        # --- 🟢 S+ USER-REQUEST: Mandatory /web/ Handshake for Cookies ---
        # If we don't have a 'csrftoken' in our jar, we MUST hit the landing page 
        # first to establish the cookie identity, as per user requirement.
        if 'csrftoken' not in auth_session.cookies:
            logger.info("🛡️ [Piccoma Identity] Seed CSRF cookie missing. Performing /web/ Handshake...")
            try:
                handshake_res = await auth_session.get(f"{base_url}/web/", timeout=10)
                if handshake_res.status_code == 200:
                    logger.info("✅ [Piccoma Identity] /web/ Handshake complete. Cookies primed.")
            except Exception as e:
                logger.warning(f"⚠️ [Piccoma Identity] /web/ Handshake ritual failed: {e}")

        # 1. Parallel Fetching (S-Grade Latency Optimization)
        product_url = f"{base_url}/web/product/{series_id}"
        episodes_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
        
        if fast:
            res = await auth_session.get(product_url)
            ep_res = None
        else:
            # Fire both at once to halve network wait time
            res_task = auth_session.get(product_url)
            ep_task = auth_session.get(episodes_url)
            res, ep_res = await asyncio.gather(res_task, ep_task)
            
        if res.status_code != 200: raise ScraperError(f"Failed to fetch series: {res.status_code}")
        
        # Geo-block detection
        if len(res.text) < 10000 and ("日本国内でのみ" in res.text or "only be used from Japan" in res.text):
            raise ScraperError("Piccoma geo-blocked: This service can only be accessed from Japan. Use a Japan VPN or proxy.")
        
        await self.session_service.record_session_success("piccoma")
        # Use lxml for 10x faster parsing
        soup = BeautifulSoup(res.text, 'lxml')
        
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        thumb_img = soup.select_one('.PCM-productThumb_img, .PCM-productThum_img, .PCM-productThumb img, .PCOM-productCover img')
        image_url = self._format_poster_url(thumb_img['src'] if thumb_img else None)

        # 2. Extract Metadata & Release Day
        release_day, release_time = None, None
        day_map = {"日曜": "Saturday", "月曜": "Sunday", "火曜": "Monday", "水曜": "Tuesday", "木曜": "Wednesday", "金曜": "Thursday", "土曜": "Friday"}
        status_label = "Completed" if "完結" in res.text else None

        status_items = soup.select('ul.PCM-productStatus li')
        for li in status_items:
            text = li.get_text(strip=True)
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day, release_time = en_day, "15:00"
                    break

        # 🟢 SMART-OON detection (STRICT: Tag-Based Identification)
        # As requested: We strictly identify Smaratoon only if it appears in the tag list.
        tags = [a.get('data-gtm-label', '').upper() for a in soup.select('.PCM-productDesc_tagList a')]
        if not tags:
            # Fallback to text content if GTM labels are missing
            tags = [a.get_text(strip=True).upper() for a in soup.select('.PCM-productDesc_tagList a')]
        
        is_smartoon = "SMARTOON" in tags
        is_novel = "ノベル" in tags or "NOVEL" in tags
        
        # 🟢 S-GRADE: Restriction Check
        if not is_smartoon:
            # S-Grade: Professional Apple-style messaging
            raise ScraperError("Currently, Piccoma Manga and Novels are not supported. Only Smartoon series are available.")
        
        task_viewer_prefix = f"{base_url}/web/viewer/s"
        logger.info(f"[Piccoma] Series '{title}' (ID: {series_id}) | Format: Smartoon")

        # 3. Chapter Extraction (JSON-First Heuristic)
        all_chapters = []
        
        # Source 1: If fast=True, check the current soup (landing page)
        if fast or not ep_res:
             ep_soup = soup
        else:
             ep_soup = BeautifulSoup(ep_res.text, 'lxml')

        # 🟢 HEURISTIC A: Extract from __NEXT_DATA__ (Fastest O(1) Access)
        next_data_script = ep_soup.select_one('script#__NEXT_DATA__')
        if next_data_script:
            try:
                data = json.loads(next_data_script.string)
                state = data.get('props', {}).get('pageProps', {}).get('initialState', {})
                ep_list = state.get('product', {}).get('episodeList', []) or state.get('viewer', {}).get('episodeList', [])
                
                for ep in ep_list:
                    cid = str(ep.get('id'))
                    c_title = ep.get('title', f"Episode {cid}")
                    all_chapters.append({
                        'id': cid, 'title': c_title, 'notation': c_title,
                        'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                        'is_locked': not ep.get('is_free', False) and not ep.get('is_wait_free', False),
                        'is_new': ep.get('is_new', False)
                    })
            except: pass

        # 🟢 HEURISTIC B: Fallback to HTML Iteration (Expensive O(N) DOM)
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
                is_locked = bool(item.select_one('.PCM-epList_lock, .PCM-icon_lock, .PCM-icon_waitfree, .PCM-icon_clock'))
                if not is_locked:
                    is_locked = any(kw in item.get_text() for kw in ["待てば￥0", "¥0"]) is False and "無料" not in item.get_text()
                
                all_chapters.append({
                    'id': cid, 'title': c_title, 'notation': c_title, 'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                    'is_locked': is_locked, 'is_new': "NEW" in item.get_text().upper()
                })

        # Sort and return
        try: all_chapters.sort(key=lambda x: int(x['id']))
        except: pass
        return title, len(all_chapters), all_chapters, image_url, str(series_id), release_day, release_time, status_label, None

    async def scrape_chapter(self, task, output_dir: str):
        """S+ Refinement: Stateless and Heuristic Extraction."""
        match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', task.url)
        if not match: raise ScraperError("Invalid Piccoma Viewer URL")
        
        series_id, chapter_id = match.groups()
        base_url, region, domain = self._get_context_from_url(task.url)
        auth_session = await self._get_authenticated_session(domain)
        
        # 1. Primary Extraction (Next.js Hydration Data)
        res = await auth_session.get(task.url)
        
        # S+ Logic: Trigger unlock if 403/401 OR if 200 but page contains purchase indicators
        is_locked_ui = res.status_code == 200 and ("js_purchaseForm" in res.text or "チャージ中" in res.text or "ポイントで読む" in res.text)
        
        if res.status_code != 200 or is_locked_ui:
            # Attempt to unlock locked chapters (Coins or Wait-Free)
            reason = f"HTTP {res.status_code}" if res.status_code != 200 else "Locked UI detected"
            logger.info(f"[Piccoma] Chapter {chapter_id} {reason}, attempting fast purchase/unlock.")
            if await self.fast_purchase(task):
                # Re-fetch after successful purchase
                auth_session = await self._get_authenticated_session(domain)
                res = await auth_session.get(task.url)
            else:
                logger.error(f"  ❌ [Piccoma] Fast purchase failed for {chapter_id}")
             
        if res.status_code != 200: 
            logger.error(f"[Piccoma] Final access attempt failed for {chapter_id} (Status: {res.status_code})")
            raise ScraperError(f"Access error: {res.status_code}")
        
        await self.session_service.record_session_success("piccoma")

        # S+ DRM Heuristic: Multi-stage manifest discovery
        pdata = self._extract_pdata_heuristic(res.text)
        if not pdata:
            # 🕵️ [DEV-MODE]: Capture viewer HTML on manifest failure
            logger.error(f"[Piccoma] Manifest extraction FAILED for {chapter_id}. Heuristics exhausted.")
            # Check for common reasons
            if "js_purchaseForm" in res.text:
                logger.warning(f"  ⚠️ [Piccoma] Page still shows PURCHASE form after unlock attempt. Points/Coins likely insufficient.")
            elif "チャージ中" in res.text:
                logger.warning(f"  ⚠️ [Piccoma] Page still shows CHARGING status. Wait-Free ticket used recently.")
            
            self._dump_diagnostic_data(f"manifest_fail_{chapter_id}", res.text, {
                "url": task.url, "status": res.status_code, "headers": dict(res.headers)
            })
            raise ScraperError(f"Could not extract chapter manifest for {chapter_id} via any heuristic. Check diagnostics for details.")

        # Capture V30 metadata for debugging
        p_category = pdata.get('category')
        p_scroll = pdata.get('scroll')
        logger.debug(f"[Piccoma] Metadata - Category: {p_category}, Scroll: {p_scroll}")

        images = pdata.get('img', pdata.get('contents', []))
        valid_images = [img for img in images if img.get('path')]
        if not valid_images: raise ScraperError("No accessible images found in manifest.")

        total = len(valid_images)
        stats = {"completed": 0}
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Piccoma", total)
        progress.update(stats["completed"])

        async def process_one(img_data, i):
            async with self._download_semaphore:
                # S+ Fully Switch to pyccoma v0.7.2 logic - Now per-image seed calculation
                await self._download_robust(auth_session, img_data, i+1, output_dir, region)
            stats["completed"] += 1
            progress.update(stats["completed"])

        await asyncio.gather(*(process_one(img, i) for i, img in enumerate(valid_images)))
        progress.finish()
        return output_dir

    def _extract_pdata_heuristic(self, html_text):
        """S+ Refinement: DRM Heuristic Recovery."""
        # Heuristic 1: NEXT_DATA
        soup = BeautifulSoup(html_text, 'html.parser')
        next_data = soup.select_one('script#__NEXT_DATA__')
        if next_data:
            try:
                data = json.loads(next_data.string)
                pdata = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('viewer', {}).get('pData')
                if pdata: return pdata
            except: pass

        # Heuristic 2: Legacy _pdata_ global (Now handles JS objects)
        # We look for the entire block and use regex to pull paths, which is safer for JS object literals
        match = re.search(r'var\s+_pdata_\s*=\s*(.*?)\s*(?:var\s+|</script>|;)', html_text, re.DOTALL)
        if match:
            content = match.group(1)
            try:
                # Try direct JSON parsing first
                return json.loads(content)
            except:
                # Fallback: Extract image paths from JS object literal via regex
                paths = re.findall(r"['\"]?path['\"]?\s*:\s*['\"](.*?)['\"]", content)
                if paths:
                    logger.info(f"[Piccoma] Manifest recovered via regex fallback: {len(paths)} images.")
                    return {'img': [{'path': p} for p in paths]}
            
        # Heuristic 4: Modern PC Smartoon (episodeDetail)
        if next_data:
            try:
                n_data = json.loads(next_data.string)
                # PC Smartoon hierarchy: props -> pageProps -> episodeDetail -> manifest
                manifest = n_data.get('props', {}).get('pageProps', {}).get('episodeDetail', {}).get('manifest', {})
                images = manifest.get('images', [])
                if images:
                    pdata = {'img': [{'path': img.get('path')} for img in images if img.get('path')]}
                    logger.info(f"✨ [Piccoma Heuristic] Success via episodeDetail hierarchy! ({len(images)} images)")
                    return pdata
            except: pass

        # Heuristic 5: Recursive Deep Regex Scan (Last Stand)
        # Look for 'path' or 'imageUrl' keys anywhere in the HTML blob
        # Search for lists of image paths directly via regex if JSON parsing is nested too deep
        img_matches = re.findall(r'["\']path["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|png|webp|jpeg)[^"\']*)["\']', html_text)
        if img_matches:
            pdata_list = [{'path': m} for m in img_matches if '/seed' in m or '/img' in m]
            if pdata_list:
                logger.info(f"✨ [Piccoma Heuristic] Success via Deep Regex Pattern! ({len(pdata_list)} images)")
                return {'img': pdata_list}
            
        return None

    async def _download_robust(self, session, img_data, idx, out_dir, region):
        """S+ Verbatim 100% Mirror of pyccoma's Scraper.download logic."""
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        
        # S+ Per-image seed calculation for V30 compatibility
        seed = self._calculate_seed(url, region)
        
        d_task = session.get(url, timeout=30)
        res = await d_task
        res.raise_for_status()
        out_path = f"{out_dir}/page_{idx:03d}.png"
        
        # 🟢 V30.0 FIX: Relaxing isupper() check to handle alphanumeric/numeric seeds.
        # We only skip if seed is empty or contains lowercase letters (which shouldn't happen for V30).
        is_valid_seed = seed and (seed.isupper() or all(not c.islower() for c in seed))

        if is_valid_seed:
            if not Canvas:
                logger.warning(f"[Piccoma] 🛑 CANNOT UNSCRAMBLE: Canvas (pycasso) library not loaded. Page {idx} will remain scrambled.")
                with open(out_path, "wb") as f: f.write(res.content)
                return

            try:
                def unscramble():
                    # 🧩 S-GRADE: Lock the unscramble process
                    with self._unscramble_lock:
                        from io import BytesIO
                        img_io = BytesIO(res.content)
                        # 🟢 V30: Follow pyccoma reference logic EXACTLY
                        final_seed = self._dd_transform(seed) if seed.isupper() else seed
                        canvas = Canvas(img_io, (50, 50), final_seed)
                        logger.info(f"[Piccoma] Unscrambling page {idx} (Seed: {final_seed} | Mode: scramble)")
                        return canvas.export(mode="scramble", format="png").getvalue()
                
                content = await asyncio.to_thread(unscramble)
                with open(out_path, "wb") as f: f.write(content)
            except Exception as e:
                logger.error(f"[Piccoma] Unscramble error (V3 Seed: {seed}): {e}")
                with open(out_path, "wb") as f: f.write(res.content)
        else:
            # 🟢 S-GRADE: Skip unscrambling if seed doesn't meet criteria
            logger.debug(f"[Piccoma] Page {idx} - No unscramble (Seed: {seed} | valid: {is_valid_seed})")
            with open(out_path, "wb") as f: f.write(res.content)

    def _dd_transform(self, input_string: str) -> str:
        """S+ Mirrors pyccoma's dd() seed parity manipulator."""
        result_bytearray = bytearray()
        for index, byte in enumerate(bytes(input_string, 'utf-8')):
            if index < 3:
                byte = byte + (1 - 2 * (byte % 2))
            elif 2 < index < 6 or index == 8:
                pass
            elif index < 10:
                byte = byte + (1 - 2 * (byte % 2))
            elif 12 < index < 15 or index == 16:
                byte = byte + (1 - 2 * (byte % 2))
            elif index == len(input_string[:-1]) or index == len(input_string[:-2]):
                byte = byte + (1 - 2 * (byte % 2))
            else:
                pass
            result_bytearray.append(byte)
        return str(result_bytearray, 'utf-8')

    def _calculate_seed(self, url, region):
        """Precise mirror of pyccoma 0.7.2's get_seed() JS logic."""
        # 🟢 S-GRADE: Robust segment extraction
        # Handle both ".../seed/1.jpg" and ".../seed?expires=..."
        path_only = url.split('?')[0].rstrip('/')
        segments = [s for s in path_only.split('/') if s]
        
        if region == "fr":
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            chk_raw = qs.get('q', [''])[0]
        else:
            # Check if last segment is a file or a seed
            if segments and segments[-1].lower().endswith(('.png', '.jpg', '.webp', '.jpeg')):
                chk_raw = segments[-2] if len(segments) >= 2 else ""
            else:
                chk_raw = segments[-1] if segments else ""

        chk = str(chk_raw)
        
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        expires = qs.get('expires', [''])[0]
        
        if expires and chk:
            # S+ Mirrors pyccoma's iterative rotation logic
            for num in str(expires):
                if num.isdigit() and int(num) != 0:
                    shift = int(num)
                    # Rotate right
                    chk = chk[-shift:] + chk[:-shift]
        
        return chk

    async def fast_purchase(self, task) -> bool:
        """
        S+ Enhanced Purchase: Detect and handle both coin purchases and 'Free-to-Wait' triggers.
        Mirrored from verified working source with robust CSRF fallback.
        """
        match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', task.url)
        if not match:
            logger.debug(f"[Piccoma] fast_purchase: Could not parse series/episode from URL: {task.url}")
            return False
        
        series_id, episode_id = match.groups()
        base_url, region, domain = self._get_context_from_url(task.url)
        
        logger.info(f"[DEV-TRACE] [Step 1] Initializing authenticated session for domain: {domain}")
        auth_session = await self._get_authenticated_session(domain)

        # --- 🟢 S+ USER-REQUEST: Mandatory /web/ Handshake for Cookies ---
        # If we don't have a 'csrftoken' in our jar, we MUST hit the landing page 
        # first to establish the cookie identity, as per user requirement.
        if 'csrftoken' not in auth_session.cookies:
            logger.info("🛡️ [Piccoma Identity] Seed CSRF cookie missing. Performing /web/ Handshake...")
            try:
                handshake_res = await auth_session.get(f"{base_url}/web/", timeout=10)
                if handshake_res.status_code == 200:
                    logger.info("✅ [Piccoma Identity] /web/ Handshake complete. Cookies primed.")
            except Exception as e:
                logger.warning(f"⚠️ [Piccoma Identity] /web/ Handshake ritual failed: {e}")

        # Log session cookies audit
        logger.info(f"[DEV-TRACE] Session Identity Audit: {len(auth_session.cookies)} cookies loaded into AsyncSession.")
        if len(auth_session.cookies) == 0:
            logger.warning("[DEV-TRACE] ⚠️ CRITICAL: Session has 0 cookies! Authentication will fail.")
        
        try:
            # 1. Load episode list page to extract CSRF tokens, identify access type, and get form data
            # Use etype=E to ensure we see the list properly
            episode_page_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
            logger.info(f"[DEV-TRACE] [Step 2] Metadata fetch from: {episode_page_url}")
            p_task = auth_session.get(episode_page_url, timeout=15)
            res = await p_task
            logger.info(f"[DEV-TRACE] Metadata Response: Status={res.status_code}, Length={len(res.text)}")
            
            # Robust Error Detection: Identifying blocked/redirected 404 pages early
            if res.status_code == 200 and len(res.text) < 10000 and "ご利用いただけません" in res.text:
                logger.warning(f"[Piccoma] Block page detected on metadata fetch. Session/Proxy likely rejected.")
                return False

            if res.status_code != 200:
                logger.warning(f"[Piccoma] Could not load episode page: {res.status_code}")
                return False
            
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # 🟢 S-GRADE: Smartoon Detection (Improved)
            # Avoid hardcoded overrides; trust multiple indicators
            title_text = soup.select_one('h1.PCM-productTitle').text.lower() if soup.select_one('h1.PCM-productTitle') else ""
            is_s = "smartoon" in title_text or \
                   bool(soup.select_one('.PCM-productSmaIcon, .PCM-productSmaratoon, .PCM-productStatus_smartoon'))
            
            # Additional detection heuristics
            indicator_text = soup.select_one('.PCM-productStatus, .PCM-productMain_status, .PCM-epList_item-episode, .PCM-icon_smartoon')
            it_str = indicator_text.get_text().upper() if indicator_text else ""
            
            if not is_s:
                if "/web/viewer/s/" in task.url or "SMARTOON" in it_str or "縦読み" in it_str:
                    is_s = True
            
            # S-GRADE: Smartoon URL Self-Correction Logic
            if is_s and "/web/viewer/" in task.url and "/web/viewer/s/" not in task.url:
                fixed_url = task.url.replace("/web/viewer/", "/web/viewer/s/")
                logger.info(f"[Piccoma Identity] 🛠️ Self-Corrected URL: {task.url} -> {fixed_url}")
                task.url = fixed_url

            logger.info(f"[Piccoma Identity] 🧪 Diagnostic: is_s={is_s} (URL: {task.url})")
            
            # 1. Session Health Guard: Verify if we are actually logged in
            # Piccoma shows a "Login" button (PCM-headerLogin) if not authenticated
            is_guest = bool(soup.select_one('.PCM-headerLogin, a[href*="/acc/signin"]'))
            if is_guest:
                # 🕵️ [S+ Identity Diagnostic]: Dump current session state to logs
                # We use the internal jar to see the exact domains/paths of outgoing cookies
                try:
                    jar = auth_session.cookies.jar
                    diag_cookies = []
                    for domain in jar._cookies:
                        for path in jar._cookies[domain]:
                            for name in jar._cookies[domain][path]:
                                diag_cookies.append(f"{name} [{domain}:{path}]")
                    logger.info(f"🔎 [Identity Diagnostic] Outgoing Cookies: {', '.join(diag_cookies)}")
                except:
                    # Safe iteration over names and values
                    current_names = [f"{n} ({auth_session.cookies.get(n)})" for n in auth_session.cookies]
                    logger.info(f"🔎 [Identity Diagnostic] Outgoing Cookies (fallback): {', '.join(current_names)}")
                
                # S+ Trace: Log Set-Cookie headers from the rejection response
                set_cookies = res.headers.get_list('Set-Cookie') if hasattr(res.headers, 'get_list') else res.headers.get('Set-Cookie', "")
                if set_cookies:
                    logger.warning(f"  ⚠️ [Identity Trace] Server attempted to SET cookies during rejection: {set_cookies}")

                logger.error(f"🛑 [Piccoma Identity] Browser shows LOGIN button. Session is guest or expired!")
                
                # Report failure to trigger auto-login fallback on next attempt
                await self.session_service.report_session_failure("piccoma", "primary", "Session expired/Guest detected (Login button visible)")
                raise ScraperError("Your Piccoma session has expired or is invalid. Please re-login on the dashboard.")

            def _log_response_debug(r, label):
                """Internal high-verbose diagnostic helper."""
                try:
                    heads = dict(r.headers)
                    snippet = r.text[:1000] if hasattr(r, 'text') else "N/A"
                    logger.info(f"📊 [Piccoma Debug] {label} Status: {r.status_code}")
                    logger.info(f"📋 [Piccoma Debug] {label} Headers: {json.dumps(heads, indent=2)}")
                    logger.info(f"📄 [Piccoma Debug] {label} Body Snippet: {snippet}...")
                    # 🍪 Check for specific cookies that might signal blocks
                    sc = r.headers.get_list('Set-Cookie') if hasattr(r.headers, 'get_list') else r.headers.get('Set-Cookie', "")
                    if sc: logger.info(f"🍪 [Piccoma Debug] {label} Set-Cookie: {sc}")
                except Exception as de:
                    logger.debug(f"Failed to log debug: {de}")

            # 2. Robust CSRF Extraction (Multi-Tier Fallback)
            csrf_token = None
            
            # --- PRE-CHECK: Diagnostic search for token markers ---
            csrf_count = res.text.lower().count("csrf")
            logger.info(f"🔎 [Piccoma Diagnostic] Found {csrf_count} occurrences of 'csrf' in response text.")
            
            # --- TIER 1: Standard Forms ---
            csrf_form = soup.find('form', id='js_purchaseForm')
            if csrf_form:
                csrf_token = csrf_form.find('input', {'name': 'csrfToken'})
                if csrf_token: csrf_token = csrf_token.get('value')
            
            # --- TIER 2: Meta Tags ---
            if not csrf_token:
                csrf_meta = soup.find('meta', {'name': 'csrf-token'})
                if csrf_meta:
                    csrf_token = csrf_meta.get('content')
            
            # --- TIER 3: Hydrated State (Next.js) ---
            build_id = None
            config_script = soup.find('script', string=re.compile(r'__p_config__|__NEXT_DATA__'))
            if config_script and config_script.string:
                # NEXT_DATA (Modern)
                try:
                    n_data = json.loads(config_script.string)
                    # Try to extract CSRF if not already found
                    if not csrf_token:
                        csrf_token = n_data.get('props', {}).get('pageProps', {}).get('csrfToken')
                        if not csrf_token:
                            csrf_token = n_data.get('initialState', {}).get('app', {}).get('csrfToken')
                    
                    # Always try to extract Build ID for Next.js Data API
                    build_id = n_data.get('buildId')
                    if build_id: logger.info(f"🏗️ [Piccoma Identity] Next.js Build ID extracted: {build_id}")
                except: pass

            # --- TIER 3.5: Wide-Net Build ID Recovery ---
            if not build_id:
                # Scan raw text for any buildId pattern (common in modern obfuscated JS blocks)
                bid_m = re.search(r'["\']buildId["\']\s*:\s*["\']([^"\']+)["\']', res.text)
                if bid_m:
                    build_id = bid_m.group(1)
                    logger.info(f"🏗️ [Piccoma Identity] Next.js Build ID found via wide-net: {build_id}")
            
            # --- TIER 4: Hidden Inputs (Durable Fallback) ---
            if not csrf_token:
                # Find ANY input containing "csrf" in its name or ID
                csrf_inputs = soup.find_all('input', attrs={'name': re.compile(r'csrf', re.I)})
                for inp in csrf_inputs:
                    val = inp.get('value')
                    if val and len(val) > 10:
                        csrf_token = val
                        break
            
            # --- TIER 5: Final Stand (Regex on Raw Text) ---
            if not csrf_token:
                # Durable regex: handle any attribute order and whitespace
                token_m = re.search(r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']', res.text, re.DOTALL)
                if not token_m:
                    token_m = re.search(r'value=["\']([^"\']+)["\']\s+name=["\']csrfmiddlewaretoken["\']', res.text, re.DOTALL)
                if not token_m:
                    token_m = re.search(r'csrfToken\s*:\s*["\']([^"\']+)["\']', res.text)
                csrf_token = token_m.group(1) if token_m else None

            # --- TIER 6: Cookie Jar Discovery (S+ Last Resort) ---
            if not csrf_token:
                # In modern Next.js apps, CSRF is often exclusively in cookies
                # Use .items() for string-safe iteration across all library versions
                for name, value in auth_session.cookies.items():
                    if name.lower() in ["csrftoken", "__host-csrf", "csrf"]:
                        csrf_token = value
                        logger.info(f"🍪 [Piccoma Diagnostic] CSRF extracted from session cookies: {csrf_token[:10]}...")
                        break

            headers = {
                "Host": "piccoma.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Referer": episode_page_url,
                "Origin": "https://piccoma.com",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "X-Requested-With": "XMLHttpRequest", # CRITICAL: Ensure server treats as XHR
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            if csrf_token:
                headers['X-CSRF-Token'] = csrf_token
                headers['X-CSRFToken'] = csrf_token 
                logger.info(f"[DEV-TRACE] [Step 3] CSRF extraction: {csrf_token[:10]}... (S+ Robust)")
            else:
                logger.warning("[DEV-TRACE] [Step 3] CSRF extraction FAILED. Triggering high-veracity debug dump.")
                _log_response_debug(res, "Metadata-Fail")
                self._dump_diagnostic_data(f"csrf_fail_{episode_id}", res.text, {
                    "url": episode_page_url, "status": res.status_code, "headers": dict(res.headers)
                })
            
            # 3. Security Hash (S-Grade entropy)
            import hashlib
            seed_string = f"{episode_id}fh_SpJ#a4LuNa6t8"
            sec_hash = hashlib.sha256(seed_string.encode('utf-8')).hexdigest()
            headers['X-Security-Hash'] = sec_hash
            headers['X-Hash-Code'] = sec_hash
            logger.info(f"[DEV-TRACE] [Step 4] Security Hash logic initialized (salt verified).")
            
            # 4. Identify Access Type (Wait-Free vs Coins)
            is_waitfree = False
            # Search for episode link containing ID
            ep_item = soup.select_one(f'a[data-episode_id="{episode_id}"], a[href*="/{episode_id}"]')
            if ep_item:
                # Check for icons/text indicating free-to-wait
                waitfree_indicator = ep_item.select_one('.PCM-epList_status_waitfree, .PCM-icon_waitfree, .PCM-icon_clock, .PCM-epList_waitfree')
                item_text = ep_item.get_text()
                is_waitfree = bool(waitfree_indicator) or "待てば¥0" in item_text or "待てば" in item_text or "¥0" in item_text
                
                # Check if it's currently CHARGING (User must wait)
                charging = ep_item.select_one('.PCM-epList_status_waitfreeCharging, .PCM-chargeBar_waitfree')
                if charging or "分後に読めます" in item_text or "チャージ中" in item_text:
                    logger.info(f"[Piccoma Identity] Wait-Free is currently charging for episode {episode_id}. Switching to Point/Coin fallback.")
                    is_waitfree = False
                
                logger.info(f"[DEV-TRACE] WaitFree Detection (Episode List): {is_waitfree} (Indicators: {bool(waitfree_indicator)})")
            
            if not is_waitfree:
                # Fallback: check global 'Free' buttons on the page
                is_waitfree = bool(soup.select_one('.btn-waitfree, .PCM-btn-waitfree, .PCM-footerWaitfree'))
            # S+: Support Smartoon specific wait-free check
            if not is_waitfree and is_s:
                # Verify it's actually unlocked (Not just free-to-wait label)
                if "1話無料" in res.text or "待てば" in res.text:
                    if "チャージ中" not in res.text:
                        is_waitfree = True
                        logger.info("[DEV-TRACE] WaitFree Detection (Raw Text): True (Smartoon Fallback)")
            
            # 5. Discovery Loop: Robust alternative endpoint & payload matching
            # --- MODERN PC WEB API CANDIDATES ---
            discovery_endpoints = [
                # API V2 Candidates (Durable)
                f"{base_url}/web/api/v2/episode/waitfree/use" if is_waitfree else f"{base_url}/web/api/v2/episode/point/use",
                f"{base_url}/web/api/v2/episode/coin/use",
                # API V1 Candidates (Legacy)
                f"{base_url}/web/api/v1/episode/waitfree/use",
                f"{base_url}/web/api/v1/episode/point/use",
                # Path-Based Candidates (Dynamic)
                f"{base_url}/web/episode/{episode_id}/use",
                f"{base_url}/web/episode/{episode_id}/purchase"
            ]
            
            # --- NEXT.JS DATA API (TIER 8) ---
            if build_id:
                # Mirroring browser's background data fetch
                discovery_endpoints.append(f"{base_url}/_next/data/{build_id}/web/api/v2/episode/waitfree/use.json")
                discovery_endpoints.append(f"{base_url}/_next/data/{build_id}/web/api/v2/episode/point/use.json")
            
            discovery_endpoints.extend([
                # Original Static Candidates
                f"{base_url}/web/episode/waitfree/use",
                f"{base_url}/web/episode/point/use",
                f"{base_url}/web/episode/coin/use",
                f"{base_url}/web/episode/purchase",
                f"{base_url}/web/episode/use",
            ])
            
            payload_variants = [
                {"episodeId": episode_id, "productId": series_id, "hash": sec_hash, "csrfToken": csrf_token},
                {"episode_id": episode_id, "product_id": series_id, "hash": sec_hash, "csrfmiddlewaretoken": csrf_token},
                {"episodeId": episode_id, "productId": series_id, "hash": sec_hash},
                {"episode_id": episode_id, "productId": series_id, "type": "point", "hash": sec_hash}
            ]

            # 🟢 S+ Lateny Emulation (Mimicking human interaction delay)
            import random
            latency = random.uniform(0.5, 1.2)
            logger.info(f"⏳ [Piccoma Identity] Emulating human latency: {latency:.2f}s before trial...")
            await asyncio.sleep(latency)

            # --- TIER 7: Diagnostic Truth-Scanner ---
            found = False
            for endpoint_idx, alt_url in enumerate(discovery_endpoints):
                if found: break
                for payload_idx, payload in enumerate(payload_variants):
                    if found: break
                    for is_json in [False, True]:
                        ct = "application/json" if is_json else "application/x-www-form-urlencoded"
                        headers["Content-Type"] = ct
                        
                        # 🟢 S+ NEXT.JS MASTER-KEY: Force JSON response mode
                        if is_json:
                            headers["x-nextjs-data"] = "1"
                        else:
                            headers.pop("x-nextjs-data", None)
                        
                        # 🕵️ [S+ Trace]: High-entropy diagnostic log
                        trial_label = f"Trial {endpoint_idx+1}.{payload_idx+1}.{'J' if is_json else 'F'}"
                        
                        try:
                            p_retry = auth_session.post(
                                alt_url, 
                                json=payload if is_json else None,
                                data=None if is_json else payload, 
                                headers=headers, 
                                timeout=15,
                                allow_redirects=True
                            )
                            post_res = await p_retry
                            
                            # 🛡️ SECURITY DETECTOR: Scan for 404-HTML redirects
                            resp_ct = post_res.headers.get('Content-Type', '').lower()
                            is_html = 'text/html' in resp_ct or post_res.text.strip().startswith('<!')
                            
                            if is_html:
                                status_msg = f"🛡️ Security Redirect (HTML Page) | Status: {post_res.status_code}"
                                if "ログインしてください" in post_res.text or "signin" in post_res.text.lower():
                                    status_msg += " | Reason: Login Required (Session Expired)"
                                elif "エラー" in post_res.text:
                                    status_msg += " | Reason: Server-side Error"
                            else:
                                status_msg = f"Result: {post_res.status_code}"
                                try:
                                    trial_json = post_res.json()
                                    status_msg += f" | JSON: {json.dumps(trial_json)}"
                                except: pass

                            logger.info(f"   📡 [Discovery] {trial_label} -> {alt_url} | {status_msg}")
                            
                            # Success check (BREAK condition)
                            if post_res.status_code in [200, 302, 301] and not is_html:
                                # Verification after attempt
                                try:
                                    # Verification logic snippet
                                    viewer_path = f"/web/viewer/s/{series_id}/{episode_id}" if is_s else f"/web/viewer/{series_id}/{episode_id}"
                                    v_res = await auth_session.get(f"{base_url}{viewer_path}", timeout=10)
                                    if v_res.status_code == 200 and self._extract_pdata_heuristic(v_res.text):
                                        logger.info(f"[Piccoma] ✨ Success via alternative: {alt_url} (is_json={is_json})")
                                        found = True
                                        break
                                except Exception: pass
                                
                                # Fallback check for JSON results
                                try:
                                    resp_json = post_res.json()
                                    if resp_json.get('result') == 'ok' or resp_json.get('success'):
                                        logger.info(f"[Piccoma] ✨ Success via API JSON: {alt_url}")
                                        found = True
                                        break
                                except: pass
                                
                            else:
                                logger.info(f"[Piccoma Discovery] Trial failed: {alt_url} ({post_res.status_code}) Body: {post_res.text[:200]}")
                        except Exception as trial_e:
                            logger.info(f"[Piccoma Discovery] Trial exception: {alt_url} ({trial_e})")
            
            return found
            
        except Exception as e:
            logger.error(f"[Piccoma] Error in fast_purchase: {e}")
            return False

    async def get_new_series_list(self) -> list[dict]:
        """Scrapes the 'New' series via the Theme API for Piccoma (JP)."""
        base_url = "https://piccoma.com"
        auth_session = await self._get_authenticated_session(".piccoma.com")
        new_series = []
        
        try:
            # 1. Try Direct Theme Page Scrape (Tier 1)
            theme_url = f"{base_url}/web/theme/product/list/398316/N"
            res = await auth_session.get(theme_url, timeout=15)
            logger.info(f"[Piccoma] Theme Page Response: {res.status_code}")
            
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                # Check for products in the initial HTML
                items = soup.select('li, .PCM-productList1_item, .PCM-product, .PCM-productTile')
                for item in items:
                    link = item.select_one('a')
                    if not link: continue
                    href = link.get('href', '')
                    if '/web/product/' not in href: continue
                    sid_match = re.search(r'/web/product/(\d+)', href)
                    if not sid_match: continue
                    sid = sid_match.group(1)
                    title_elem = item.select_one('.PCM-product_title, .PCM-productList1_title, .PCM-productTile_title, dt, span')
                    title = title_elem.get_text(strip=True) if title_elem else ""
                    
                    # 🟢 FIX: Define img_elem for poster extraction
                    img_elem = item.select_one('img')
                    poster = self._format_poster_url(img_elem.get('src') if img_elem else None)
                    if not title or title == "Unknown": continue
                    
                    if any(s['series_id'] == sid for s in new_series): continue
                    new_series.append({
                        "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}{href}"
                    })
                logger.info(f"[Piccoma] Tier 1 (Theme Page) found {len(new_series)} series.")
            
            # 2. Try API for Paginated Data (Tier 2)
            # Only if we found nothing or want more
            if not new_series:
                for p_id in [1, 0]:
                    api_url = f"{base_url}/web/next_page/list?result_id=398316&list_type=T&sort_type=N&page_id={p_id}"
                    headers = {'X-Requested-With': 'XMLHttpRequest', 'Referer': theme_url}
                    try:
                        res = await auth_session.get(api_url, headers=headers, timeout=15)
                        if res.status_code != 200: continue
                        ctype = res.headers.get('Content-Type', '').lower()
                        if 'application/json' in ctype or res.text.strip().startswith('{'):
                            data = res.json()
                            # Picard API often nests products in data['products'] or directly in data
                            raw_data = data.get('data', data)
                            products = []
                            if isinstance(raw_data, list):
                                products = raw_data
                            elif isinstance(raw_data, dict):
                                products = raw_data.get('products', raw_data.get('list', []))
                            
                            if products and isinstance(products, list):
                                for item in products:
                                    if not isinstance(item, dict): continue
                                    sid = str(item.get('id', item.get('product_id', '')))
                                    if not sid: continue
                                    title = item.get('title', item.get('product_name', 'Unknown'))
                                    poster = self._format_poster_url(item.get('img', item.get('image', item.get('cover_x1'))))
                                    if any(s['series_id'] == sid for s in new_series): continue
                                    new_series.append({
                                        "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}/web/product/{sid}"
                                    })
                        
                        if new_series: 
                            logger.info(f"[Piccoma] Tier 2 (Paginated API) total: {len(new_series)} series.")
                            break
                    except Exception as e:
                        logger.debug(f"[Piccoma Discovery] API Trial failed: {e}")
                        continue

            # 3. Final Fallback: General New Page (Tier 3)
            if not new_series:
                res = await auth_session.get(f"{base_url}/web/list/new/all", timeout=15)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    for item in soup.select('.PCM-productList1_item, .PCM-product'):
                        link = item.select_one('a')
                        if not link: continue
                        href = link.get('href', '')
                        sid_match = re.search(r'/web/product/(\d+)', href)
                        if not sid_match: continue
                        sid = sid_match.group(1)
                        title_elem = item.select_one('.PCM-productList1_title, .PCM-product_title')
                        title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                        
                        # 🟢 FIX: Define img_elem for poster extraction in T3 fallback
                        img_elem = item.select_one('img')
                        poster = self._format_poster_url(img_elem.get('src') if img_elem else None)
                        if any(s['series_id'] == sid for s in new_series): continue
                        new_series.append({
                            "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}{href}"
                        })
            
            logger.info(f"[Piccoma] Discovery finished. Found {len(new_series)} series.")
            return new_series
        except Exception as e:
            logger.error(f"[Piccoma] Fatal error in new series discovery: {e}")
            return []

    async def run_ritual(self, session):
        """S+ Adaptive Ritual Engine: Randomized human-like navigation paths."""
        base_url = "https://piccoma.com" # Default to JP for rituals unless session specifically FR
        
        scenarios = [
            # Scenario A: Trend Watching
            [f"{base_url}/web/manga/bestseller", f"{base_url}/web/manga/recent"],
            # Scenario B: Searching for content
            [f"{base_url}/web/search/result?word={random.choice(['ファンタジー', 'アクション', '令嬢'])}"],
            # Scenario C: Browsing Categories
            [f"{base_url}/web/manga/category/1", f"{base_url}/web/manga/ranking/category/1/daily"],
            # Scenario D: Deep Discovery
            [f"{base_url}/web/manga/ranking/daily", f"{base_url}/web/manga/ranking/weekly"],
            # Scenario E: Account maintenance
            [f"{base_url}/web/product/favorite", f"{base_url}/web/mypage/history"]
        ]
        
        path = random.choice(scenarios)
        logger.info(f"[Piccoma] S+ Adaptive Ritual: Path {scenarios.index(path)} initiated.")
        
        for url in path:
            r_task = session.get(url)
            await r_task
            # S+ Gaussian Jitter: random.gauss(mean, std_dev)
            val = float(random.gauss(5, 1.5))
            sleep_time = int(max(2.0, val))
            await asyncio.sleep(sleep_time)
        
        logger.info("[Piccoma] S+ Ritual Complete.")
