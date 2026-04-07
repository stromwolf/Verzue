import re
import json
import logging
import asyncio
import threading
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.services.login_service import LoginService
from app.core.exceptions import ScraperError
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
        self.session_service = SessionService()
        self.login_service = LoginService()
        
        # S-Grade: Chrome 120 baseline headers
        self.default_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.default_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            "Sec-Ch-Ua-Mobile": "?0",
            "Upgrade-Insecure-Requests": "1"
        }
        self._download_semaphore = asyncio.Semaphore(10)
        
        # Initialize sub-modules
        self.helpers = PiccomaHelpers(self)
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
        auth_session = await self._get_authenticated_session(domain)
        
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
        
        if fast:
            res = await auth_session.get(product_url)
            ep_res = None
        else:
            res_task = auth_session.get(product_url)
            ep_task = auth_session.get(episodes_url)
            res, ep_res = await asyncio.gather(res_task, ep_task)
            
        if res.status_code != 200: raise ScraperError(f"Failed to fetch series: {res.status_code}")
        
        # Geo-block detection
        if len(res.text) < 10000 and ("日本国内でのみ" in res.text or "only be used from Japan" in res.text):
            raise ScraperError("Piccoma geo-blocked: This service can only be accessed from Japan. Use a Japan VPN or proxy.")
        
        await self.session_service.record_session_success("piccoma")
        soup = BeautifulSoup(res.text, 'lxml' if 'lxml' in str(BeautifulSoup.DEFAULT_BUILDER) else 'html.parser')
        
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
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day, release_time = en_day, "15:00"
                    break

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
        ep_soup = soup if (fast or not ep_res) else BeautifulSoup(ep_res.text, 'lxml' if 'lxml' in str(BeautifulSoup.DEFAULT_BUILDER) else 'html.parser')

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
                    all_chapters.append({
                        'id': cid, 'title': c_title, 'notation': c_title,
                        'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                        'is_locked': not ep.get('is_free', False) and not ep.get('is_wait_free', False),
                        'is_new': ep.get('is_new', False)
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
                is_locked = bool(item.select_one('.PCM-epList_lock, .PCM-icon_lock, .PCM-icon_waitfree, .PCM-icon_clock'))
                if not is_locked:
                    is_locked = any(kw in item.get_text() for kw in ["待てば￥0", "¥0"]) is False and "無料" not in item.get_text()
                
                all_chapters.append({
                    'id': cid, 'title': c_title, 'notation': c_title, 'url': f"{task_viewer_prefix}/{series_id}/{cid}",
                    'is_locked': is_locked, 'is_new': "NEW" in item.get_text().upper()
                })

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
        
        res = await auth_session.get(task.url)
        is_locked_ui = res.status_code == 200 and ("js_purchaseForm" in res.text or "チャージ中" in res.text or "ポイントで読む" in res.text)
        
        if res.status_code != 200 or is_locked_ui:
            reason = f"HTTP {res.status_code}" if res.status_code != 200 else "Locked UI detected"
            logger.info(f"[Piccoma] Chapter {chapter_id} {reason}, attempting fast purchase/unlock.")
            if await self.fast_purchase(task):
                auth_session = await self._get_authenticated_session(domain)
                res = await auth_session.get(task.url)
            else:
                logger.error(f"  ❌ [Piccoma] Fast purchase failed for {chapter_id}")
             
        if res.status_code != 200: 
            raise ScraperError(f"Access error: {res.status_code}")
        
        await self.session_service.record_session_success("piccoma")

        # Manifest discovery
        pdata = self._extract_pdata_heuristic(res.text)
        if not pdata:
            logger.error(f"[Piccoma] Manifest extraction FAILED for {chapter_id}.")
            self._dump_diagnostic_data(f"manifest_fail_{chapter_id}", res.text)
            raise ScraperError(f"Could not extract chapter manifest for {chapter_id}.")

        images = pdata.get('img', pdata.get('contents', []))
        valid_images = [img for img in images if img.get('path')]
        if not valid_images: raise ScraperError("No accessible images found in manifest.")

        total = len(valid_images)
        from app.core.progress import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Piccoma", total)
        progress.update(0)

        async def process_one(img_data, i):
            async with self._download_semaphore:
                await self._download_robust(auth_session, img_data, i+1, output_dir, region)
            progress.update(i + 1)

        await asyncio.gather(*(process_one(img, i) for i, img in enumerate(valid_images)))
        progress.finish()
        return output_dir
