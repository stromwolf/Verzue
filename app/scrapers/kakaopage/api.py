import re
import json
import logging
import math
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
from urllib.parse import unquote

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError, LoginRequiredError
from app.models.chapter import TaskStatus

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests # Fallback

logger = logging.getLogger("KakaoApi")

class KakaoApiScraper(BaseScraper):
    GRAPHQL_URL = "https://bff-page.kakao.com/graphql"
    KWEBTOON_API_BASE = "https://gateway-kw.kakao.com"
    KWEBTOON_NEXT_ACTION = "/api/viewer/episode/[episodeId]/media-resources"

    def __init__(self):
        self.session = crequests.Session(impersonate="chrome120")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/json',
            'Origin': 'https://page.kakao.com',
            'Referer': 'https://page.kakao.com/'
        })
        self._load_cookies()

    def _load_cookies(self):
        """Injects cookies into the API session from all Kakao accounts."""
        kakao_dir = Settings.SECRETS_DIR / "kakao"
        kakao_dir.mkdir(parents=True, exist_ok=True)
        
        # Scan data/secrets/kakao/*.json + legacy cookies.json
        cookie_paths = list(kakao_dir.glob("*.json"))
        cookie_paths.append(Settings.COOKIES_FILE)
        
        total_loaded = 0
        files_found = 0
        
        for path in cookie_paths:
            if not path.exists(): continue
            files_found += 1
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                
                file_count = 0
                for c in cookies:
                    # Kakao is greedy, we map all to .kakao.com to cover subdomains
                    self.session.cookies.set(c['name'], c['value'], domain='.kakao.com')
                    file_count += 1
                
                total_loaded += file_count
                logger.debug(f"[Kakao] 🍪 Loaded {file_count} cookies from {path.name}")
            except Exception as e:
                logger.error(f"[Kakao] Cookie load failed from {path.name}: {e}")

        if files_found > 0:
            logger.info(f"[Kakao] ✅ Multi-Account Ready: {total_loaded} cookies from {files_found} sources.")

    def get_series_info(self, url: str):
        if "webtoon.kakao.com" in url:
            return self._get_webtoon_info(url)
        return self._get_page_info(url)

    def _get_webtoon_info(self, url: str):
        match = re.search(r'/content/[^/]+/(\d+)', url)
        if not match: raise ScraperError("Invalid Kakao Webtoon URL")
        content_id = match.group(1)

        title = f"Kakao Webtoon {content_id}"
        image_url = None
        
        try:
            logger.info(f"[Kakao-Webtoon] Fetching SEO Metadata...")
            h = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
            }
            r = requests.get(url, headers=h, timeout=10)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, 'html.parser')
                
                og_t = soup.find("meta", property="og:title")
                if og_t:
                    title = og_t["content"].split(" | ")[0].strip()
                
                og_i = soup.find("meta", property="og:image")
                if og_i:
                    image_url = og_i["content"]
                
                logger.info(f"[Kakao-Webtoon] SEO Title Found: {title}")
        except Exception as e:
            logger.warning(f"[Kakao-Webtoon] SEO Hijack failed: {e}")

        all_chapters = []
        offset = 0
        limit = 100

        self.session.headers.update({
            'Origin': 'https://webtoon.kakao.com',
            'Referer': f'https://webtoon.kakao.com/content/{content_id}'
        })

        while True:
            api_url = f"{self.KWEBTOON_API_BASE}/episode/v2/views/content-home/contents/{content_id}/episodes?sort=NO&offset={offset}&limit={limit}"
            res = self.session.get(api_url)
            if res.status_code != 200: break
            
            data = res.json().get('data', {})
            episodes = data.get('episodes', [])
            
            if not episodes: break 

            for ep in episodes:
                all_chapters.append({
                    'id': str(ep['id']),
                    'title': ep.get('title', f"Episode {ep['no']}"),
                    'number_text': str(ep['no']),
                    'url': f"https://webtoon.kakao.com/viewer/{content_id}/{ep['id']}",
                    'is_locked': ep.get('useType') != "FREE"
                })
            
            pagination = data.get('pagination', {})
            if offset + len(episodes) >= pagination.get('totalCount', 0): break
            offset += len(episodes)

        return title, len(all_chapters), all_chapters, image_url, content_id

    def _get_page_info(self, url: str):
        match = re.search(r'/content/(\d+)', url)
        if not match: raise ScraperError("Invalid Kakao Page URL")
        series_id = int(match.group(1))
        
        meta_query = """
        query contentHomeOverview($seriesId: Long!) {
          contentHomeOverview(seriesId: $seriesId) {
            content { title thumbnail }
          }
        }
        """
        title = f"Kakao Page {series_id}"
        image_url = None

        try:
            self.session.headers.update({
                'Referer': f'https://page.kakao.com/content/{series_id}',
                'Accept': 'application/json'
            })
            res_meta = self.session.post(self.GRAPHQL_URL, json={
                "query": meta_query, "variables": {"seriesId": series_id}
            })
            if res_meta.status_code == 200:
                meta = res_meta.json().get('data', {}).get('contentHomeOverview', {}).get('content', {})
                if meta:
                    title = meta.get('title', title)
                    image_url = meta.get('thumbnail', "").replace("//", "https://")
        except: pass

        chapter_query = """
        query contentHomeProductList($seriesId: Long!, $after: String, $first: Int) {
          contentHomeProductList(seriesId: $seriesId, after: $after, first: $first, boughtOnly: false, sortType: "asc") {
            pageInfo { hasNextPage endCursor }
            edges {
              node { single { productId title isFree } }
            }
          }
        }
        """
        all_chapters = []
        cursor = None
        has_next = True

        while has_next:
            payload = {"query": chapter_query, "variables": {"seriesId": series_id, "first": 100, "after": cursor}}
            res = self.session.post(self.GRAPHQL_URL, json=payload)
            if res.status_code != 200: break

            data = res.json().get('data', {}).get('contentHomeProductList')
            if not data or not data['edges']: break

            for edge in data['edges']:
                node = edge['node']['single']
                all_chapters.append({
                    'id': str(node['productId']),
                    'title': node['title'],
                    'number_text': str(len(all_chapters) + 1),
                    'is_locked': not node.get('isFree', False),
                    'url': f"https://page.kakao.com/content/{series_id}/viewer/{node['productId']}"
                })
            has_next = data['pageInfo']['hasNextPage']
            cursor = data['pageInfo']['endCursor']
            
        return title, len(all_chapters), all_chapters, image_url, str(series_id)

    def scrape_chapter(self, task, output_dir):
        if "webtoon.kakao.com" in task.url:
            return self._scrape_webtoon_chapter(task, output_dir)
        return self._scrape_page_chapter(task, output_dir)

    def _dump_request_as_curl(self, method, url, headers, data, cookies):
        """Helper to print a request as a cURL command for debugging."""
        logger.info("--- cURL COMMAND (for manual debugging) ---")
        
        curl_cmd = f"curl -X {method.upper()} '{url}'"

        for key, value in headers.items():
            curl_cmd += f" -H '{key}: {value}'"

        cookie_string = '; '.join([f'{c.name}={c.value}' for c in cookies])
        if cookie_string:
            curl_cmd += f" -H 'Cookie: {cookie_string}'"

        if data:
            # json.dumps to ensure it's a single, correctly escaped line
            curl_cmd += f" --data-raw '{json.dumps(data)}'"

        logger.info(curl_cmd)
        logger.info("-------------------------------------------")


    def _get_robust_session(self):
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        for k, v in self.session.cookies.items():
            session.cookies.set(k, v, domain='.kakao.com')
        return session

    def _scrape_webtoon_chapter(self, task, output_dir):
        """
        Binary-Exact Handshake for Kakao Webtoon.
        Removes all JSON whitespace to prevent 400 BAD_REQUEST.
        """
        match = re.search(r'/viewer/(\d+)/(\d+)', task.url)
        content_id, episode_id = match.groups()

        logger.info(f"[Kakao-Webtoon] 🤝 Handshaking Episode: {episode_id}")

        if not self.session.cookies.get('_kpdid'):
            self.session.get("https://webtoon.kakao.com/", timeout=10)

        handshake_headers = {
            'accept': 'application/json, text/plain, */*',
            'accept-language': 'ko',
            'content-type': 'application/json;charset=UTF-8',
            'next-action': self.KWEBTOON_NEXT_ACTION,
            'origin': 'https://webtoon.kakao.com',
            'referer': 'https://webtoon.kakao.com/',
            'x-requested-with': 'XMLHttpRequest',
            'priority': 'u=1, i'
        }

        payload_obj = [{
            "input": {
                "seriesEpisodeId": str(episode_id),
                "lastViewedAt": True,
                "startViewing": True,
                "lastViewedPageNumber": "1",
                "viewerType": "NORMAL"
            }
        }]
        compact_payload = json.dumps(payload_obj, separators=(',', ':'))

        api_url = f"https://gateway-kw.kakao.com/episode/v1/views/viewer/episodes/{episode_id}/media-resources"

        try:
            res = self.session.post(
                api_url, 
                data=compact_payload, 
                headers=handshake_headers, 
                timeout=15
            )
            
            if res.status_code != 200:
                logger.error(f"   ❌ Handshake Rejected (Status {res.status_code})")
                logger.error(f"   Server Response: {res.text}")
                raise ScraperError(f"Gateway rejected request: {res.status_code}")

            resp_json = res.json()
            data = resp_json[0].get('data', {}) if isinstance(resp_json, list) else resp_json.get('data', {})
            
            media = data.get('media', {})
            files = media.get('files', [])

            if not files:
                raise ScraperError("No images found. Chapter might be PAID/LOCKED.")

            task.smartoon_id = media.get('zid') or media.get('aid') or episode_id
            
            image_urls = [f['url'] for f in files]
            total = len(image_urls)
            logger.info(f"   ✅ Handshake Success! Mapped {total} pages.")

            task.status = TaskStatus.DOWNLOADING
            dl_session = self._get_robust_session()
            with ThreadPoolExecutor(max_workers=5) as executor:
                executor.map(lambda x: self._download_image_robust(dl_session, x[1], x[0]+1, output_dir), enumerate(image_urls))

        except Exception as e:
            logger.error(f"[Kakao-Webtoon] Scrape failed: {e}")
            raise ScraperError(str(e))

    def _scrape_page_chapter(self, task, output_dir):
        pid = task.episode_id
        sid = task.series_id_key
        
        viewer_query = """
        query viewerInfo($seriesId: Long!, $productId: Long!) {
          viewerInfo(seriesId: $seriesId, productId: $productId) {
            viewerData {
              ... on ImageViewerData {
                imageDownloadData { files { secureUrl } }
              }
            }
          }
        }
        """
        payload = {"query": viewer_query, "variables": {"seriesId": int(sid), "productId": int(pid)}}
        res = self.session.post(self.GRAPHQL_URL, json=payload)
        
        data = res.json()
        try:
             files = data['data']['viewerInfo']['viewerData']['imageDownloadData']['files']
             urls = [f['secureUrl'] for f in files]
             dl_session = self._get_robust_session()
             with ThreadPoolExecutor(max_workers=5) as exe:
                 exe.map(lambda x: self._download_image_robust(dl_session, x[1], x[0]+1, output_dir), enumerate(urls))
        except: raise ScraperError("Failed to get images from KakaoPage")

    def _download_image_robust(self, dl_session, url, idx, out_dir):
        time.sleep(0.3)
        res = dl_session.get(url, timeout=30)
        if res.status_code == 200:
            with open(f"{out_dir}/page_{idx:03d}.webp", "wb") as f:
                f.write(res.content)