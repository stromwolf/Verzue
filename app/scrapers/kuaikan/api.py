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
        
        # ATTEMPT 1: API (Cleanest Data)
        api_url = f"https://api.kuaikanmanhua.com/v1/topics/{series_id}"
        try:
            api_res = self.session.get(api_url, timeout=10)
            if api_res.status_code == 200:
                data = api_res.json()
                if data.get('code') == 200:
                    t_data = data['data']
                    title = t_data.get('title', f"Kuaikan_{series_id}")
                    image_url = t_data.get('cover_image_url')
                    # Kuaikan API usually returns newest first; we want chronological
                    comics = sorted(t_data.get('comics', []), key=lambda x: x.get('created_at', 0))
                    all_chapters = []
                    for idx, ch in enumerate(comics):
                        cid = str(ch.get('id'))
                        ch_title = ch.get('title', str(idx+1))
                        all_chapters.append({
                            'id': cid,
                            'number_text': str(idx+1).zfill(2),
                            'title': ch_title, # No more "Ch.1 - -" prefix
                            'url': f"https://www.kuaikanmanhua.com/web/comic/{cid}/",
                            'is_locked': ch.get('is_free', True) is False
                        })
                    return title, len(all_chapters), all_chapters, image_url, series_id
        except Exception as e:
            logger.warning(f"[Kuaikan] API fallback triggered: {e}")

        # ATTEMPT 2: NUHT HTML Mining (Fallback)
        res = self.session.get(f"https://www.kuaikanmanhua.com/web/topic/{series_id}/", timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("meta", property="og:title")["content"].split('|')[0].strip()
        image_url = soup.find("meta", property="og:image")["content"]

        all_chapters, seen_ids = [], set()
        nuxt_script = re.search(r'window\.__NUXT__\s*=\s*(.+?)(?:;</script>|$)', res.text, re.DOTALL)
        if nuxt_script:
            # Re-implementing the mjs logic: Extract id and title from NUXT
            items = re.findall(r'id\s*:\s*(\d+).*?title\s*:\s*["\']([^"\']+)["\']', nuxt_script.group(1))
            for cid, ch_title in items:
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    all_chapters.append({
                        'id': cid,
                        'url': f"https://www.kuaikanmanhua.com/web/comic/{cid}/",
                        'title_raw': ch_title,
                        'is_locked': False 
                    })
            # Ensure chronological order (NUXT often lists newest first)
            if all_chapters and int(all_chapters[0]['id']) > int(all_chapters[-1]['id']):
                all_chapters.reverse()

        for idx, ch in enumerate(all_chapters):
            ch['number_text'] = str(idx+1).zfill(2)
            ch['title'] = ch.get('title_raw', str(idx+1))

        return title, len(all_chapters), all_chapters, image_url, series_id

    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Kuaikan] 🕷️ EXTRACTING: {task.title}")
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # 🟢 NATIVE ROBUST DOWNLOADER
        dl_session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry))
        
        # 🟢 1. Set Browser-Grade Headers with dynamic Referer
        dl_session.headers.update({
            'Referer': task.url, # REQUIRED: Blocks 403 Forbidden
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'image',
            'Sec-Fetch-Mode': 'no-cors',
            'Sec-Fetch-Site': 'cross-site'
        })
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.kuaikanmanhua.com')

        res = self.session.get(task.url, timeout=20)
        if "login" in res.url or "pay" in res.url:
            raise ScraperError("Chapter is locked. Check Kuaikan cookies.")
        
        # 🟢 2. Dynamic Image Extraction from the main image container
        soup = BeautifulSoup(res.text, 'html.parser')
        image_urls = []
        
        # Kuaikan lists all chapter panels inside the "imgList" div
        container = soup.find("div", class_="imgList")
        if container:
            # Grab every image source inside this specific container
            for img in container.find_all("img", src=True):
                src = img["src"]
                # Verify the URL is a comic panel from KKMH or V3MH CDNs
                if any(cdn in src for cdn in ['kkmh.com', 'v3mh.com']) and "avatar" not in src:
                    if src not in image_urls:
                        image_urls.append(src)

        # 🟢 3. Fallback regex (General pattern, ignoring specific extensions)
        if not image_urls:
            # Finds any URL pointing to the KKMH/V3MH image CDNs regardless of extension
            pattern = r'https?://(?:tn1|tn2|tn3)\.(?:kkmh|v3mh)\.com/image/[^\s"\'\\]+'
            raw_found = re.findall(pattern, res.text)
            for url in raw_found:
                # Clean up any trailing characters or duplicates
                clean_url = url.split('"')[0].split("'")[0].split('\\')[0]
                if clean_url not in image_urls:
                    image_urls.append(clean_url)

        if not image_urls:
            raise ScraperError(f"Failed to extract image list for {task.chapter_str}.")

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