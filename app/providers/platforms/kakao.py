import re
import json
import logging
import math
import asyncio
import random
import time
from app.providers.curl_compat import AsyncSession, RequestsError, ProxyError
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("KakaoProvider")

class KakaoProvider(BaseProvider):
    IDENTIFIER = "kakao"
    BASE_URL = "https://page.kakao.com"
    SERIES_PATH = "/content/"
    GRAPHQL_URL = "https://bff-page.kakao.com/graphql"
    KWEBTOON_API_BASE = "https://gateway-kw.kakao.com"

    def __init__(self):
        self.session_service = SessionService()
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ko,en-US;q=0.9,en;q=0.8',
            'Content-Type': 'application/json',
        }
        self._download_semaphore = asyncio.Semaphore(10)

    async def _get_authenticated_session(self):
        session_obj = await self.session_service.get_active_session("kakao")
        async_session = AsyncSession(impersonate="chrome120", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)
        
        if session_obj:
            for c in session_obj["cookies"]:
                name, value = c.get('name'), c.get('value')
                if name and value:
                    async_session.cookies.set(name, value, domain='.kakao.com')
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        # Neutral check for Kakao
        try:
            res = await session.get("https://page.kakao.com/", timeout=10)
            return res.status_code == 200
        except: return False

    async def get_series_info(self, url: str, fast: bool = False):
        if "webtoon.kakao.com" in url:
            return await self._get_webtoon_info(url, fast=fast)
        return await self._get_page_info(url, fast=fast)

    async def _get_webtoon_info(self, url: str, fast: bool = False):
        match = re.search(r'/content/[^/]+/(\d+)', url)
        if not match: raise ScraperError("Invalid Webtoon URL")
        content_id = match.group(1)
        
        auth_session = await self._get_authenticated_session()
        title = f"Webtoon_{content_id}"
        image_url = None
        
        # SEO Meta
        try:
            res_seo = await auth_session.get(url, headers={'Accept': 'text/html'})
            if res_seo.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(res_seo.text, 'html.parser')
                og_t = soup.find("meta", property="og:title")
                if og_t: title = og_t["content"].split(" | ")[0].strip()
                og_i = soup.find("meta", property="og:image")
                if og_i: image_url = og_i["content"]
        except ProxyError:
             raise ScraperError("Scraping Proxy Denied Access (403) during SEO fetch.", code="PX_403")
        except: pass

        all_chapters = []
        if fast:
            logger.info(f"[Kakao] Fast Fetch (Webtoon Meta Only): {title}")
            return title, 0, [], image_url, content_id, None, None, None, None

        offset = 0
        limit = 100
        while True:
            api_url = f"{self.KWEBTOON_API_BASE}/episode/v2/views/content-home/contents/{content_id}/episodes?sort=NO&offset={offset}&limit={limit}"
            res = await auth_session.get(api_url)
            if res.status_code != 200: break
            
            data = res.json().get('data', {})
            episodes = data.get('episodes', [])
            if not episodes: break 

            for ep in episodes:
                all_chapters.append({
                    'id': str(ep['id']), 'title': ep.get('title', f"Ep {ep['no']}"),
                    'url': f"https://webtoon.kakao.com/viewer/{content_id}/{ep['id']}",
                    'is_locked': ep.get('useType') != "FREE"
                })
            
            total_count = data.get('pagination', {}).get('totalCount', 0)
            if offset + len(episodes) >= total_count: break
            offset += len(episodes)

        return title, len(all_chapters), all_chapters, image_url, content_id, None, None, None, None

    async def _get_page_info(self, url: str, fast: bool = False):
        match = re.search(r'/content/(\d+)', url)
        if not match: raise ScraperError("Invalid Page URL")
        series_id = match.group(1)
        
        auth_session = await self._get_authenticated_session()
        title = f"Page_{series_id}"
        image_url = None

        # Meta
        meta_query = "query contentHomeOverview($seriesId: Long!) { contentHomeOverview(seriesId: $seriesId) { content { title thumbnail } } }"
        try:
            res_meta = await auth_session.post(self.GRAPHQL_URL, json={"query": meta_query, "variables": {"seriesId": int(series_id)}})
        except RequestsError as e:
            logger.error(f"[Kakao] Request Error (Potential Proxy): {e}")
            raise ScraperError("Scraping Proxy Denied Access (403). Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
             
        if res_meta.status_code == 200:
            meta = res_meta.json().get('data', {}).get('contentHomeOverview', {}).get('content', {})
            if meta:
                title = meta.get('title', title)
                image_url = meta.get('thumbnail', "").replace("//", "https://")

        # Chapters
        chapter_query = """
        query contentHomeProductList($seriesId: Long!, $after: String, $first: Int) {
          contentHomeProductList(seriesId: $seriesId, after: $after, first: $first, boughtOnly: false, sortType: "asc") {
            pageInfo { hasNextPage endCursor }
            edges { node { single { productId title isFree } } }
          }
        }
        """
        all_chapters = []
        if fast:
            logger.info(f"[Kakao] Fast Fetch (Page Meta Only): {title}")
            return title, 0, [], image_url, series_id, None, None, None, None

        cursor = None
        has_next = True
        while has_next:
            res = await auth_session.post(self.GRAPHQL_URL, json={"query": chapter_query, "variables": {"seriesId": int(series_id), "first": 100, "after": cursor}})
            if res.status_code != 200: break
            data = res.json().get('data', {}).get('contentHomeProductList')
            if not data or not data['edges']: break
            for edge in data['edges']:
                node = edge['node']['single']
                all_chapters.append({
                    'id': str(node['productId']), 'title': node['title'],
                    'is_locked': not node.get('isFree', False),
                    'url': f"https://page.kakao.com/content/{series_id}/viewer/{node['productId']}"
                })
            has_next = data['pageInfo']['hasNextPage']
            cursor = data['pageInfo']['endCursor']
            
        return title, len(all_chapters), all_chapters, image_url, series_id, None, None, None, None

    async def scrape_chapter(self, task, output_dir: str):
        if "webtoon.kakao.com" in task.url:
            return await self._scrape_webtoon_chapter(task, output_dir)
        return await self._scrape_page_chapter(task, output_dir)

    async def _scrape_webtoon_chapter(self, task, output_dir: str):
        match = re.search(r'/viewer/(\d+)/(\d+)', task.url)
        content_id, episode_id = match.groups()
        auth_session = await self._get_authenticated_session()
        
        # Binary-Exact Handshake
        payload = [{"input": {"seriesEpisodeId": str(episode_id), "lastViewedAt": True, "startViewing": True, "lastViewedPageNumber": "1", "viewerType": "NORMAL"}}]
        compact_payload = json.dumps(payload, separators=(',', ':'))
        
        headers = {
            'next-action': "/api/viewer/episode/[episodeId]/media-resources",
            'Origin': 'https://webtoon.kakao.com',
            'Referer': 'https://webtoon.kakao.com/',
            'x-requested-with': 'XMLHttpRequest'
        }
        api_url = f"https://gateway-kw.kakao.com/episode/v1/views/viewer/episodes/{episode_id}/media-resources"
        res = await auth_session.post(api_url, data=compact_payload, headers=headers)
        
        if res.status_code != 200: raise ScraperError(f"Handshake failed: {res.status_code}")
        await self.session_service.record_session_success("kakao")
        
        resp_json = res.json()
        data = resp_json[0].get('data', {}) if isinstance(resp_json, list) else resp_json.get('data', {})
        files = data.get('media', {}).get('files', [])
        if not files: raise ScraperError("No images found.")

        total = len(files)
        completed = 0
        from app.core.progress import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Kakao", total)
        progress.update(completed)

        async def dl(f, idx):
            nonlocal completed
            async with self._download_semaphore:
                img_res = await auth_session.get(f['url'] if 'url' in f else f['secureUrl'], timeout=30)
                with open(f"{output_dir}/page_{idx:03d}.webp", "wb") as out: out.write(img_res.content)
            completed += 1
            progress.update(completed)

        await asyncio.gather(*(dl(f, i+1) for i, f in enumerate(files)))
        progress.finish()
        return output_dir

    async def _scrape_page_chapter(self, task, output_dir: str):
        sid = task.series_id_key
        pid = task.episode_id
        auth_session = await self._get_authenticated_session()
        
        viewer_query = """
        query viewerInfo($seriesId: Long!, $productId: Long!) {
          viewerInfo(seriesId: $seriesId, productId: $productId) {
            viewerData { ... on ImageViewerData { imageDownloadData { files { secureUrl } } } }
          }
        }
        """
        res = await auth_session.post(self.GRAPHQL_URL, json={"query": viewer_query, "variables": {"seriesId": int(sid), "productId": int(pid)}})
        await self.session_service.record_session_success("kakao")
        
        try:
            files = res.json()['data']['viewerInfo']['viewerData']['imageDownloadData']['files']
            total = len(files)
            completed = 0
            from app.core.progress import ProgressBar
            progress = ProgressBar(task.req_id, "Downloading", "Kakao", total)
            progress.update(completed)

            async def dl(f, idx):
                nonlocal completed
                async with self._download_semaphore:
                    img_res = await auth_session.get(f['secureUrl'], timeout=30)
                    with open(f"{output_dir}/page_{idx:03d}.webp", "wb") as out: out.write(img_res.content)
                completed += 1
                progress.update(completed)
            
            await asyncio.gather(*(dl(f, i+1) for i, f in enumerate(files)))
            progress.finish()
            return output_dir
        except: raise ScraperError("Failed parsing KakaoPage manifest.")

    async def fast_purchase(self, task) -> bool:
        return False

    async def run_ritual(self, session):
        # S-Grade Ritual: Visit main sections
        logger.info("[Kakao] Running behavioral ritual...")
        await session.get("https://page.kakao.com/main")
        await asyncio.sleep(random.uniform(2, 5))
        await session.get("https://webtoon.kakao.com/ranking")
        await asyncio.sleep(random.uniform(1, 3))
        await session.get("https://page.kakao.com/my/recent")
