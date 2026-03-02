import os
import re
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

try:
    from curl_cffi import requests
except ImportError:
    import requests

logger = logging.getLogger("KuaikanApiScraper")

class KuaikanApiScraper(BaseScraper):
    BASE_URL = "https://www.kuaikanmanhua.com"

    def __init__(self):
        # We use curl_cffi for the initial handshake to bypass potential WAF
        self.session = requests.Session(impersonate="chrome120")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Referer': 'https://www.kuaikanmanhua.com/'
        })
        self._load_cookies()

    def _load_cookies(self):
        kk_dir = Settings.SECRETS_DIR / "kuaikan"
        kk_dir.mkdir(parents=True, exist_ok=True)
        cookie_paths = list(kk_dir.glob("*.json"))
        self.session.cookies.clear()
        total_loaded = 0
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                cookies_list = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                for c in cookies_list:
                    if c.get('name') and c.get('value'):
                        self.session.cookies.set(c['name'], c['value'], domain='.kuaikanmanhua.com')
                        total_loaded += 1
            except Exception as e:
                logger.error(f"[Kuaikan] Cookie load failed: {e}")
        if total_loaded > 0:
            logger.info(f"[Kuaikan] ✅ Multi-Account Sync: {total_loaded} cookies active.")

    def get_series_info(self, url: str):
        match = re.search(r'(?:topic|mobile)/(\d+)', url)
        if not match: raise ScraperError("Invalid Kuaikan URL.")
        series_id = match.group(1)
        
        # 🟢 API-ONLY EXTRACTION (Clean Data)
        api_url = f"https://api.kuaikanmanhua.com/v1/topics/{series_id}"
        try:
            api_res = self.session.get(api_url, timeout=10)
            if api_res.status_code == 200:
                data = api_res.json()
                if data.get('code') == 200 and 'data' in data:
                    t_data = data['data']
                    title = t_data.get('title', f"Kuaikan_{series_id}")
                    image_url = t_data.get('cover_image_url')
                    
                    # Sort chronological
                    comics = sorted(t_data.get('comics', []), key=lambda x: x.get('created_at', 0))
                    all_chapters = []
                    
                    for idx, ch in enumerate(comics):
                        cid = str(ch.get('id'))
                        # 🟢 PULL RAW TITLE FROM API
                        raw_title = ch.get('title', str(idx+1))
                        all_chapters.append({
                            'id': cid,
                            'number_text': str(idx+1).zfill(2),
                            'title': raw_title,
                            'url': f"https://www.kuaikanmanhua.com/web/comic/{cid}/",
                            'is_locked': ch.get('is_free', True) is False
                        })
                    
                    return title, len(all_chapters), all_chapters, image_url, series_id
                else:
                    raise ScraperError(f"Kuaikan API returned error code: {data.get('code')}")
        except Exception as e:
            if isinstance(e, ScraperError): raise
            logger.error(f"[Kuaikan] API Failure: {e}")
            raise ScraperError(f"Failed to fetch metadata from Kuaikan API: {e}")

    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Kuaikan] 🕷️ EXTRACTING: {task.title}")
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # 🟢 NATIVE ROBUST DOWNLOADER
        dl_session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry))
        
        # 🟢 1. Force the Root PC Domain as Referer
        dl_session.headers.update({
            'Referer': 'https://www.kuaikanmanhua.com/', # Do not use task.url here to avoid mobile sub-domain blocks
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'image',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'cross-site'
        })
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.kuaikanmanhua.com')

        # 🟢 2. API-ONLY EXTRACTION
        api_url = f"https://api.kuaikanmanhua.com/v1/comics/{task.episode_id}"
        api_res = self.session.get(api_url, timeout=15)
        
        if api_res.status_code != 200:
            raise ScraperError(f"Kuaikan API failed (HTTP {api_res.status_code}). Check cookies/login.")
            
        data = api_res.json()
        if data.get('code') != 200:
            raise ScraperError(f"Kuaikan API Error: {data.get('message')}")

        # 1. Try API first
        image_urls = [img.get('url') for img in data.get('data', {}).get('comic_images', []) if img.get('url')]
        
        # 🟢 2. SMART FALLBACK: IIFE Regex Extraction
        if not image_urls:
            logger.info(f"[Kuaikan] API empty. Extracting IIFE data from HTML for {task.chapter_str}...")
            res = self.session.get(task.url, timeout=20)
            
            # Clean escaped slashes, unicode ampersands, and HTML entity ampersands
            clean_html = res.text.replace('\\u002F', '/').replace('\\u0026', '&').replace('&amp;', '&')
            
            # task.episode_id is the Kuaikan Comic ID (e.g., 829730)
            cid = task.episode_id
            
            # Extract all URLs belonging to this specific chapter's directory (/image/c{cid}/)
            # This perfectly filters out avatars, ads, and recommended comics!
            pattern = rf'"(https?://[^"]+?\.kkmh\.com/image/c{cid}/[^"]+)"'
            raw_urls = re.findall(pattern, clean_html)
            
            # Prioritize high-quality w1280 images. dict.fromkeys removes duplicates while preserving the exact reading order.
            w1280_urls = [u for u in raw_urls if 'w1280' in u]
            
            if w1280_urls:
                image_urls = list(dict.fromkeys(w1280_urls))
            else:
                image_urls = list(dict.fromkeys(raw_urls))

        if not image_urls:
            raise ScraperError(f"Failed to find images for {task.chapter_str}. Possibly VIP or access restricted.")

        image_data = [{'file': f"page_{idx+1:03d}.jpg", 'url': url} for idx, url in enumerate(image_urls)]
        
        # 🟢 PACED PARALLEL DOWNLOAD
        task.status = TaskStatus.DOWNLOADING
        def download_worker(item):
            time.sleep(0.2)
            img_res = dl_session.get(item['url'], timeout=30)
            img_res.raise_for_status()
            with open(os.path.join(output_dir, item['file']), 'wb') as f:
                f.write(img_res.content)

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(download_worker, image_data))
                
        return output_dir