import os
import re
import json
import math
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from urllib.parse import urljoin

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
                logger.error(f"[Kuaikan] Cookie load failed from {path.name}: {e}")
                
        if total_loaded > 0:
            logger.info(f"[Kuaikan] ✅ Multi-Account Sync: {total_loaded} cookies active.")

    def get_series_info(self, url: str):
        # Support both desktop (/web/topic/) and mobile (/mobile/) formats
        match = re.search(r'(?:topic|mobile)/(\d+)', url)
        if not match: raise ScraperError("Invalid Kuaikan URL.")
        series_id = match.group(1)
        
        # 🟢 ATTEMPT 1: High-Speed API
        api_url = f"https://api.kuaikanmanhua.com/v1/topics/{series_id}"
        try:
            api_res = self.session.get(api_url, timeout=10)
            if api_res.status_code == 200:
                data = api_res.json()
                if data.get('code') == 200 and 'data' in data:
                    t_data = data['data']
                    title = t_data.get('title', f"Kuaikan_{series_id}")
                    image_url = t_data.get('cover_image_url')
                    
                    comics = sorted(t_data.get('comics', []), key=lambda x: x.get('created_at', 0))
                    all_chapters = []
                    
                    for idx, ch in enumerate(comics):
                        cid = str(ch.get('id'))
                        ch_title = ch.get('title', f"Chapter {idx+1}")
                        is_locked = ch.get('is_free', True) is False
                        all_chapters.append({
                            'id': cid,
                            'number_text': f"Ch.{idx+1}",
                            'title': f"Ch.{idx+1} - {ch_title}",
                            'url': f"https://www.kuaikanmanhua.com/web/comic/{cid}/",
                            'is_locked': is_locked
                        })
                    
                    if all_chapters:
                        return title, len(all_chapters), all_chapters, image_url, series_id
        except Exception as e:
            logger.warning(f"[Kuaikan] API fallback triggered: {e}")

        # 🟢 ATTEMPT 2: Nuxt HTML Fallback
        target_url = f"https://www.kuaikanmanhua.com/web/topic/{series_id}/"
        res = self.session.get(target_url, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')

        og_title = soup.find("meta", property="og:title")
        title = og_title["content"].split('|')[0].strip() if og_title else f"Kuaikan_{series_id}"
        
        og_image = soup.find("meta", property="og:image")
        image_url = og_image["content"] if og_image else None

        all_chapters, seen_ids = [], set()
        
        # Mine Nuxt state using regex
        nuxt_script = re.search(r'window\.__NUXT__\s*=\s*(.+?)(?:;</script>|$)', res.text, re.DOTALL)
        if nuxt_script:
            # Look for id and title pairs within the comics array
            items = re.findall(r'id\s*:\s*(\d+).*?title\s*:\s*["\']([^"\']+)["\']', nuxt_script.group(1))
            for cid, ch_title in items:
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    all_chapters.append({
                        'id': cid,
                        'url': f"https://www.kuaikanmanhua.com/web/comic/{cid}/",
                        'title_raw': ch_title,
                        'is_locked': False # Difficult to parse from raw Nuxt regex
                    })

        # Ensure Chronological sorting
        if all_chapters and len(all_chapters) > 1:
            if int(all_chapters[0]['id']) > int(all_chapters[-1]['id']):
                all_chapters.reverse()

        for idx, ch in enumerate(all_chapters):
            ch['number_text'] = f"Ch.{idx+1}"
            ch['title'] = f"Ch.{idx+1} - {ch.get('title_raw', f'Chapter {idx+1}')}"

        return title, len(all_chapters), all_chapters, image_url, series_id

    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Kuaikan] 🕷️ EXTRACTING: {task.title}")
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # 🟢 1. ROBUST DOWNLOAD SESSION
        dl_session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry))
        dl_session.headers.update({'Referer': 'https://www.kuaikanmanhua.com/'})
        
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.kuaikanmanhua.com')

        chapter_id = task.episode_id
        image_urls = []
        
        # 🟢 2. EXTRACT IMAGES
        res = self.session.get(task.url, timeout=20)
        
        if "login" in res.url or "pay" in res.url:
            raise ScraperError("Chapter is locked/VIP. Check Kuaikan cookies.")
        
        # Search the Nuxt payload for the comic_images array
        nuxt_script = re.search(r'window\.__NUXT__\s*=\s*(.+?)(?:;</script>|$)', res.text, re.DOTALL)
        if nuxt_script:
            images_block = re.search(r'comic_images\s*:\s*\[(.*?)\]', nuxt_script.group(1))
            if images_block:
                urls = re.findall(r'url\s*:\s*["\'](https?://[^"\']+)["\']', images_block.group(1))
                # Unescape Unicode JSON slashes
                image_urls = [u.replace('\\u002F', '/').replace('\\/', '/') for u in urls]

        # Fallback regex if comic_images mapping is obfuscated
        if not image_urls:
            raw_urls = re.findall(r'(https?://[^"\'\s,;]+\.(?:jpg|jpeg|png|webp)[^"\'\s,;]*)', res.text)
            valid_cdns = ['kkmh.com', 'v3mh.com', 'kuaikanmanhua.com']
            for u in raw_urls:
                u = u.replace('\\u002F', '/').replace('\\/', '/')
                if any(cdn in u for cdn in valid_cdns) and not any(x in u for x in ['avatar', 'icon', 'banner']):
                    if u not in image_urls:
                        image_urls.append(u)

        if not image_urls:
            raise ScraperError(f"No images found for {task.chapter_str}. Payload may be obfuscated.")

        # 🟢 3. SAVE MANIFEST
        image_data = [{'file': f"page_{idx+1:03d}.jpg", 'url': url} for idx, url in enumerate(image_urls)]
        with open(os.path.join(output_dir, "math.json"), "w") as f:
            json.dump(image_data, f)

        # 🟢 4. PACED PARALLEL DOWNLOAD
        task.status = TaskStatus.DOWNLOADING
        def download_worker(item):
            time.sleep(0.3) # Avoid IP blocks
            res = dl_session.get(item['url'], timeout=30)
            res.raise_for_status()
            with open(os.path.join(output_dir, item['file']), 'wb') as f:
                f.write(res.content)

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(download_worker, image_data))
                
        return output_dir