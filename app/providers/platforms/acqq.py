import re
import json
import logging
import asyncio
import random
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from curl_cffi.requests import AsyncSession, RequestsError
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("AcqqProvider")

class AcqqProvider(BaseProvider):
    IDENTIFIER = "acqq"
    BASE_URL = "https://ac.qq.com"
    SERIES_PATH = "/Comic/comicInfo/id/"

    def __init__(self):
        self.session_service = SessionService()
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://m.ac.qq.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Ch-Ua-Platform': '"Chrome OS"',
        }
        self._download_semaphore = asyncio.Semaphore(10)

    async def _get_authenticated_session(self):
        # Tencent specific: We keep the Chrome OS impersonation
        session_obj = await self.session_service.get_active_session("acqq")
        async_session = AsyncSession(impersonate="chrome120", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)
        
        if session_obj:
            for c in session_obj["cookies"]:
                name, value = c.get('name'), c.get('value')
                if name and value:
                    async_session.cookies.set(name, value, domain='.ac.qq.com')
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        try:
            res = await session.get("https://ac.qq.com/MyComic", timeout=10)
            return res.status_code == 200
        except: return False

    async def get_series_info(self, url: str, fast: bool = False):
        if fast:
            # Tencent mobile page is small enough that we can just fetch it, 
            # but we'll respect the flag for consistency.
            pass
        match = re.search(r'id/(\d+)', url)
        if not match: raise ScraperError("Invalid Tencent URL.")
        series_id = match.group(1)
        logger.info(f"[ACQQ] 🔍 Series Info Requested: {url} (ID: {series_id})")
        
        auth_session = await self._get_authenticated_session()
        target_url = f"https://m.ac.qq.com/comic/index/id/{series_id}"
        
        try:
            res = await auth_session.get(target_url, timeout=15)
            if res.status_code != 200: raise ScraperError(f"Tencent metadata fail: {res.status_code}")
        except RequestsError as e:
            logger.error(f"[ACQQ] Request Error (Potential Proxy): {e}")
            raise ScraperError("Scraping Proxy Denied Access (403). Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
        except Exception as e:
            if "ScraperError" in type(e).__name__: raise
            raise ScraperError(f"Request failed: {e}")
            
        await self.session_service.record_session_success("acqq")
        
        soup = BeautifulSoup(res.text, 'html.parser')
        title_meta = soup.find("meta", property="og:title")
        title = title_meta["content"] if title_meta else "Unknown"
        
        image_url = None
        cover_meta = soup.find("meta", property="og:image")
        if cover_meta: image_url = cover_meta["content"]

        all_chapters = []
        items = soup.select(".chapter-item, .works-chapter-item, .chapter-link")
        for idx, item in enumerate(items):
            link = item if item.name == 'a' else item.find("a")
            if not link: continue
            href = link.get('href')
            cid_match = re.search(r'cid/(\d+)', href)
            if not cid_match: continue
            
            raw_text = link.get_text(strip=True)
            clean_title = raw_text.split(title)[-1].strip() if title in raw_text else raw_text
            
            all_chapters.append({
                'id': cid_match.group(1), 'title': clean_title or f"Ep {idx+1}",
                'url': f"https://m.ac.qq.com/chapter/id/{series_id}/cid/{cid_match.group(1)}",
                'is_locked': bool(item.select_one(".lock, .vip, .ui-icon-pay"))
            })

        all_chapters.sort(key=lambda x: int(x['id']))
        logger.info(f"[ACQQ] Metadata Parsed: Title='{title}', Chapters='{len(all_chapters)}', Image='{'Yes' if image_url else 'No'}'")
        return title, len(all_chapters), all_chapters, image_url, series_id, None, None, None, None

    async def scrape_chapter(self, task, output_dir: str):
        auth_session = await self._get_authenticated_session()
        target_url = task.url
        if "m.ac.qq.com" not in target_url:
            target_url = target_url.replace("ac.qq.com/ComicView", "m.ac.qq.com/chapter")
            
        res = await auth_session.get(target_url, timeout=20)
        await self.session_service.record_session_success("acqq")
        
        image_urls = []
        soup = BeautifulSoup(res.text, 'html.parser')
        imgs = soup.select(".comic-pic-list img, #comic-pic-list img, .comic-contain img")
        for img in imgs:
            src = img.get('src') or img.get('data-src')
            if src and 'http' in src and '.gif' not in src: image_urls.append(src)

        if not image_urls:
            json_match = re.search(r'var\s+(?:DATA|_v)\s*=\s*({.*?});', res.text, re.DOTALL)
            if json_match:
                try:
                    data = json.loads(json_match.group(1))
                    if 'picture' in data: image_urls = [p.get('url') for p in data['picture']]
                except: pass

        if not image_urls: raise ScraperError("No images found.")

        total = len(image_urls)
        completed = 0
        from app.core.progress import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "AC.QQ", total, episode_id=task.episode_id)
        progress.update(completed)

        async def dl(url, idx):
            nonlocal completed
            async with self._download_semaphore:
                img_res = await auth_session.get(url, timeout=30)
                with open(f"{output_dir}/page_{idx:03d}.jpg", "wb") as out: out.write(img_res.content)
            completed += 1
            progress.update(completed)

        await asyncio.gather(*(dl(u, i+1) for i, u in enumerate(image_urls)))
        progress.finish()
        return output_dir

    async def fast_purchase(self, task) -> bool:
        return False

    async def run_ritual(self, session):
        logger.info("[AC.QQ] Running behavioral ritual...")
        await session.get("https://m.ac.qq.com/")
        await asyncio.sleep(random.uniform(2, 4))
        await session.get("https://m.ac.qq.com/category/index")
