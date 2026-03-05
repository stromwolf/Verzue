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


    def _fetch_poster_via_search(self, title: str, series_id: str):
        try:
            from urllib.parse import quote
            encoded_title = quote(title)
            # 🟢 FIX 1: Use path routing to avoid Next.js redirect loops
            search_url = f"{self.BASE_URL}/search/{encoded_title}"
            
            # 🟢 FIX 2: Use an unauthenticated session. Jumptoon's search API 
            # throws a 500 error if hit with expired or certain auth cookies.
            fresh_session = requests.Session(impersonate="chrome120")
            fresh_session.headers.update({
                'User-Agent': self.session.headers.get('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'),
                'Accept-Language': 'ja,en-US;q=0.9',
            })
            
            res = fresh_session.get(search_url, timeout=30, allow_redirects=True, max_redirects=5)
            
            # 🟢 Search PosterのHTMLから対象シリーズの画像リンクを直接抽出 
            img_match = re.search(rf'https://assets\.jumptoon\.com/series/{series_id}/[^"\'\s\\]+\.(?:png|jpg|webp|jpeg)', res.text)
            if img_match:
                clean_url = img_match.group(0).split('?')[0]
                return f"{clean_url}?auto=avif-webp&width=3840"
        except Exception as e:
            logger.error(f"Search poster fetch failed for {series_id}: {e}")
            
        return None

    def get_series_info(self, url: str):
        match = re.search(r'jumptoon\.com/series/([a-zA-Z0-9]+)', url)
        if not match: raise ScraperError("Invalid Jumptoon URL.")
        series_id = match.group(1)
        
        # Fix URL: use the main series path to avoid Next.js redirect loops, and set max_redirects=5
        # as Jumptoon embeds Next.js JSON payloads entirely on the series landing page now.
        res = self.session.get(f"{self.BASE_URL}/series/{series_id}", timeout=30, allow_redirects=True, max_redirects=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        clean_json = res.text.replace('\\/', '/').replace('\\"', '"')

        # 🟢 1. タイトルの取得
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown"

        # 🟢 2. 検索ページを経由した確実なポスター取得
        image_url = self._fetch_poster_via_search(title, series_id)
        
        if not image_url:
            og_img = soup.find("meta", attrs={"property": "og:image"})
            if og_img:
                image_url = og_img.get("content", "").split('?')[0] + "?auto=avif-webp&width=3840"

        # 🟢 3. JSONペイロードからのチャプター抽出
        all_chapters = []
        seen_ids = set()

        # extract JSON blocks containing episode details
        pattern = r'\"id\":\"(\d+)\"(?:(?:(?!\"id\":).)*?)\"notation\":\"([^\"]+)\"(?:(?:(?!\"id\":).)*?)\"title\":\"([^\"]*)\"'
        matches = re.findall(pattern, clean_json)

        for ep_id, notation, ch_title in matches:
            if ep_id in seen_ids:
                continue
            seen_ids.add(ep_id)

            # Look for the immediate properties to check if locked
            segment_idx = clean_json.find(f'"id":"{ep_id}"')
            if segment_idx != -1:
                segment = clean_json[segment_idx:segment_idx+400]
                is_free = '"offerType":"FREE"' in segment
                is_purchased = '"isPurchased":true' in segment
                is_locked = not (is_free or is_purchased)
            else:
                is_locked = True

            all_chapters.append({
                'id': ep_id,
                'title': ch_title.strip(),
                'notation': notation.strip(),
                'is_locked': is_locked,
                'is_new': False
            })

        # Sort the chapters by notation number to ensure correct ordering
        def extract_num(notat):
            m = re.search(r'\d+', notat)
            return int(m.group()) if m else 0
        all_chapters.sort(key=lambda x: extract_num(x['notation']))

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
            list(executor.map(l̥download_worker, image_data))
                
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
