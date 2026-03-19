import re
import json
import logging
import math
import asyncio
import urllib.parse
import os
import random
from io import BytesIO
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError

try:
    from pycasso import Canvas
except ImportError:
    Canvas = None

logger = logging.getLogger("PiccomaProvider")

def dd(input_string):
    result_bytearray = bytearray()
    for index, byte in enumerate(bytes(input_string, 'utf-8')):
        if index < 3: byte = byte + (1 - 2 * (byte % 2))
        elif 2 < index < 6 or index == 8: pass
        elif index < 10: byte = byte + (1 - 2 * (byte % 2))
        elif 12 < index < 15 or index == 16: byte = byte + (1 - 2 * (byte % 2))
        elif index == len(input_string[:-1]) or index == len(input_string[:-2]): byte = byte + (1 - 2 * (byte % 2))
        else: pass
        result_bytearray.append(byte)
    return str(result_bytearray, 'utf-8')

class PiccomaProvider(BaseProvider):
    IDENTIFIER = "piccoma"

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

    async def _get_authenticated_session(self, region_domain: str):
        """S+ Refinement: TLS Fingerprint Entropy & Explicit Scoping."""
        session_obj = await self.session_service.get_active_session("piccoma")
        
        # S+ Fingerprint Entropy: Rotate between modern browser profiles
        browser_profiles = ["chrome110", "chrome116", "chrome120", "safari15_5", "edge101"]
        impersonation = random.choice(browser_profiles)
        
        async_session = AsyncSession(impersonate=impersonation)
        async_session.headers.update(self.default_headers)
        
        if session_obj:
            for c in session_obj["cookies"]:
                name, value = c.get('name'), c.get('value')
                if name and value: 
                    async_session.cookies.set(name, value, domain=region_domain)
        
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

    async def get_series_info(self, url: str):
        """S+ Refinement: Pure function, deriving all context from URL."""
        match = re.search(r'/web/product/(\d+)', url)
        if not match: raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        base_url, region, domain = self._get_context_from_url(url)
            
        auth_session = await self._get_authenticated_session(domain)
        res = await auth_session.get(f"{base_url}/web/product/{series_id}")
        if res.status_code != 200: raise ScraperError(f"Failed to fetch series: {res.status_code}")
        
        await self.session_service.record_session_success("piccoma")
        soup = BeautifulSoup(res.text, 'html.parser')
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        image_url = None
        thumb_img = soup.select_one('img.PCM-productThum_img')
        if thumb_img and thumb_img.get('src'):
            image_url = thumb_img['src']
            if image_url.startswith('//'): image_url = 'https:' + image_url
            if 'cover_x2' in image_url: image_url = f"https://wsrv.nl/?url={urllib.parse.quote(image_url)}"

        # S-Grade: Extract Release Day (V2 Feature)
        release_day = None
        release_time = None
        
        # JST to UTC Mapping for midnight releases
        day_map = {
            "日曜": "Saturday", "月曜": "Sunday", "火曜": "Monday",
            "水曜": "Tuesday", "木曜": "Wednesday", "金曜": "Thursday",
            "土曜": "Friday"
        }

        status_items = soup.select('ul.PCM-productStatus li')
        for li in status_items:
            text = li.get_text(strip=True)
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day = en_day
                    release_time = "15:00" # Midnight JST = 15:00 UTC
                    break
            if release_day: break

        return title, len(all_chapters), all_chapters, image_url, str(series_id), release_day, release_time

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
                res = await auth_session.get(task.url)
             
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
        completed = 0
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Piccoma", total)
        progress.update(completed)

        async def process_one(img_data, i):
            nonlocal completed
            async with self._download_semaphore:
                await self._download_robust(auth_session, img_data, i+1, output_dir, master_seed)
            completed += 1
            progress.update(completed)

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

        # Heuristic 2: Legacy _pdata_ global
        match = re.search(r'var\s+_pdata_\s*=\s*(\{.*?\})\s*(?:var\s+|</script>)', html_text, re.DOTALL)
        if match:
            try: return json.loads(match.group(1))
            except: pass
            
        return None

    async def _download_robust(self, session, img_data, idx, out_dir, seed):
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        res = await session.get(url, timeout=30)
        res.raise_for_status()
        out_path = f"{out_dir}/page_{idx:03d}.png"
        
        if seed and seed.isupper() and Canvas:
            try:
                def unscramble():
                    img_io = BytesIO(res.content)
                    canvas = Canvas(img_io, (50, 50), dd(seed))
                    return canvas.export(mode="scramble", format="png").getvalue()
                content = await asyncio.to_thread(unscramble)
                with open(out_path, "wb") as f: f.write(content)
            except Exception:
                with open(out_path, "wb") as f: f.write(res.content)
        else:
            with open(out_path, "wb") as f: f.write(res.content)

    def _calculate_seed(self, url, region):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        chk = qs.get('q', [''])[0] if region == "fr" else url.split('?')[0].split('/')[-2]
        expires = qs.get('expires', [''])[0]
        if expires.isdigit():
            n = int(expires)
            if n != 0: chk = chk[-n:] + chk[:len(chk)-n]
        return chk

    async def fast_purchase(self, task) -> bool:
        """Coin purchase via API - mirrors the browser 'BUY AT' button click."""
        match = re.search(r'/web/viewer/(\d+)/(\d+)', task.url)
        if not match:
            logger.debug(f"[Piccoma] fast_purchase: Could not parse series/episode from URL: {task.url}")
            return False
        
        series_id, episode_id = match.groups()
        base_url, region, domain = self._get_context_from_url(task.url)
        auth_session = await self._get_authenticated_session(domain)
        
        try:
            # 1. Fetch episode list page to get CSRF tokens & purchase form data
            episode_page_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
            res = await auth_session.get(episode_page_url, timeout=15)
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
            post_res = await auth_session.post(
                purchase_url, data=purchase_payload, headers=headers, timeout=15,
                allow_redirects=True
            )
            
            # 5. Verify success - check if we can now access the viewer
            if post_res.status_code in [200, 302]:
                # Try the scroll viewer URL format (from act files: /web/viewer/s/{sid}/{eid})
                for viewer_path in [f"/web/viewer/s/{series_id}/{episode_id}", f"/web/viewer/{series_id}/{episode_id}"]:
                    viewer_url = f"{base_url}{viewer_path}"
                    viewer_res = await auth_session.get(viewer_url, timeout=15)
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
            await session.get(url)
            # S+ Gaussian Jitter: random.gauss(mean, std_dev)
            sleep_time = max(2, random.gauss(5, 1.5))
            await asyncio.sleep(sleep_time)
        
        logger.info("[Piccoma] S+ Ritual Complete.")
