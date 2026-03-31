import re
import json
import logging
import math
import asyncio
import urllib.parse
import os
import random
import struct
import base64
import wasmtime
from io import BytesIO
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings

try:
    from pycasso import Canvas
except ImportError:
    Canvas = None

logger = logging.getLogger("PiccomaProvider")

class PiccomaWASM:
    """S-Grade: WebAssembly Runner for Piccoma's V30 DRM."""
    
    def __init__(self):
        self.wasm_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "Piccoma", "diamond_bg.wasm")
        if not os.path.exists(self.wasm_path):
            # Fallback for different directory structures or production environments
            self.wasm_path = os.path.join(os.getcwd(), "Piccoma", "diamond_bg.wasm")
            
        self.engine = wasmtime.Engine()
        self.linker = wasmtime.Linker(self.engine)
        self.store = wasmtime.Store(self.engine)
        
        self._init_mocks()
        
        try:
            self.module = wasmtime.Module.from_file(self.engine, self.wasm_path)
            self.instance = self.linker.instantiate(self.store, self.module)
            
            exports = self.instance.exports(self.store)
            self.memory = exports["memory"]
            self.malloc = exports["__wbindgen_malloc"]
            self.free = exports["__wbindgen_free"]
            self._dd = exports["dd"]
            self.stack_ptr_func = exports["__wbindgen_add_to_stack_pointer"]
            logger.info("[PiccomaWASM] Successfully initialized V30 DRM engine.")
        except Exception as e:
            logger.error(f"[PiccomaWASM] Failed to initialize WASM engine: {e}")
            raise ScraperError(f"Critical DRM Initialization Failure: {e}")

    def _init_mocks(self):
        """Implement required wbg host imports for the Rust-based WASM module."""
        def define_wbg(name, params, results, callback):
            p_types = [getattr(wasmtime.ValType, p)() for p in params]
            r_types = [getattr(wasmtime.ValType, r)() for r in results]
            ft = wasmtime.FuncType(p_types, r_types)
            self.linker.define_func("wbg", name, ft, callback)

        # Better Mocks
        def mock_btoa(caller, ptr, len, ret_ptr):
            return 0
        def mock_string_get(caller, ptr, len):
            return 0

        # Environment & Registry Mocks
        define_wbg("__wbindgen_object_drop_ref", ["i32"], [], lambda *args: None)
        define_wbg("__wbg_instanceof_Window_cde2416cf5126a72", ["i32"], ["i32"], lambda *args: 1)
        define_wbg("__wbg_location_61ca61017633c753", ["i32"], ["i32"], lambda *args: 0)
        define_wbg("__wbg_btoa_396932eb505ec155", ["i32", "i32", "i32"], ["i32"], mock_btoa)
        define_wbg("__wbg_newnoargs_ccdcae30fd002262", ["i32", "i32"], ["i32"], lambda *args: 0)
        define_wbg("__wbg_call_669127b9d730c650", ["i32", "i32"], ["i32"], lambda *args: 0)
        define_wbg("__wbindgen_string_get", ["i32", "i32"], ["i32"], mock_string_get)
        define_wbg("__wbindgen_object_clone_ref", ["i32"], ["i32"], lambda _, x: x)
        define_wbg("__wbg_self_3fad056edded10bd", [], ["i32"], lambda _: 0)
        define_wbg("__wbg_window_a4f46c98a61d4089", [], ["i32"], lambda _: 0)
        define_wbg("__wbg_globalThis_17eff828815f7d84", [], ["i32"], lambda _: 0)
        define_wbg("__wbg_global_46f939f6541643c5", [], ["i32"], lambda _: 0)
        define_wbg("__wbindgen_is_undefined", ["i32"], ["i32"], lambda *args: 0)
        define_wbg("__wbg_toString_2c5d5b612e8bdd61", ["i32"], ["i32"], lambda *args: 0)
        define_wbg("__wbindgen_debug_string", ["i32", "i32"], [], lambda *args: None)
        define_wbg("__wbindgen_throw", ["i32", "i32"], [], self._mock_throw)

    def _mock_throw(self, caller, ptr, len):
        msg = self.memory.read(self.store, ptr, ptr + len).decode()
        logger.error(f"[PiccomaWASM] WASM Exception: {msg}")
        raise ScraperError(f"WASM Execution Error: {msg}")

    def dd(self, seed: str) -> str:
        """Executes the diamond_bg 'dd' transformation on the provided rotated checksum."""
        seed_bytes = seed.encode('utf-8')
        seed_len = len(seed_bytes)
        
        # 1. Allocate input buffer in WASM memory
        seed_ptr = self.malloc(self.store, seed_len)
        self.memory.write(self.store, seed_bytes, seed_ptr)
        
        # 2. Reserve stack space for result metadata [ptr, len]
        self.stack_ptr_func(self.store, -16)
        ret_ptr = self.stack_ptr_func(self.store, 0)
        
        try:
            # 3. Call exported dd(ret_ptr, input_ptr, input_len)
            self._dd(self.store, ret_ptr, seed_ptr, seed_len)
            
            # 4. Extract result pointer and length from stack
            res_mem = self.memory.read(self.store, ret_ptr, ret_ptr + 8)
            res_ptr, res_len = struct.unpack("<II", res_mem)
            
            if res_len == 0:
                return seed # Fallback if transformation fails
                
            # 5. Read transformed string from WASM memory
            res_bytes = self.memory.read(self.store, res_ptr, res_ptr + res_len)
            return res_bytes.decode('utf-8')
        except Exception as e:
            logger.warning(f"[PiccomaWASM] Transformation failed, using raw seed. Error: {e}")
            return seed
        finally:
            # Cleanup: Restore stack and free allocated input
            self.stack_ptr_func(self.store, 16)
            self.free(self.store, seed_ptr, seed_len)

