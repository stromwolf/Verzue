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
        
        # Scan data/secrets/jumptoon/*.json + legacy cookies.json
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
                
                # Robust parsing: Handle both Selenium [{}, {}] and legacy {k: v} formats
                cookies_list = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                
                file_count = 0
                for c in cookies_list:
                    name = c.get('name')
                    value = c.get('value')
                    if not name or not value: continue
                    
                    # 🟢 DOMAIN NORMALIZATION
                    # Jumptoon uses subdomains (contents., assets.) for images.
                    # Setting the domain to '.jumptoon.com' ensures cookies are shared.
                    raw_domain = c.get('domain', 'jumptoon.com').lstrip('.')
                    
                    # Set for base domain and all subdomains
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
        # 🟢 FIX: Broadened regex to accept ANY alphanumeric series ID (JT, MD, etc.)
        match = re.search(r'jumptoon\.com/series/([a-zA-Z0-9]+)', url)
        if not match: raise ScraperError("Invalid Jumptoon URL.")
        
        series_id = match.group(1)
        base_series_url = f"{self.BASE_URL}/series/{series_id}/episodes/"
        
        # Request HTML for metadata
        self.session.headers['Accept'] = 'text/html,application/xhtml+xml,application/xml'
        res = self.session.get(f"{base_series_url}?page=1", timeout=15)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')

        # 1. Clean Title
        title = "Unknown"
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = re.sub(r'\s*([-|]|–)\s*(全話一覧|ジャンプTOON|Jumptoon).*', '', og_title["content"]).strip()

        # 2. Max Page (30 items per page)
        max_page = 1
        page_text = soup.get_text()
        count_match = re.search(r'全(\d+)話', page_text)
        if count_match:
            max_page = math.ceil(int(count_match.group(1)) / 30)

        # 3. Chapter Extraction (Parallel)
        all_chapters, seen_ids = [], set()

        def fetch_page(p_num):
            try:
                p_res = self.session.get(f"{base_series_url}?page={p_num}", timeout=15)
                p_soup = BeautifulSoup(p_res.text, 'html.parser')
                # Find only real episode links, skip generic 'Read' buttons
                links = p_soup.find_all('a', href=re.compile(r'/episodes/\d+'))
                
                results = []
                for link in links:
                    href = link['href']
                    cid = href.strip('/').split('/')[-1]
                    if not cid.isdigit(): continue
                    
                    raw_text = link.get_text(" ", strip=True)
                    if "はじめから" in raw_text or "最新話" in raw_text: continue
                    
                    # 🟢 SMART LOCK DETECTION (Jumptoon V4)
                    # We check the 'link' AND its parent container for status indicators.
                    container = link.find_parent(['li', 'div']) or link
                    container_text = container.get_text(" ", strip=True)
                    
                    # 'alt="無料"' is the icon for completely free chapters
                    is_free_icon = container.find('img', alt=re.compile(r'無料|free')) is not None
                    # '初回無料' (Initial Free) requires a visit/click
                    needs_click = "初回" in container_text and "無料" in container_text
                    
                    is_locked = needs_click or not is_free_icon

                    results.append({
                        'id': cid,
                        'title': raw_text.replace("無料", "").replace("初回", "").strip(),
                        'url': urljoin(self.BASE_URL, href),
                        'is_locked': is_locked
                    })
                return results
            except: return []

        # 🟢 FIX: Use map() to enforce strict chronological page order
        with ThreadPoolExecutor(max_workers=5) as exe:
            # exe.map guarantees the results are returned in the exact order of the input (Page 1, then Page 2, etc.)
            for page_results in exe.map(fetch_page, range(1, max_page + 1)):
                for item in page_results:
                    if item['id'] not in seen_ids:
                        seen_ids.add(item['id'])
                        all_chapters.append(item)

        # 🟢 FIX: Remove the flawed ID-based sort completely. 
        # By enforcing page order above, the list is already in the exact visual order presented by the website!
        
        # 4. Extract High-Res Poster directly from Next.js JSON state (Fastest Method)
        image_url = None
        
        # Unescape the Next.js JSON payload (\", \/, \u0026 -> ", /, &)
        clean_html = res.text.replace('\\"', '"').replace('\\/', '/').replace('\\u0026', '&')
        
        # Priority 1: ThumbnailV2, Priority 2: HeroImage, Priority 3: SignboardLarge
        img_match = re.search(r'"seriesThumbnailV2ImageUrl":"(https://assets\.jumptoon\.com/series/[^"]+)"', clean_html)
        if not img_match:
            img_match = re.search(r'"seriesHeroImageUrl":"(https://assets\.jumptoon\.com/series/[^"]+)"', clean_html)
        if not img_match:
            img_match = re.search(r'"seriesSignboardLargeImageUrl":"(https://assets\.jumptoon\.com/series/[^"]+)"', clean_html)

        if img_match:
            raw_url = img_match.group(1)
            # Force max resolution (width=3840) for Discord
            if 'width=' in raw_url:
                image_url = re.sub(r'width=\d+', 'width=3840', raw_url)
            else:
                sep = '&' if '?' in raw_url else '?'
                image_url = f"{raw_url}{sep}width=3840"
        else:
            # Absolute fallback to OG Image meta tag
            og_img = soup.find("meta", property="og:image")
            if og_img:
                image_url = og_img["content"]
                if 'width=' in image_url:
                    image_url = re.sub(r'width=\d+', 'width=3840', image_url)
                else:
                    sep = '&' if '?' in image_url else '?'
                    image_url += f"{sep}width=3840"

        return title, len(all_chapters), all_chapters, image_url, series_id

    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Jumptoon] 🕷️  EXTRACTING: {task.title}")
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # 🟢 1. ROBUST DOWNLOAD SESSION
        dl_session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        
        # Sync cookies from curl_cffi
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.jumptoon.com')

        # 1. Fetch raw source
        res = self.session.get(task.url, timeout=20)
        
        # 2. Universal Unescape (Flattening the RSC stream)
        clean_text = res.text.replace('\\/', '/').replace('\\u0026', '&').replace('\\"', '"').replace('\\\\', '\\')

        # 3. CONTEXTUAL PATH LOCKING
        s_id = task.series_id_key
        # Extract pure number (e.g. '3')
        ep_num = "".join(filter(str.isdigit, task.chapter_str))
        
        # Target only high-res images in the specific episode folder
        # We look for the 'v2_0_0' or 'v1_0_0' markers which denote scrambled content
        target_path = f"contents.jumptoon.com/series/{s_id}/episode/{ep_num}/"
        logger.info(f"[Jumptoon] 🎯 Target Path Locked: {target_path}")

        # 4. WINDOW-SEARCH MINING (The Fix)
        # First, find ALL URLs matching our locked path
        url_pattern = rf'https?://{re.escape(target_path)}[^\s\"\\>]+'
        found_urls = list(dict.fromkeys(re.findall(url_pattern, clean_text)))

        image_data = []
        for url in found_urls:
            # Skip preview/thumbnail variants
            if "preview" in url.lower() or "thumb" in url.lower() or "width=" in url.lower():
                continue

            # --- SEED SEARCH ---
            # We look for the "seed":XXXX key in a 300-character window following the URL
            start_pos = clean_text.find(url)
            window = clean_text[start_pos : start_pos + 300]
            
            seed_match = re.search(r'\"seed\":(\d+)', window)
            seed_val = int(seed_match.group(1)) if seed_match else None
            
            # Use page index as fallback seed if specific seed is missing (Test 3 Logic)
            if seed_val is None:
                seed_val = f"{s_id}:{ep_num}"

            if url not in [d['url'] for d in image_data]:
                image_data.append({
                    'file': f"page_{len(image_data)+1:03d}.webp",
                    'url': url,
                    'seed': seed_val
                })

        # 5. VALIDATION & ERROR REPORTING
        if not image_data:
            logger.error(f"[Jumptoon] ❌ Precision mining failed for {target_path}")
            # Diagnostic: Show what was actually found to identify naming shifts
            any_images = re.findall(r'https?://contents\.jumptoon\.com/series/[^\"\\\s>]+', clean_text)
            logger.info(f"   💡 Found {len(any_images)} total CDN links, but none matched Ch.{ep_num}")
            if any_images:
                logger.info(f"   💡 Example found path: {any_images[0]}")
            
            raise ScraperError(f"Manifest not found for Ch.{ep_num}. Check if the chapter number in Drive matches the site.")

        # 6. SAVE MANIFEST & EXECUTE
        # 🟢 FIX 1: Remove the 'seed' from math.json so the Stitcher doesn't try to unscramble it a second time!
        clean_manifest = [{'file': d['file'], 'url': d['url']} for d in image_data]
        with open(os.path.join(output_dir, "math.json"), "w") as f:
            json.dump(clean_manifest, f)
            
        total = len(image_data)
        logger.info(f"[Jumptoon] ✅ Success! Filtered out dummies. Mapped {total} REAL pages.")

        # 🟢 2. PACED PARALLEL DOWNLOAD & UNSCRAMBLING
        task.status = TaskStatus.DOWNLOADING
        def download_worker(item):
            time.sleep(0.3) # Avoid hitting WAF limit
            # Pass the seed to the robust downloader
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
        
        # Save the scrambled image temporarily
        with open(raw_path, 'wb') as f:
            f.write(res.content)
            
        # 🟢 FIX 2: Unscramble the image immediately inside this parallel thread
        try:
            if seed:
                img = ImageOptimizer.unscramble_jumptoon_v2(raw_path, seed)
                img.save(final_path, format="WEBP", quality=100)
                os.remove(raw_path) # Delete the raw scrambled file
            else:
                os.rename(raw_path, final_path)
        except Exception as e:
            logger.error(f"Failed to unscramble {filename}: {e}")
            if os.path.exists(raw_path):
                os.rename(raw_path, final_path)