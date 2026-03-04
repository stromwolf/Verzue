import os
import re
import json
import logging
import math
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

# Use curl_cffi for high-quality browser impersonation
try:
    from curl_cffi import requests
except ImportError:
    import requests

logger = logging.getLogger("JumptoonApi")

class JumptoonApiScraper(BaseScraper):
    BASE_URL = "https://jumptoon.com"

    def __init__(self):
        # Mimic Chrome 120 fingerprint to bypass WAF
        self.session = requests.Session(impersonate="chrome120")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        })
        self._load_cookies()

    def _load_cookies(self):
        """Injects cookies into the API session from all Jumptoon accounts."""
        jt_dir = Settings.SECRETS_DIR / "jumptoon"
        jt_dir.mkdir(parents=True, exist_ok=True)
        
        cookie_paths = list(jt_dir.glob("*.json"))
        if Settings.COOKIES_FILE.exists():
            cookie_paths.append(Settings.COOKIES_FILE)
        
        self.session.cookies.clear()
        
        total_loaded = 0
        files_found = 0
        for path in cookie_paths:
            if not path.exists(): continue
            files_found += 1
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                cookies_list = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                
                file_count = 0
                for c in cookies_list:
                    name = c.get('name')
                    value = c.get('value')
                    if not name or not value: continue
                    raw_domain = c.get('domain', 'jumptoon.com').lstrip('.')
                    self.session.cookies.set(name, value, domain=raw_domain, path=c.get('path', '/'))
                    self.session.cookies.set(name, value, domain='.' + raw_domain, path=c.get('path', '/'))
                    file_count += 1
                
                total_loaded += file_count
                logger.debug(f"[Jumptoon] 🍪 Loaded {file_count} cookies from {path.name}")
            except Exception as e:
                logger.error(f"[Jumptoon] Cookie load failed from {path.name}: {e}")

        if total_loaded > 0:
            logger.info(f"[Jumptoon] ✅ Multi-Account Sync: {total_loaded} cookies active from {files_found} sources.")

    def get_series_info(self, url: str):
        match = re.search(r'jumptoon\.com/series/([a-zA-Z0-9]+)', url)
        if not match: raise ScraperError("Invalid Jumptoon URL.")
        series_id = match.group(1)
        
        res = self.session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page=1", timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')

        # 🟢 CORRECT POSTER: Use the specific vertical V2 key from JSON
        image_url = None
        clean_json = res.text.replace('\\/', '/').replace('\\"', '"')
        img_match = re.search(r'seriesThumbnailV2ImageUrl":"(https?://[^"]+)"', clean_json)
        if img_match:
            image_url = img_match.group(1).split('?')[0] + "?auto=avif-webp&width=3840"

        # 🟢 SAFETY FALLBACK: If V2 is missing, grab the default meta image
        if not image_url:
            og_img = soup.find("meta", property="og:image")
            if og_img:
                image_url = og_img["content"]
                if 'width=' in image_url:
                    image_url = re.sub(r'width=\d+', 'width=3840', image_url)
                else:
                    sep = '&' if '?' in image_url else '?'
                    image_url += f"{sep}width=3840"

        # 🟢 CHAPTERS: Extract with "UP" Badge detection
        all_chapters = []
        episodes_data = re.findall(
            r'\\?"id\\?":\\?"(\d+)\\?",\\?"number\\?":\\?"(\d+)\\?",\\?"notation\\?":\\?"([^"]+)\\?",\\?"title\\?":\\?"([^"]+)\\?".*?\\?"isNew\\?":\s*(true|false)', 
            res.text
        )
        
        for ep_id, num, notation, title, is_new_str in episodes_data:
            is_new = is_new_str.lower() == "true"
            # Lock detection logic
            is_locked = f'\\"id\\":\\"{ep_id}\\",\\"isPurchased\\":false' in res.text
            all_chapters.append({
                'id': ep_id,
                'title': title.replace("UP", "").strip(),
                'notation': notation,
                'is_locked': is_locked,
                'is_new': is_new
            })

        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Unknown"
        return title, len(all_chapters), all_chapters, image_url, series_id

    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Jumptoon] 🕷️  EXTRACTING: {task.title}")
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        dl_session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.jumptoon.com')

        res = self.session.get(task.url, timeout=20)
        clean_text = res.text.replace('\\/', '/').replace('\\u0026', '&').replace('\\"', '"').replace('\\\\', '\\')

        s_id = task.series_id_key
        ep_num = "".join(filter(str.isdigit, task.chapter_str))
        
        target_path = f"contents.jumptoon.com/series/{s_id}/episode/{ep_num}/"
        logger.info(f"[Jumptoon] 🎯 Target Path Locked: {target_path}")

        url_pattern = rf'https?://{re.escape(target_path)}[^\s\"\\>]+'
        found_urls = list(dict.fromkeys(re.findall(url_pattern, clean_text)))

        image_data = []
        for url in found_urls:
            if "preview" in url.lower() or "thumb" in url.lower() or "width=" in url.lower():
                continue

            start_pos = clean_text.find(url)
            window = clean_text[start_pos : start_pos + 300]
            
            seed_match = re.search(r'\"seed\":(\d+)', window)
            seed_val = int(seed_match.group(1)) if seed_match else f"{s_id}:{ep_num}"

            if url not in [d['url'] for d in image_data]:
                image_data.append({
                    'file': f"page_{len(image_data)+1:03d}.webp",
                    'url': url,
                    'seed': seed_val
                })

        if not image_data:
            raise ScraperError(f"Manifest not found for Ch.{ep_num}.")

        clean_manifest = [{'file': d['file'], 'url': d['url']} for d in image_data]
        with open(os.path.join(output_dir, "math.json"), "w") as f:
            json.dump(clean_manifest, f)
            
        total = len(image_data)
        logger.info(f"[Jumptoon] ✅ Success! Filtered out dummies. Mapped {total} REAL pages.")

        task.status = TaskStatus.DOWNLOADING
        def download_worker(item):
            time.sleep(0.3)
            self._download_image_robust(dl_session, item['url'], item['file'], output_dir, item['seed'])

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(download_worker, image_data))
                
        return output_dir

    def _download_image_robust(self, dl_session, url, filename, out_dir, seed):
        from app.services.image.optimizer import ImageOptimizer
        res = dl_session.get(url, timeout=30)
        res.raise_for_status()
        raw_path = os.path.join(out_dir, f"raw_{filename}")
        final_path = os.path.join(out_dir, filename)
        with open(raw_path, 'wb') as f:
            f.write(res.content)
        try:
            if seed:
                img = ImageOptimizer.unscramble_jumptoon_v2(raw_path, seed)
                img.save(final_path, format="WEBP", quality=100)
                os.remove(raw_path)
            else:
                os.rename(raw_path, final_path)
        except Exception as e:
            logger.error(f"Failed to unscramble {filename}: {e}")
            if os.path.exists(raw_path):
                os.rename(raw_path, final_path)