# Singleton instance to avoid repeated initialization costs
wasm_engine = None
try:
    wasm_engine = PiccomaWASM()
except Exception as e:
    logger.warning(f"[Piccoma] Could not initialize WASM engine globally. Retrying per task. Error: {e}")

class PiccomaProvider(BaseProvider):
    IDENTIFIER = "piccoma"
    BASE_URL = "https://piccoma.com"
    SERIES_PATH = "/web/product/"

    def __init__(self):
        self.session_service = SessionService()
        self.default_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        # S-Grade Backpressure
        self._download_semaphore = asyncio.Semaphore(15)

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
                    async_session.cookies.set(name, value, domain=region_domain)
                    d_base = str(region_domain)
                    if d_base.startswith('.'):
                        async_session.cookies.set(name, value, domain=d_base[1:])
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
                    'id': cid, 'title': c_title, 'notation': notation, 'url': f"{base_url}/web/viewer/{series_id}/{cid}",
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
                
                # Title extraction
                title_tag = item.select_one('p.PCM-epList_title, span.PCM-epList_title, .PCM-epList_title')
                if title_tag:
                    c_title = title_tag.get_text(strip=True)
                else:
                    # Fallback to link text or serial number
                    c_title = link.get_text(strip=True).split('\n')[0]
                
                notation = c_title # Typically 第1話
                
                # Lock status
                is_locked = bool(item.select_one('span.PCM-epList_lock, i.PCM-epList_lock_icon, .PCM-icon_lock, .PCM-epList_status_waitfree'))
                # Piccoma often uses a "Wait for free" (Wait-until-free) icon too
                if not is_locked:
                    is_locked = "待てば￥0" not in item.get_text() and "無料" not in item.get_text() and "¥0" not in item.get_text()
                
                all_chapters.append({
                    'id': cid,
                    'title': c_title,
                    'notation': notation,
                    'url': f"{base_url}/web/viewer/{series_id}/{cid}",
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
                                'url': f"{base_url}/web/viewer/s/{series_id}/{cid}",
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
        match = re.search(r'/web/viewer/(\d+)/(\d+)', task.url)
        if not match: raise ScraperError("Invalid Piccoma Viewer URL")
        
        series_id, chapter_id = match.groups()
        base_url, region, domain = self._get_context_from_url(task.url)
        auth_session = await self._get_authenticated_session(domain)
        
        # 1. Primary Extraction (Next.js Hydration Data)
        res = await auth_session.get(task.url)
        if res.status_code != 200:
            # Attempt coin purchase for locked chapters
            logger.info(f"[Piccoma] Chapter locked (HTTP {res.status_code}), attempting coin purchase for {chapter_id}")
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

        images = pdata.get('img', pdata.get('contents', []))
        valid_images = [img for img in images if img.get('path')]
        if not valid_images: raise ScraperError("No accessible images found in manifest.")

        # Seed is derived per task, no instance storage
        master_seed = self._calculate_seed(valid_images[0]['path'], region)
        
        total = len(valid_images)
        stats = {"completed": 0}
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Piccoma", total)
        progress.update(stats["completed"])

        async def process_one(img_data, i):
            async with self._download_semaphore:
                await self._download_robust(auth_session, img_data, i+1, output_dir, master_seed)
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

    async def _download_robust(self, session, img_data, idx, out_dir, seed):
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        d_task = session.get(url, timeout=30)
        res = await d_task
        res.raise_for_status()
        out_path = f"{out_dir}/page_{idx:03d}.png"
        
        if seed and len(seed) > 1 and Canvas:
            try:
                def unscramble():
                    if not Canvas:
                        raise ScraperError("pycasso (scrambler) not installed. Cannot process Piccoma images.")
                    img_io = BytesIO(res.content)
                    # The intermediate seed must be passed through dd() (WASM transform)
                    # before being fed into the PRNG for tile unshuffling.
                    final_seed = seed
                    if wasm_engine:
                        final_seed = wasm_engine.dd(seed)
                    
                    canvas = Canvas(img_io, (50, 50), final_seed)
                    return canvas.export(mode="unscramble", format="png").getvalue()
                content = await asyncio.to_thread(unscramble)
                with open(out_path, "wb") as f: f.write(content)
            except Exception:
                with open(out_path, "wb") as f: f.write(res.content)
        else:
            with open(out_path, "wb") as f: f.write(res.content)

    def _calculate_seed(self, url, region):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        chk = qs.get('q', [''])[0] if region == "fr" else url.split('?')[0].rstrip('/').split('/')[-1]
        
        # S+ Enhancement: Strip Piccoma bracket suffixes or other artifacts
        if isinstance(chk, str): 
            chk = chk.rstrip('[')

        expires = qs.get('expires', [''])[0]
        # expires is used for a seed shift loop, but for timestamps (long strings), 
        # it might cause offset issues if not clamped or handled as per Piccoma viewer spec.
        if expires:
            # Sum of digits logic (Piccoma V30 update)
            sum_digits = sum(int(digit) for digit in str(expires) if digit.isdigit())
            if len(chk) > 0:
                shift = sum_digits % len(chk)
                if shift != 0:
                    c_base = str(chk)
                    chk = c_base[-shift:] + c_base[:len(c_base)-shift]
        return chk

    async def fast_purchase(self, task) -> bool:
        """Coin purchase via API - mirrors the browser 'BUY AT' button click."""
        match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', task.url)
        if not match:
            logger.debug(f"[Piccoma] fast_purchase: Could not parse series/episode from URL: {task.url}")
            return False
        
        # 🟢 S-GRADE: Slice with explicit cast to appease lint
        s_base = str(match.group(1))
        e_base = str(match.group(2))
        series_id, episode_id = s_base, e_base
        base_url, region, domain = self._get_context_from_url(task.url)
        auth_session = await self._get_authenticated_session(domain)
        
        try:
            # 1. Fetch episode list page to get CSRF tokens & purchase form data
            episode_page_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
            p_task = auth_session.get(episode_page_url, timeout=15)
            res = await p_task
            if res.status_code != 200:
                logger.warning(f"[Piccoma] Could not load episode page: {res.status_code}")
                return False
            
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # 2. Extract CSRF token
            headers = {
                "Referer": episode_page_url,
                "Origin": base_url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            csrf_meta = soup.find('meta', {'name': 'csrf-token'})
            if csrf_meta:
                headers['X-CSRF-Token'] = csrf_meta['content']
            
            # 3. Build purchase payload
            purchase_url = f"{base_url}/web/episode/purchase"
            purchase_payload = {
                "episodeId": episode_id,
                "productId": series_id,
            }
            
            # Look for purchase form in the page and extract hidden fields
            purchase_form = soup.select_one('#js_purchaseForm, form[action*="purchase"], form[action*="episode"]')
            if purchase_form:
                for hidden in purchase_form.find_all('input', type='hidden'):
                    if hidden.get('name'):
                        purchase_payload[hidden['name']] = hidden.get('value', '')
                action = purchase_form.get('action')
                if action:
                    purchase_url = action if action.startswith('http') else f"{base_url}{action}"
            
            # 4. POST purchase request
            logger.info(f"[Piccoma] Sending coin purchase request for episode {episode_id} (series {series_id})")
            post_task = auth_session.post(
                purchase_url, data=purchase_payload, headers=headers, timeout=15,
                allow_redirects=True
            )
            post_res = await post_task
            
            # 5. Verify success - check if we can now access the viewer
            if post_res.status_code in [200, 302]:
                # Try the scroll viewer URL format (from act files: /web/viewer/s/{sid}/{eid})
                for viewer_path in [f"/web/viewer/s/{series_id}/{episode_id}", f"/web/viewer/{series_id}/{episode_id}"]:
                    viewer_url = f"{base_url}{viewer_path}"
                    v_task = auth_session.get(viewer_url, timeout=15)
                    viewer_res = await v_task
                    if viewer_res.status_code == 200:
                        pdata = self._extract_pdata_heuristic(viewer_res.text)
                        if pdata:
                            logger.info(f"[Piccoma] ✅ Coin purchase successful for episode {episode_id}")
                            await self.session_service.record_session_success("piccoma")
                            return True
                
                # Also check if JSON response indicates success
                try:
                    resp_data = post_res.json()
                    if resp_data.get('result') == 'ok' or resp_data.get('success'):
                        logger.info(f"[Piccoma] ✅ Coin purchase confirmed via API response for episode {episode_id}")
                        await self.session_service.record_session_success("piccoma")
                        return True
                except (json.JSONDecodeError, ValueError):
                    pass
            
            logger.warning(f"[Piccoma] Coin purchase failed for episode {episode_id} (HTTP {post_res.status_code})")
            return False
            
        except Exception as e:
            logger.error(f"[Piccoma] Coin purchase error for episode {episode_id}: {e}")
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
