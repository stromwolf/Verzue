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

        with ThreadPoolExecutor(max_workers=5) as exe:
            futures = [exe.submit(fetch_page, p) for p in range(1, max_page + 1)]
            for f in as_completed(futures):
                for item in f.result():
                    if item['id'] not in seen_ids:
                        seen_ids.add(item['id']); all_chapters.append(item)

        all_chapters.sort(key=lambda x: int(x['id']))
        
        image_url = self._fetch_high_res_poster(title) or (soup.find("meta", property="og:image")["content"] if soup.find("meta", property="og:image") else None)

        return title, len(all_chapters), all_chapters, image_url, series_id

    def _fetch_high_res_poster(self, title):
        try:
            res = self.session.get(f"{self.BASE_URL}/search/{quote(title)}/", timeout=10)
            soup = BeautifulSoup(res.text, 'html.parser')
            img = soup.select_one("ul#load-searchResultList li img")
            if img and img.get('srcset'):
                return img['srcset'].split(',')[-1].strip().split(' ')[0]
        except: return None
        
    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Jumptoon] 🕷️  EXTRACTING: {task.title}")
        self._load_cookies()
        
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
        with open(os.path.join(output_dir, "math.json"), "w") as f:
            json.dump(image_data, f)
            
        total = len(image_data)
        logger.info(f"[Jumptoon] ✅ Success! Filtered out dummies. Mapped {total} REAL pages.")

        task.status = TaskStatus.DOWNLOADING
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self._download_image, item['url'], item['file'], output_dir) 
                      for item in image_data]
            for f in as_completed(futures):
                f.result()
                
        return output_dir

    def _download_image(self, url, filename, out_dir):
        for attempt in range(3):
            try:
                res = self.session.get(url, timeout=30)
                if res.status_code == 200:
                    with open(os.path.join(out_dir, filename), 'wb') as f:
                        f.write(res.content)
                    return
                time.sleep(1)
            except:
                if attempt == 2: raise
                time.sleep(2)
