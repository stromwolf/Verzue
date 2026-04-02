import re
import json
import logging
import asyncio
import random
from curl_cffi.requests import AsyncSession, ProxyError
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("KuaikanProvider")

class KuaikanProvider(BaseProvider):
    IDENTIFIER = "kuaikan"
    BASE_URL = "https://www.kuaikanmanhua.com"
    SERIES_PATH = "/web/topic/"

    def __init__(self):
        self.session_service = SessionService()
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://www.kuaikanmanhua.com/'
        }
        self._download_semaphore = asyncio.Semaphore(10)

    async def _get_authenticated_session(self):
        session_obj = await self.session_service.get_active_session("kuaikan")
        async_session = AsyncSession(impersonate="chrome120", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)
        
        if session_obj:
            for c in session_obj["cookies"]:
                name, value = c.get('name'), c.get('value')
                if name and value:
                    async_session.cookies.set(name, value, domain='.kuaikanmanhua.com')
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        try:
            res = await session.get(self.BASE_URL, timeout=10)
            return res.status_code == 200
        except: return False

    async def get_series_info(self, url: str, fast: bool = False):
        match = re.search(r'(?:topic|mobile)/(\d+)', url)
        if not match: raise ScraperError("Invalid Kuaikan URL.")
        series_id = match.group(1)
        
        auth_session = await self._get_authenticated_session()
        try:
            res = await auth_session.get(api_url, timeout=15)
            if res.status_code != 200: raise ScraperError(f"Kuaikan API fail: {res.status_code}")
        except ProxyError:
            raise ScraperError("Scraping Proxy Denied Access (403) during Kuaikan fetch.", code="PX_403")
        except Exception as e:
            if "ScraperError" in type(e).__name__: raise
            raise ScraperError(f"Request failed: {e}")
            
        await self.session_service.record_session_success("kuaikan")
        
        data = res.json()
        if data.get('code') != 200: raise ScraperError(f"Kuaikan API Code: {data.get('code')}")
        
        t_data = data['data']
        title = t_data.get('title', f"Kuaikan_{series_id}")
        image_url = t_data.get('cover_image_url')
        
        comics = sorted(t_data.get('comics', []), key=lambda x: x.get('created_at', 0))
        all_chapters = []
        for idx, ch in enumerate(comics):
            all_chapters.append({
                'id': str(ch.get('id')), 'title': ch.get('title', str(idx+1)),
                'url': f"https://www.kuaikanmanhua.com/web/comic/{ch.get('id')}/",
                'is_locked': ch.get('is_free', True) is False
            })
            
        return title, len(all_chapters), all_chapters, image_url, series_id, None, None, None, None

    async def scrape_chapter(self, task, output_dir: str):
        auth_session = await self._get_authenticated_session()
        api_url = f"https://api.kuaikanmanhua.com/v1/comics/{task.episode_id}"
        
        res = await auth_session.get(api_url, timeout=15)
        if res.status_code != 200: raise ScraperError(f"Kuaikan chapter fail: {res.status_code}")
        await self.session_service.record_session_success("kuaikan")
        
        data = res.json()
        if data.get('code') != 200: raise ScraperError(f"Kuaikan API Error: {data.get('message')}")
        
        image_urls = [img.get('url') for img in data.get('data', {}).get('comic_images', []) if img.get('url')]
        if not image_urls: raise ScraperError("No images found.")

        total = len(image_urls)
        completed = 0
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Kuaikan", total)
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
        logger.info("[Kuaikan] Running behavioral ritual...")
        await session.get(self.BASE_URL)
        await asyncio.sleep(random.uniform(2, 5))
        await session.get(f"{self.BASE_URL}/web/category/")
