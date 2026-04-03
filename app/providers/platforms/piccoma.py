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
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
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
    
    # S-GRADE: Thread-safe lock to prevent pycasso's global state race condition
    _unscramble_lock = threading.Lock()

    def __init__(self):
        self.session_service = SessionService()
        
        self.default_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        # S-Grade Backpressure
        self._download_semaphore = asyncio.Semaphore(10)

    def _get_context_from_url(self, url: str):
        """S+ Refinement: Stateless context derivation."""
        if "fr.piccoma" in url or "/fr" in url:
            return "https://fr.piccoma.com", "fr", ".fr.piccoma.com"
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
            logger.debug(f"[Piccoma] Applying {len(session_obj['cookies'])} cookies for domain {region_domain}")
            for c in session_obj["cookies"]:
                name, value = c.get('name'), c.get('value')
                if name and value: 
                    # S+ Refinement: Use original metadata (domain/path) if available for cross-subdomain compatibility.
                    # This is critical for authentication handshakes on Piccoma subdomains.
                    c_domain = c.get('domain') or region_domain
                    c_path = c.get('path') or "/"
                    async_session.cookies.set(name, value, domain=c_domain, path=c_path)
        else:
            # S-GRADE: Explicitly fail if no session is available
            raise ScraperError("No healthy sessions available for piccoma. Use /add-cookies to fix.")
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        """Stateless validation: Check if redirected on the current session's base."""
        try:
            # We use a neutral endpoint. 
            # Note: In a stateless world, we don't know the base_url yet, 
            # so we check the session's own cookie domains or use the JP default.
            base_url = "https://piccoma.com" 
            res = await session.get(f"{base_url}/web/product/favorite", timeout=15, allow_redirects=False)
            valid = res.status_code == 200
            if valid:
                await self.session_service.record_session_success("piccoma")
            return valid
        except Exception: return False

    async def get_series_info(self, url: str, fast: bool = False):
        """S+ Refinement: Pure function, deriving all context from URL."""
        match = re.search(r'/web/product/(\d+)', url)
        if not match: raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        base_url, region, domain = self._get_context_from_url(url)
            
        auth_session = await self._get_authenticated_session(domain)
        
        # 1. Fetch main product page for basic metadata & schedule
        t_get = auth_session.get(f"{base_url}/web/product/{series_id}")
        res = await t_get
        if res.status_code != 200: raise ScraperError(f"Failed to fetch series: {res.status_code}")
        
        # Geo-block detection: Piccoma shows a short page when accessed from outside Japan
        if len(res.text) < 10000 and ("日本国内でのみ" in res.text or "only be used from Japan" in res.text):
            raise ScraperError("Piccoma geo-blocked: This service can only be accessed from Japan. Use a Japan VPN or proxy.")
        
        await self.session_service.record_session_success("piccoma")
        soup = BeautifulSoup(res.text, 'html.parser')
        
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        # 🟢 FIX: Define thumb_img for poster extraction (handles new PCM-productThum_img typo)
        thumb_img = soup.select_one('.PCM-productThumb_img, .PCM-productThum_img, .PCM-productThumb img, .PCOM-productCover img')
        image_url = self._format_poster_url(thumb_img['src'] if thumb_img else None)

        # 2. Extract Release Day (V2 Feature)
        release_day = None
        release_time = None
        day_map = {
            "日曜": "Saturday", "月曜": "Sunday", "火曜": "Monday",
            "水曜": "Tuesday", "木曜": "Wednesday", "金曜": "Thursday",
            "土曜": "Friday"
        }

        status_label = None
        status_items = soup.select('ul.PCM-productStatus li')
        for li in status_items:
            text = li.get_text(strip=True)
            # S-GRADE: Status Detection (Mar 25 Request)
            if "完結" in text:
                status_label = "Completed"
            
            # Release Day Extraction
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day = en_day
                    release_time = "15:00" # Midnight JST = 15:00 UTC
                    break

        # 🟢 S-GRADE: Smartoon Detection (Precision fix for series like 170586 and 200519)
        # Search title, standard icons, and also the etype indicator in current URL if known
        is_smartoon = "smartoon" in title.lower() or bool(soup.select_one('.PCM-productSmaIcon, .PCM-productSmaratoon, .PCM-productStatus_smartoon'))
        
        # Fallback: Look for Smartoon-only keywords or vertical scroll indicators in the status list
        if not is_smartoon:
            indicator_text = soup.select_one('.PCM-productStatus, .PCM-productMain_status, .PCM-productStatus_item')
            it_str = indicator_text.get_text().upper() if indicator_text else ""
            if "縦読み" in it_str or "SMARTOON" in it_str:
                is_smartoon = True
            elif "ETYPE" in url.upper():
                is_smartoon = True
        
        task_viewer_prefix = f"{base_url}/web/viewer" + ("/s" if is_smartoon else "")
        logger.info(f"[Piccoma] Series '{title}' (ID: {series_id}) - Smartoon: {is_smartoon}")

        # 🟢 S-GRADE: FAST LOADING SUPPORT
        # If fast=True, we parse whatever episodes are already on the landing page (if any)
        # Usually Piccoma has a few episodes on the product page, but for deep list we go to /episodes
        all_chapters = []
        if fast:
            logger.info(f"[Piccoma] Fast Fetch initiated for: {title}")
            # Try to find episodes on the landing page itself
            landing_items = soup.select('ul.PCM-epList li, div.PCM-epList_item')
            for item in landing_items:
                link = item.select_one('a')
                if not link: continue
                href = link.get('href', '')
                cid = link.get('data-episode_id')
                if not cid:
                    m = re.search(r'/web/viewer/(?:s/)?\d+/(\d+)', href)
                    cid = m.group(1) if m else None
                
                if not cid: continue
                
                title_tag = item.select_one('p.PCM-epList_title, .PCM-epList_title')
                c_title = title_tag.get_text(strip=True) if title_tag else f"Episode {cid}"
                notation = c_title # For Piccoma, notation is typically the title (e.g. 第1話)
                
                # Lock status check for new layout
                is_locked = bool(item.select_one('span.PCM-epList_lock, .PCM-icon_lock, .PCM-epList_status_waitfree'))
                if not is_locked:
                    is_locked = "待てば￥0" not in item.get_text() and "無料" not in item.get_text() and "¥0" not in item.get_text()
                
                all_chapters.append({
                    'id': cid, 'title': c_title, 'notation': notation, 'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                    'is_locked': is_locked, 'is_new': "NEW" in item.get_text().upper()
                })
            
            # If we found something, return immediately
            if all_chapters:
                try: all_chapters.sort(key=lambda x: int(x['id']))
                except: pass
                return title, len(all_chapters), all_chapters, image_url, str(series_id), release_day, release_time, status_label, None

        # 3. Fetch episodes page specifically (Full Load)
        episodes_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
        t_ep = auth_session.get(episodes_url)
        ep_res = await t_ep
        
        if ep_res.status_code != 200:
            raise ScraperError(f"Failed to fetch Piccoma episodes: HTTP {ep_res.status_code}. Session might be invalid.")
            
        if ep_res.status_code == 200:
            ep_soup = BeautifulSoup(ep_res.text, 'html.parser')
            
            # Heuristic 1: Extract from HTML list
            ep_items = ep_soup.select('ul.PCM-epList li, div.PCM-epList_item, li[class*="PCM-epList"]')
            
            for item in ep_items:
                link = item.select_one('a')
                if not link: continue
                
                href = link.get('href', '')
                # Viewer URL format: /web/viewer/s/{series_id}/{chapter_id} or /web/viewer/{series_id}/{chapter_id}
                cid = link.get('data-episode_id')
                if not cid:
                    m = re.search(r'/web/viewer/(?:s/)?\d+/(\d+)', href)
                    cid = m.group(1) if m else None
                
                if not cid: continue
                
                # 🟢 SMART-OON detection (Crucial for URL construction & payload)
                # Scroll-based webtoons (Smartoons) often have PCM-epList_item-episode while paged manga has PCM-epList_item-volume
                is_smartoon_item = bool(item.select_one('.PCM-epList_item-episode')) or "/s/" in href
                
                # Title extraction
                title_tag = item.select_one('p.PCM-epList_title, span.PCM-epList_title, .PCM-epList_title')
                c_title = title_tag.get_text(strip=True) if title_tag else link.get_text(strip=True).split('\n')[0]
                notation = c_title # Typically 第1話
                
                # 🟢 Lock status & Wait-free indicator
                waitfree_indicator = item.select_one('.PCM-epList_status_waitfree, .PCM-icon_waitfree, .PCM-icon_clock')
                is_locked = bool(item.select_one('span.PCM-epList_lock, i.PCM-epList_lock_icon, .PCM-icon_lock')) or bool(waitfree_indicator)
                
                if not is_locked:
                    is_locked = "待てば￥0" not in item.get_text() and "無料" not in item.get_text() and "¥0" not in item.get_text()
                
                # Use /s/ prefix for Smartoons
                item_viewer_prefix = f"{base_url}/web/viewer/s" if is_smartoon_item else f"{base_url}/web/viewer"
                
                all_chapters.append({
                    'id': cid,
                    'title': c_title,
                    'notation': notation,
                    'url': f"{item_viewer_prefix}/{series_id}/{cid}",
                    'is_locked': is_locked,
                    'is_new': "NEW" in item.get_text().upper()
                })
            
            # Heuristic 2: If HTML parsing failed, try __NEXT_DATA__
            if not all_chapters:
                next_data_script = ep_soup.select_one('script#__NEXT_DATA__')
                if next_data_script:
                    try:
                        data = json.loads(next_data_script.string)
                        # Structure varies, but usually under props.pageProps.initialState.product.episodeList
                        ep_list = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('product', {}).get('episodeList', [])
                        for ep in ep_list:
                            cid = str(ep.get('id'))
                            title = ep.get('title', f"Episode {cid}")
                            all_chapters.append({
                                'id': cid,
                                'title': title,
                                'notation': title,
                                'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                                'is_locked': not ep.get('is_free', False),
                                'is_new': ep.get('is_new', False)
                            })
                    except: pass

        # Sort chapters by ID (usually ascending)
        try:
            all_chapters.sort(key=lambda x: int(x['id']))
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
        if res.status_code != 200:
            # Attempt to unlock locked chapters (Coins or Wait-Free)
            logger.info(f"[Piccoma] Chapter {chapter_id} locked (HTTP {res.status_code}), attempting fast purchase/unlock.")
            if await self.fast_purchase(task):
                # Re-fetch after successful purchase
                auth_session = await self._get_authenticated_session(domain)
                r_task = auth_session.get(task.url)
                res = await r_task
             
        if res.status_code != 200: raise ScraperError(f"Access error: {res.status_code}")
        await self.session_service.record_session_success("piccoma")

        # S+ DRM Heuristic: Multi-stage manifest discovery
        pdata = self._extract_pdata_heuristic(res.text)
        if not pdata: raise ScraperError("Could not extract chapter manifest via any heuristic.")

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

        # 🟢 FIX: Exactly match pyccoma's img_url rotation.
        chk = str(chk_raw)
        
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        expires = qs.get('expires', [''])[0]
        
        # 🟢 COMMUNICATION LOGS: Log the raw data flow for verification
        logger.info(f"[Piccoma V30 Debug] Segment Source: {path_only}")
        logger.info(f"[Piccoma V30 Debug] Raw Checksum Segment: {chk_raw}")
        logger.info(f"[Piccoma V30 Debug] Expiry Key: {expires}")
        
        if expires and chk:
            # S+ Mirrors pyccoma's iterative rotation logic
            for num in str(expires):
                if num.isdigit() and int(num) != 0:
                    shift = int(num)
                    # Rotate right
                    chk = chk[-shift:] + chk[:-shift]
                    
            logger.info(f"[Piccoma V30 Debug] Rotated Seed Result: {chk}")
            logger.info(f"[Piccoma V30 Debug] Transformed Seed (Ready): {self._dd_transform(chk)}")
        else:
            logger.warning(f"[Piccoma V30 Debug] Missing parameters for unscramble. Chk: {bool(chk)}, Expires: {bool(expires)}")
        
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
        auth_session = await self._get_authenticated_session(domain)
        
        try:
            # 1. Load episode list page to extract CSRF tokens, identify access type, and get form data
            # Use etype=E to ensure we see the list properly
            episode_page_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
            p_task = auth_session.get(episode_page_url, timeout=15)
            res = await p_task
            
            # Robust Error Detection: Identifying blocked/redirected 404 pages early
            if res.status_code == 200 and len(res.text) < 10000 and "ご利用いただけません" in res.text:
                logger.warning(f"[Piccoma] Block page detected on metadata fetch. Session/Proxy likely rejected.")
                return False

            if res.status_code != 200:
                logger.warning(f"[Piccoma] Could not load episode page: {res.status_code}")
                return False
            
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # 🟢 S-GRADE: Early Smartoon detection via HTML/URL for correct endpoint sync
            is_s = "/s/" in task.url.lower() or "smartoon" in str(soup).lower() or bool(soup.select_one('.PCM-productSmaIcon, .PCM-productSmaratoon, .PCM-productStatus_smartoon'))
            if not is_s:
                indicator_text = soup.select_one('.PCM-productStatus, .PCM-productMain_status, .PCM-productStatus_item')
                it_str = indicator_text.get_text().upper() if indicator_text else ""
                is_s = "縦読み" in it_str or "SMARTOON" in it_str or "ETYPE" in task.url.upper() or "/S/" in task.url.upper()
            
            # 2. Robust CSRF Extraction (Multi-Tier Fallback)
            csrf_token = None
            csrf_meta = soup.find('meta', {'name': 'csrf-token'})
            if csrf_meta:
                csrf_token = csrf_meta.get('content')
            
            if not csrf_token:
                config_script = soup.find('script', string=re.compile(r'__p_config__'))
                if config_script and config_script.string:
                    token_m = re.search(r'csrfToken\s*:\s*["\']([^"\']+)["\']', config_script.string)
                    csrf_token = token_m.group(1) if token_m else None
            
            headers = {
                "Referer": episode_page_url,
                "Origin": base_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            if csrf_token:
                headers['X-CSRF-Token'] = csrf_token
            
            # 3. Security Hash (S-Grade entropy)
            import hashlib
            seed_string = f"{episode_id}fh_SpJ#a4LuNa6t8"
            sec_hash = hashlib.sha256(seed_string.encode('utf-8')).hexdigest()
            headers['X-Security-Hash'] = sec_hash
            headers['X-Hash-Code'] = sec_hash
            
            # 4. Identify Access Type (Wait-Free vs Coins)
            is_waitfree = False
            ep_item = soup.select_one(f'a[data-episode_id="{episode_id}"]')
            if ep_item:
                waitfree_indicator = ep_item.select_one('.PCM-epList_status_waitfree, .PCM-icon_waitfree, .PCM-icon_clock')
                is_waitfree = bool(waitfree_indicator) or "待てば¥0" in ep_item.get_text() or "待てば" in ep_item.get_text()
            
            if not is_waitfree:
                is_waitfree = bool(soup.select_one('.btn-waitfree, .PCM-btn-waitfree'))
            
            # 5. Build Final Payload and Endpoint
            # S-GRADE: Build correctly targeted URL based on primary detection (Smartoon: {is_s})
            if is_waitfree:
                target_url = f"{base_url}/web/episode/{'s/' if is_s else ''}waitfree/use"
                logger.info(f"[Piccoma] ⏳ Detected 'Free to Wait' for episode {episode_id}... (Smartoon: {is_s})")
            else:
                target_url = f"{base_url}/web/episode/{'s/' if is_s else ''}purchase"
                logger.info(f"[Piccoma] 🪙 Detected coin purchase required for episode {episode_id}... (Smartoon: {is_s})")
                
            purchase_payload = {
                "episodeId": episode_id,
                "productId": series_id,
                "hash": sec_hash,
                "hashCode": sec_hash
            }
            
            # Extract additional hidden fields from purchase form if available
            purchase_form = soup.select_one('#js_purchaseForm, .js_purchaseForm, form[action*="purchase"]')
            if purchase_form:
                for hidden in purchase_form.find_all('input', type='hidden'):
                    name = hidden.get('name')
                    if name and name not in purchase_payload:
                        purchase_payload[name] = hidden.get('value', '')
            
            # 6. POST purchase/usage request
            post_task = auth_session.post(
                target_url, data=purchase_payload, headers=headers, timeout=15, allow_redirects=True
            )
            post_res = await post_task
            
            # 🟢 S-GRADE: 404 Recovery & Discovery Heuristic
            if post_res.status_code == 404:
                logger.error(f"[Piccoma] Primary endpoint ({target_url}) 404. Initiating discovery loop (Smartoon: {is_s}).")
                
                # S-GRADE: Trialing multiple endpoint signatures in priority order
                discovery_endpoints = []
                
                # 1. Trial Next.js JSON Data (Highest success rate)
                config_script = soup.find('script', string=re.compile(r'__p_config__|next_data'))
                build_id = None
                if config_script and config_script.string:
                    bid_m = re.search(r'buildId\s*:\s*["\']([^"\']+)["\']', config_script.string)
                    build_id = bid_m.group(1) if bid_m else None
                
                if build_id:
                    ext = ".json"
                    base_ps = ["/web/episode/waitfree/use", "/web/viewer/waitfree/push", "/web/episode/purchase"]
                    for bp in base_ps:
                        discovery_endpoints.append(f"{base_url}/_next/data/{build_id}{bp}{ext}")
                        discovery_endpoints.append(f"{base_url}/_next/data/{build_id}{bp.replace('/waitfree/', '/waitfree/s/')}{ext}")

                # 2. Traditional Endpoints (Unified List)
                prefixes = ["/web/viewer", "/web/episode"]
                middle_segments = ["/waitfree", "/purchase"]
                postfixes = ["/use", "/push", ""]
                
                for pref in prefixes:
                    for mid in middle_segments:
                        for post in postfixes:
                            # Try both standard and Smartoon (/s/) variants
                            discovery_endpoints.append(f"{base_url}{pref}{mid}{post}")
                            discovery_endpoints.append(f"{base_url}{pref}{mid}/s{post}")
                            discovery_endpoints.append(f"{base_url}{pref}/s{mid}{post}")
                
                # Cleanup and unique
                discovery_endpoints = list(dict.fromkeys(discovery_endpoints))
                
                # High-Entropy Payload Variants
                payload_variants = []
                for cid in ["episodeId", "episode_id"]:
                    for pid in ["productId", "product_id"]:
                        for conf in ["false", "true"]:
                            p = {
                                cid: episode_id,
                                pid: series_id,
                                "confirm": conf,
                                "hasWaitFree": "true" if is_waitfree else ("true" if conf == "true" else "false")
                            }
                            if is_waitfree: p["ticketType"] = "WAITFREE"
                            if p not in payload_variants: payload_variants.append(p)
                
                for alt_url in discovery_endpoints:
                    for payload in payload_variants:
                        for is_json in [False, True]:
                            ct = "application/json" if is_json else "application/x-www-form-urlencoded"
                            headers["Content-Type"] = ct
                            try:
                                p_retry = auth_session.post(
                                    alt_url, json=payload if is_json else None,
                                    data=None if is_json else payload, headers=headers, timeout=15
                                )
                                post_res = await p_retry
                                if post_res.status_code in [200, 302, 301]:
                                    logger.info(f"[Piccoma] ✨ Success via alternative: {alt_url}")
                                    break
                            except: continue
                        if post_res.status_code in [200, 302, 301]: break
                    if post_res.status_code in [200, 302, 301]: break
            
            # 7. Verification Loop
            if post_res.status_code in [200, 302]:
                viewer_paths = [f"/web/viewer/s/{series_id}/{episode_id}", f"/web/viewer/{series_id}/{episode_id}"]
                for v_path in viewer_paths:
                    v_res = await auth_session.get(f"{base_url}{v_path}", timeout=10)
                    if v_res.status_code == 200:
                        pdata = self._extract_pdata_heuristic(v_res.text)
                        if pdata:
                            logger.info(f"[Piccoma] ✅ Access granted for episode {episode_id}")
                            return True
                
                # Check JSON for success
                try:
                    resp_json = post_res.json()
                    if resp_json.get('result') == 'ok' or resp_json.get('success'):
                        logger.info(f"[Piccoma] ✅ Purchase confirmed via JSON")
                        return True
                except: pass
                
            logger.warning(f"[Piccoma] Purchase/Usage failed for episode {episode_id} (HTTP {post_res.status_code})")
            return False
            
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
                    except Exception: continue

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
        except Exception as e:
            logger.error(f"[Piccoma] Error fetching new series: {e}")
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
