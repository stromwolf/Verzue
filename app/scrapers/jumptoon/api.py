import os
import re
import json
import logging
import math
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

# Use curl_cffi for high-quality browser impersonation
import requests
import requests as std_requests
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = std_requests

logger = logging.getLogger("JumptoonApi")

class JumptoonApiScraper(BaseScraper):
    BASE_URL = "https://jumptoon.com"

    def __init__(self):
        # Mimic Chrome fingerprint to bypass WAF
        self.session = curl_requests.Session(impersonate="chrome")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        })
        self._load_cookies_initial()

    def _create_fresh_session(self):
        """Creates a fresh, isolated session with standard headers and cookies."""
        session = curl_requests.Session(impersonate="chrome")
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        })
        self._inject_cookies_into_session(session)
        return session

    def _inject_cookies_into_session(self, session):
        """Injects deduplicated cookies into the provided session from all Jumptoon accounts."""
        jt_dir = Settings.SECRETS_DIR / "jumptoon"
        if not jt_dir.exists(): return
        
        cookie_paths = sorted(list(jt_dir.glob("*.json")))
        if Settings.COOKIES_FILE.exists():
            cookie_paths.append(Settings.COOKIES_FILE)
        
        # 🛡️ Deduplication: Use a dict to ensure 'last file wins' for the same cookie name
        cookie_dict = {}
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                cookies_list = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                for c in cookies_list:
                    name, value = c.get('name'), c.get('value')
                    if name and value:
                        cookie_dict[name] = c # Store the whole dict to preserve domain/path if needed
            except Exception: continue
        
        # Apply deduplicated cookies
        for name, c in cookie_dict.items():
            value = c.get('value')
            raw_domain = c.get('domain', 'jumptoon.com').lstrip('.')
            session.cookies.set(name, value, domain=raw_domain, path=c.get('path', '/'))
            # Also set for the dot domain to be safe, but deduplicated by name
            session.cookies.set(name, value, domain='.' + raw_domain, path=c.get('path', '/'))
        
        if cookie_dict:
            logger.debug(f"[Jumptoon] 🍪 Injected {len(cookie_dict)} deduplicated cookies.")

    def is_session_valid(self):
        """Checks if the current session is still authenticated."""
        try:
            # Hit a member-only endpoint or a profile page
            res = self.session.get(f"{self.BASE_URL}/mypage", timeout=15, allow_redirects=False)
            # If we get a 200, we are likely logged in. If 302 to login, session expired.
            return res.status_code == 200
        except Exception:
            return False

    def _load_cookies_initial(self):
        """Injects cookies into the API session from all Jumptoon accounts and logs the count."""
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
            fresh_session = std_requests.Session() # Use std_requests here as per original
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
        logger.info(f"[Jumptoon] 🔍 Link analysis triggered for: {url}")
        match = re.search(r'jumptoon\.com/series/([a-zA-Z0-9]+)', url)
        if not match: raise ScraperError("Invalid Jumptoon URL.")
        series_id = match.group(1)
        
        # 1. Fetch page 1 using a CLEAN session (no cookies to avoid WAF redirect loops)
        # Use a specific known-working profile (chrome110)
        logger.info(f"[Jumptoon] 🕷️  Fetching metadata for: {series_id} (Clean Session/chrome110)")
        clean_session = curl_requests.Session(impersonate="chrome110")
        clean_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': f"{self.BASE_URL}/"
        })
        
        try:
            res = clean_session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page=1", timeout=30)
            logger.info(f"[Jumptoon] 📥 Received response: Status {res.status_code}, Length {len(res.text)}")
        except Exception as e:
            logger.error(f"[Jumptoon] ❌ Network request failed for {series_id}: {e}")
            raise ScraperError(f"Failed to fetch Jumptoon page: {e}")

        html_content = res.text
        
        # 2. Extract metadata from HTML tags (more reliable than escaped JSON)
        # Extract title from <h1>
        logger.info("[Jumptoon] 📝 Extracting series title...")
        title_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content, re.DOTALL)
        title = "Unknown"
        if title_match:
            title = BeautifulSoup(title_match.group(1), "html.parser").get_text().strip()
            title = title.replace('\\', '').rstrip() # Clean up any trailing escaping artifacts
        
        # Fallback to JSON if <h1> failed or returned "Unknown"
        if title == "Unknown":
            logger.info("[Jumptoon] ⚠️ Title not found in H1, trying JSON fallback...")
            title_json = re.search(r'\\?\"series\\?\":\s*{\\?\"name\\?\":\s*\\?\"([^\"]+)\\?\"', html_content)
            if title_json:
                title = title_json.group(1).replace('\\\\', '\\').replace('\\"', '"').rstrip('\\')
        
        logger.info(f"[Jumptoon] 📖 Title identified: {title}")
        
        # Extract poster from the series DETAIL page (seriesThumbnailV2ImageUrl lives there,
        # not on the episodes listing page that was fetched above)
        image_url = None
        try:
            logger.info(f"[Jumptoon] 🖼️  Fetching series detail page for poster URL...")
            res_detail = clean_session.get(f"{self.BASE_URL}/series/{series_id}/", timeout=20)
            detail_text = res_detail.text
            img_match = re.search(r'\\?\"seriesThumbnailV2ImageUrl\\?\":\s*\\?\"([^\"]+)\\?\"', detail_text)
            if img_match:
                base_url = img_match.group(1).replace('\\/', '/').rstrip('\\')
                image_url = base_url.split('?')[0] + '?auto=avif-webp&width=3840'
                logger.info(f"[Jumptoon] ✅ Poster extracted: {image_url}")
        except Exception as e:
            logger.warning(f"[Jumptoon] ⚠️ Could not fetch series detail page for poster: {e}")

        # Fallback: check the episodes page HTML in case it has the field
        if not image_url:
            img_match2 = re.search(r'\\?\"seriesThumbnailV2ImageUrl\\?\":\s*\\?\"([^\"]+)\\?\"', html_content)
            if img_match2:
                base_url = img_match2.group(1).replace('\\/', '/').rstrip('\\')
                image_url = base_url.split('?')[0] + '?auto=avif-webp&width=3840'
                logger.info(f"[Jumptoon] ✅ Poster extracted (episodes page fallback): {image_url}")

        if image_url and not image_url.startswith('http'):
            image_url = None  # Safety: discard relative/malformed URLs
        
        # Extract total count - try multiple formats
        logger.info("[Jumptoon] 🔢 Extracting total chapter count...")
        # 1. HTML tag with comments (some series)
        total_match_comment = re.search(r'<h2>全<!-- -->(\d+)<!-- -->話</h2>', html_content)
        # 2. Standard HTML tag (other series)
        total_match_plain = re.search(r'<h2>全(\d+)話</h2>', html_content)
        
        if total_match_comment:
            total_chapters_reported = int(total_match_comment.group(1))
        elif total_match_plain:
            total_chapters_reported = int(total_match_plain.group(1))
        else:
            # 3. Fallback to JSON (very robust)
            # Prioritize the count associated with SeriesEpisodeEdge as per user request
            # Use bounded wildcard .{0,300}? to prevent ReDoS (catastrophic backtracking)
            total_edge_match = re.search(r'SeriesEpisodeEdge.{0,300}?totalCount\\?\":\s*(\d+)', html_content)
            if total_edge_match:
                total_chapters_reported = int(total_edge_match.group(1))
            else:
                # Find all occurrences and take the maximum found
                total_jsons = re.findall(r'\\?\"total(?:Episode)?Count\\?\":\s*\\?\"?(\d+)\\?\"?', html_content)
                if total_jsons:
                    total_chapters_reported = max(int(val) for val in total_jsons)
                else:
                    total_chapters_reported = 0
        
        logger.info(f"[Jumptoon] 📊 Total Chapters: {total_chapters_reported}")
        
        # 3. Chapter Fetching Logic
        logger.info("[Jumptoon] 🚀 Parsing Page 1 chapters...")
        all_chapters = []
        seen_ids = set()

        # Always parse Page 1 first
        new_chaps = self._parse_page_data(html_content, 1, seen_ids)
        all_chapters.extend(new_chaps)

        # Pre-fetch the LAST page so the "Latest" chapter is immediately available
        if total_chapters_reported > 30:
            last_page = math.ceil(total_chapters_reported / 30)
            if last_page > 1:
                logger.info(f"[Jumptoon] ⏩ Pre-fetching the FINAL page ({last_page}) to capture the latest chapters...")
                try:
                    res_last = clean_session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page={last_page}", timeout=30)
                    if res_last.status_code == 200:
                        last_chaps = self._parse_page_data(res_last.text, last_page, seen_ids)
                        all_chapters.extend(last_chaps)
                except Exception as e:
                    logger.error(f"[Jumptoon] ❌ Failed to pre-fetch final page {last_page}: {e}")

        # Sort all chapters numerically by notation
        def extract_num(notat):
            m = re.search(r'\d+', notat)
            return int(m.group()) if m else 0
        
        all_chapters.sort(key=lambda x: extract_num(x['notation']))

        return title, total_chapters_reported, all_chapters, image_url, series_id

    def _parse_page_data(self, html_str, page_num, seen_ids):
        clean_json = html_str.replace('\\/', '/').replace('\\"', '"')
        pattern = r'\"id\":\"(\d+)\".*?\"notation\":\"([^\"]+)\".*?\"title\":\"([^\"]*)\"'
        matches = re.findall(pattern, clean_json)
        
        logger.info(f"[Jumptoon]   🔍 Page {page_num}: Found {len(matches)} raw chapter matches in JSON.")
        
        page_chapters = []
        for ep_id, notation, ch_title in matches:
            if ep_id in seen_ids: continue

            # 🟢 FILTER: Skip chapters that are "COMING SOON" / not yet released.
            # The <li> element for upcoming chapters contains id="{ep_id}" and has
            # either "coming-soon" image or "次回更新" (next update) badge.
            li_start = html_str.find(f'id="{ep_id}"')
            if li_start != -1:
                li_window = html_str[li_start:li_start + 1500]
                if 'coming-soon' in li_window or '次回更新' in li_window:
                    logger.info(f"[Jumptoon]   ⏭️ Skipping upcoming chapter ep_id={ep_id} ({notation})")
                    continue

            seen_ids.add(ep_id)

            segment_idx = clean_json.find(f'\"id\":\"{ep_id}\"')
            is_locked = True
            if segment_idx != -1:
                segment = clean_json[segment_idx:segment_idx+1000]
                is_locked = not ('"offerType":"FREE"' in segment or '"isPurchased":true' in segment)
            # Check for "UP" badge strictly associated with this episode ID
            # The HTML structure has the UP badge *before* the a tag with the episode ID
            up_pattern = rf'>UP</b>.*?href=\"/series/[^/]+/episodes/{ep_id}/\"'
            is_new = bool(re.search(up_pattern, html_str, re.DOTALL))

            page_chapters.append({
                'id': ep_id,
                'title': ch_title.strip(),
                'notation': notation.strip(),
                'is_locked': is_locked,
                'is_new': is_new
            })
        return page_chapters

    def fetch_more_chapters(self, series_id: str, target_jt_page: int, seen_ids: set, skip_pages: list = None):
        skip_pages = skip_pages or []
        logger.info(f"[Jumptoon] 🕷️ Fetching additional chapters up to Jumptoon Page {target_jt_page} for {series_id}")
        clean_session = curl_requests.Session(impersonate="chrome110")
        clean_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': f"{self.BASE_URL}/"
        })
        
        new_chapters = []
        # We start from page 2 since page 1 is already fetched in info
        # Jumptoon pages start page 1 as ?page=1
        for page_num in range(2, target_jt_page + 1):
            if page_num in skip_pages:
                logger.info(f"[Jumptoon] ⏩ Skipping page {page_num} (already pre-fetched).")
                continue
            try:
                res = clean_session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page={page_num}", timeout=30)
                if res.status_code == 200:
                    chaps = self._parse_page_data(res.text, page_num, seen_ids)
                    if not chaps: break # No more chapters found
                    new_chapters.extend(chaps)
                else: break
            except Exception as e:
                logger.error(f"[Jumptoon] ❌ Failed to fetch page {page_num}: {e}")
                break
        return new_chapters
    
    def scrape_chapter(self, task, output_dir):
        logger.info(f"[Jumptoon] 🕷️  EXTRACTING: {task.title}")
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        dl_session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
        
        for k, v in self.session.cookies.items():
            dl_session.cookies.set(k, v, domain='.jumptoon.com')

        # Use a clean chrome110 session with unified deduplicated cookies
        fetch_session = curl_requests.Session(impersonate="chrome110")
        self._inject_cookies_into_session(fetch_session)
        
        # Ensure trailing slash to avoid 301 redirects
        target_url = task.url if task.url.endswith('/') else f"{task.url}/"
        logger.info(f"[Jumptoon] 🌐 Fetching chapter page: {target_url}")
        
        try:
            res = fetch_session.get(target_url, timeout=30)
        except Exception as first_err:
            logger.warning(f"[Jumptoon] ⚠️ Cookie session failed ({first_err}), retrying without cookies...")
            bare_session = curl_requests.Session(impersonate="chrome110")
            res = bare_session.get(target_url, timeout=30)
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
