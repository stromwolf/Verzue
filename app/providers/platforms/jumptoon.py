import os
import re
import time
import json
import logging
import math
import asyncio
import urllib.parse
import random
import base64
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("JumptoonProvider")

# Jumptoon releases at 00:00 JST = 15:00 UTC
JUMPTOON_RELEASE_TIME_UTC = "15:00"


class JumptoonProvider(BaseProvider):
    IDENTIFIER = "jumptoon"
    BASE_URL = "https://jumptoon.com"
    SERIES_PATH = "/series/"

    def __init__(self):
        self.session_service = SessionService()
        self.active_account_id = None
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        }
        # S-Grade Backpressure Control: Restricted concurrency (3-5) to prevent bandwidth saturation
        self._download_semaphore = asyncio.Semaphore(random.randint(3, 5))

    async def _get_authenticated_session(self):
        """Fetches a healthy session from the Vault and initializes an AsyncSession."""
        session_obj = await self.session_service.get_active_session("jumptoon")
        if not session_obj:
            raise ScraperError("No healthy sessions available for jumptoon. Use /add-cookies to fix.")

        self.active_account_id = session_obj["account_id"]
        async_session = curl_requests.AsyncSession(impersonate="chrome", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)
        
        for c in session_obj["cookies"]:
            name, value = c.get('name'), c.get('value')
            if not name or not value: continue
            raw_domain = c.get('domain', 'jumptoon.com').lstrip('.')
            async_session.cookies.set(name, value, domain=raw_domain)
            async_session.cookies.set(name, value, domain='.' + raw_domain)
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        """Checks if the provided session is still authenticated."""
        try:
            res = await session.get(f"{self.BASE_URL}/mypage", timeout=15, allow_redirects=False)
            valid = res.status_code == 200 and "ログイン・新規登録" not in res.text
            if not valid and self.active_account_id:
                await self.session_service.report_session_failure("jumptoon", self.active_account_id, "Session invalidated @ /mypage")
            elif valid:
                await self.session_service.record_session_success("jumptoon")
            return valid
        except Exception as e:
            logger.error(f"Session validation error: {e}")
            return False

    async def get_series_info(self, url: str, fast: bool = False):
        """Phase 1: Intelligence. Extraction with Telemetry."""
        # 1. Normalization: Extract series ID regardless of deep links (e.g., ?page=2 or /episodes/)
        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if series_id_match:
            series_id = series_id_match.group(1)
        else:
            series_id = url.split("?")[-1] if "?" in url else url.split("/")[-1]
            if not series_id or series_id == "episodes": 
                series_id = url.split("/")[-2]
                
        # Move to base series page for consistent metadata (title, total_chapters)
        fetch_url = f"{self.BASE_URL}/series/{series_id}/"
        logger.info(f"[Jumptoon] 🔍 Intelligence Phase for: {series_id}")
        
        # 2. Page Fetch & Auth
        auth_session = await self._get_authenticated_session()
        res = await auth_session.get(fetch_url, timeout=30, allow_redirects=True)
        
        if res.status_code in (301, 302, 303, 307, 308):
            loc = res.headers.get("Location", "Unknown")
            raise ScraperError(f"Auth Expired or Age Restricted accessing {fetch_url}. Redirected to {loc}")
            
        if res.status_code != 200:
            raise ScraperError(f"Failed to access Jumptoon: HTTP {res.status_code} on {fetch_url}")
        
        html_content = res.text
        clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_content)
        clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')

        # Extract Series ID from HTML if available (more reliable than URL)
        id_match = re.search(r'"seriesId"\s*:\s*"([^"]+)"', clean_html)
        if id_match: series_id = id_match.group(1)

        total_chapters = 0
        count_patterns = [
            r'"totalEpisodeCount"\s*:\s*"(\d+)"',
            r'"totalEpisodeCount"\s*:\s*(\d+)',
            r'"totalCount"\s*:\s*"(\d+)"',
            r'"totalCount"\s*:\s*(\d+)',
        ]
        for p in count_patterns:
            m = re.search(p, clean_html)
            if m:
                total_chapters = int(m.group(1))
                break
        
        if total_chapters == 0:
            h2_count = re.search(r'<h2>全\s*(?:<!-- -->)?\s*(\d+)\s*(?:<!-- -->)?\s*話</h2>', html_content)
            if h2_count: total_chapters = int(h2_count.group(1))

        # 2. Title Extraction
        title = series_id
        series_match = re.search(r'"series"\s*:\s*\{', clean_html)
        if series_match:
            window = clean_html[series_match.end() : series_match.end() + 2000]
            name_match = re.search(r'"name"\s*:\s*"([^"]+)"', window)
            if name_match:
                title = name_match.group(1)

        try:
            if "\\u" in title:
                title = title.encode('utf-8').decode('unicode_escape')
        except: pass
        title = title.replace('&amp;', '&').strip()
        
        if not title or title == series_id:
            h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content)
            if h1_match: 
                title = BeautifulSoup(h1_match.group(1), "html.parser").get_text().strip()
            if not title or title == series_id:
                t_tag = re.search(r'<title>(.*?)</title>', html_content, re.I)
                if t_tag:
                    title = t_tag.group(1).strip().split('|')[0].strip().split(' | ')[0].strip()

        # 3. Poster Extraction
        image_url = None
        img_match = re.search(r'"(?:seriesHeroImageUrl|seriesThumbnailV2ImageUrl)"\s*:\s*"(https:[^"]+)"', clean_html)
        if img_match:
            image_url = img_match.group(1) + '?auto=avif-webp&width=3840'
        
        # 4. Status Label Extraction (Oneshot / Completed)
        # 🟢 S-GRADE: Subscription Block Rule (Mar 25 Request)
        status_label = None
        if "読切" in html_content: status_label = "Oneshot"
        elif "完結" in html_content: status_label = "Completed"

        # 🟢 Extract Release Day (S-Grade Automation)
        release_day = None
        day_match = re.search(r'"publishDayNames"\s*:\s*\["([^"]+)"\]', clean_html)
        if day_match:
            release_day = day_match.group(1).capitalize() # e.g. "Monday"
        
        # 🟢 Initialize tracking sets early to prevent UnboundLocalError in fast-fetch
        up_ids = set()
        coming_soon_ids = set()
        self._extract_tag_ids(html_content, up_ids, coming_soon_ids)

        if fast:
            logger.info(f"[Jumptoon] Fast Fetch (Page 1 Only): {title} ({series_id})")
            # 🟢 S-GRADE: Return Page 1 chapters instead of empty list for instant UI
            all_chapters = self._parse_page_data(html_content, sees_ids=set(), up_ids=up_ids, coming_soon_ids=coming_soon_ids)
            return title, total_chapters, all_chapters, image_url, series_id, release_day, None, status_label, None

        # --- FULL CRAWL LOGIC (Only if not fast) ---
        pg_size = 30
        total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1
        all_chapters = []
        seen_ids = set()
        # up_ids and coming_soon_ids are already initialized above
        all_chapters.extend(self._parse_page_data(html_content, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids))
        
        if len(all_chapters) < pg_size or not all_chapters:
            p1_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page=1"
            p1_res = await auth_session.get(p1_url, timeout=30, allow_redirects=True)
            if p1_res.status_code in (301, 302, 303, 307, 308):
                loc = p1_res.headers.get("Location", "Unknown")
                raise ScraperError(f"Auth Expired or Age Restricted accessing {p1_url}. Redirected to {loc}")
                
            if p1_res.status_code == 200:
                self._extract_tag_ids(p1_res.text, up_ids, coming_soon_ids)
                p1_chaps = self._parse_page_data(p1_res.text, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                all_chapters.extend(p1_chaps)
        
        if total_chapters == 0 and all_chapters:
            total_chapters = len(all_chapters)

        for page_num in range(2, int(total_pages) + 1):
            p_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={page_num}"
            p_res = await auth_session.get(p_url, timeout=30, allow_redirects=True)
            if p_res.status_code in (301, 302, 303, 307, 308):
                loc = p_res.headers.get("Location", "Unknown")
                raise ScraperError(f"Auth Expired or Age Restricted accessing {p_url}. Redirected to {loc}")
                
            if p_res.status_code == 200:
                self._extract_tag_ids(p_res.text, up_ids, coming_soon_ids)
                pg_chaps = self._parse_page_data(p_res.text, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                if not pg_chaps: break 
                all_chapters.extend(pg_chaps)
            else: break

        def extract_sort_key(ch):
            # 1. Primary: Use the numeric 'number' if available
            num = ch.get('number')
            if num and str(num).isdigit(): return int(num)
            
            # 2. Secondary: Regex extraction from notation (e.g. 第1話 -> 1)
            not_match = re.search(r'(\d+)', ch.get('notation', ''))
            if not_match: return int(not_match.group(1))
            
            # 3. Tertiary: Fallback to the numeric ID (ensures chronological order for hiatuses)
            raw_id = ch.get('id')
            if raw_id and str(raw_id).isdigit(): return int(raw_id)
            
            return 0

        all_chapters.sort(key=extract_sort_key)
        for ch in all_chapters:
            if str(ch['id']) in up_ids: ch['is_new'] = True
        
        release_time = JUMPTOON_RELEASE_TIME_UTC if release_day else None
        return title, total_chapters, all_chapters, image_url, series_id, release_day, release_time, status_label, None

    async def get_new_series_list(self) -> list[dict]:
        """Fetches the list of new series from the 'new' series page."""
        url = f"{self.BASE_URL}/series/original/new/"
        logger.info(f"[Jumptoon] 🔍 Fetching new series list from: {url}")
        
        auth_session = await self._get_authenticated_session()
        res = await auth_session.get(url, timeout=30, allow_redirects=True)
        
        if res.status_code != 200:
            logger.error(f"[Jumptoon] Failed to fetch new series list: HTTP {res.status_code}")
            return []
            
        html_content = res.text
        series_list = []
        
        # Next.js App Router (RSC) stores data in __next_f script tags as escaped JSON strings.
        # 1. Find all unique JT IDs
        all_ids = sorted(list(set(re.findall(r'JT\d+', html_content))))
        
        for sid in all_ids:
            # 2. Extract title: "href":"/series/JTXXXXX","children":"TITLE"
            # Note: quotes are escaped as \\\" in the script tags
            title_pattern = rf'\\"href\\":\\"/series/{sid}\\",\\"children\\":\\"([^\\"]+)\\"'
            title_match = re.search(title_pattern, html_content)
            if not title_match:
                continue
                
            title = title_match.group(1)
            
            # 3. Extract poster: "src":"https://assets.jumptoon.com/series/JTXXXXX/..."
            image_pattern = rf'\\"src\\":\\"(https://assets.jumptoon.com/series/{sid}/[^\\"]+\.(?:png|jpg|webp))\\"'
            image_match = re.search(image_pattern, html_content)
            poster_url = image_match.group(1) if image_match else ""
            
            if poster_url and "?" in poster_url:
                poster_url = poster_url.split("?")[0]
            if poster_url:
                poster_url += "?auto=avif-webp&width=3840"
            
            series_list.append({
                "series_id": sid,
                "title": title,
                "url": f"{self.BASE_URL}/series/{sid}",
                "poster": poster_url
            })
            
        logger.info(f"[Jumptoon] Successfully extracted {len(series_list)} new series via regex.")
        return series_list

    async def sync_latest_chapters(self, url):
        """Background optimization: Directly visit the LAST page of the series to find the latest state."""
        try:
            # 1. First get total count from main page to find last page index
            title, total_count, _, _, series_id, _, _, _, genre_label = await self.get_series_info(url, fast=True)
            if total_count == 0: return []
            
            pg_size = 30
            last_page = math.ceil(total_count / pg_size)
            
            auth_session = await self._get_authenticated_session()
            last_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={last_page}"
            logger.info(f"[Jumptoon] Syncing Latest Chapters (Background): Page {last_page}")
            
            res = await auth_session.get(last_url, timeout=30)
            if res.status_code == 200:
                up_ids = set()
                coming_soon_ids = set()
                self._extract_tag_ids(res.text, up_ids, coming_soon_ids)
                latest_chaps = self._parse_page_data(res.text, sees_ids=set(), up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                
                # Sort them
                def extract_sort_key(ch):
                    num = ch.get('number')
                    if num and str(num).isdigit(): return int(num)
                    not_match = re.search(r'(\d+)', ch.get('notation', ''))
                    if not_match: return int(not_match.group(1))
                    return 0
                latest_chaps.sort(key=extract_sort_key)
                
                logger.debug(f"[Jumptoon] Sync Background: Found {len(latest_chaps)} chapters on last page.")
                return latest_chaps
        except Exception as e:
            logger.error(f"[Jumptoon] Background sync failed: {e}")
        return []

    async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set, skip_pages: list | None = None):
        """Standard method for background scans to fill in gaps if any."""
        skip_pages = skip_pages or []
        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if not series_id_match: return []
        series_id = series_id_match.group(1)
        
        session_task = self._get_authenticated_session()
        auth_session = await session_task
        
        async def fetch_page(p):
            if p in skip_pages: return []
            try:
                p_task = auth_session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page={p}", timeout=30, allow_redirects=True)
                p_res = await p_task
                if p_res.status_code == 200:
                    return self._parse_page_data(p_res.text, sees_ids=seen_ids)
            except Exception as e:
                logger.error(f"[Jumptoon] Background fetch error for page {p}: {e}")
            return []

        # 🟢 S-GRADE: Parallel Fetching
        tasks = [fetch_page(p) for p in range(1, total_pages + 1)]
        results = await asyncio.gather(*tasks)
        
        extra_chapters = []
        for pg_chaps in results:
            if pg_chaps: extra_chapters.extend(pg_chaps)
        return extra_chapters

    def _extract_tag_ids(self, html_str, up_ids, coming_soon_ids):
        """Scans HTML for UP and Coming Soon tags in <li> blocks, mutating the provided sets."""
        if not html_str: return
        
        # 1. Unescape Unicode (\u003c -> <) to handle tags hidden in JSON strings
        try:
            clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_str)
        except:
            clean_html = html_str
            
        # 2. Flatten other escapes
        clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')
        
        # 3. Extract blocks and detect tags
        # 🟢 FIX: Support alphanumeric/base64 IDs (not just \d+)
        li_blocks = re.findall(r'<li[^>]*id=["\']?([^"\'>\s]+)["\']?[^>]*>(.*?)</li>', clean_html, re.S | re.I)
        for raw_id, block in li_blocks:
            # 🟢 S-GRADE: Decode Base64 IDs to match metadata
            ep_id = str(raw_id).strip()
            if not ep_id.isdigit():
                try: 
                    import base64
                    decoded = base64.b64decode(ep_id).decode('utf-8')
                    if ':' in decoded: ep_id = str(decoded.split(':')[-1])
                except: pass
            
            # Broad but safe detection for 'UP' tag
            if re.search(r'>UP<|UP\s*</|[>{\s]UP[\s<}]', block, re.I):
                up_ids.add(ep_id)
            
            block_upper = block.upper()
            if 'COMING SOON' in block_upper or '次回更新' in block_upper or 'に更新予定' in block_upper or 'occ4rb0' in block.lower():
                coming_soon_ids.add(ep_id)
                
        if up_ids:
            logger.info(f"[Jumptoon] Tag Scan: Detected {len(up_ids)} chapters with UP tag: {up_ids}")

    def _parse_page_data(self, html_str, sees_ids, up_ids=None, coming_soon_ids=None):
        """Extracts chapter data from a page's HTML/JSON. Uses shared up_ids/coming_soon_ids sets."""
        if up_ids is None: up_ids = set()
        if coming_soon_ids is None: coming_soon_ids = set()
        
        # Normalize the HTML locally for this page's data
        clean_html = re.sub(r'\\+"', '"', html_str).replace('\\/', '/')
        
        # 1. NEW: Extract precise names from H3 tags using BeautifulSoup
        soup = BeautifulSoup(html_str, 'html.parser')
        h3_mapping = {}
        for li in soup.find_all('li', id=True):
            ep_id = li.get('id', '').strip()
            if not ep_id: continue
            h3 = li.find('h3')
            if h3:
                # 🟢 S-GRADE: Match the [Main] - [Sub] system requested by the user
                full_name = h3.get_text(separator=' - ', strip=True)
                full_name = re.sub(r'(\s*-\s*)+', ' - ', full_name)
                # Split back into notation and title for UI consistency
                parts = full_name.split(' - ', 1)
                h3_mapping[ep_id] = {
                    'notation': parts[0] if len(parts) > 0 else "",
                    'title': parts[1] if len(parts) > 1 else ""
                }

        # 2. Extract Hydrated Data for metadata (id, date, locked state, etc.)
        page_chapters = []
        potential_nodes = re.finditer(r'\{"id"\s*:\s*"([^"]+)"', clean_html)
        
        for match in potential_nodes:
            start = match.start()
            depth = 0
            node_str = ""
            for i, ch in enumerate(clean_html[start:], start=start):
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        node_str = clean_html[start : i+1]
                        break
            
            if not node_str: continue
            
            try:
                node = json.loads(node_str)
                if not isinstance(node, dict): continue
                
                raw_id = node.get('id')
                if not raw_id: continue
                
                # Normalization
                ep_id = str(raw_id)
                if not str(raw_id).isdigit():
                    try: decoded = base64.b64decode(raw_id).decode('utf-8')
                    except: decoded = ""
                    if ':' in decoded: ep_id = str(decoded.split(':')[-1])
                
                # Use BeautifulSoup naming if available to satisfy user request
                h3_name = h3_mapping.get(ep_id)
                if h3_name:
                    notation = h3_name['notation']
                    title = h3_name['title']
                else:
                    notation = (node.get('notation') or '').strip()
                    title = (node.get('title') or '').strip()

                number = node.get('number')
                if not notation and number is None: continue
                
                # COMING SOON FILTER: Unreleased chapters or those scheduled for the future
                # 🟢 Mar 25 Fix: Strict Timestamp Filter
                curr_ts = time.time() * 1000
                pub_ts = node.get('publishStartDatetime')
                
                is_future = False
                if pub_ts is not None:
                    try: is_future = int(pub_ts) > (curr_ts + 60000) # 1min buffer
                    except: pass

                if node.get('offerType') is None or ep_id in coming_soon_ids or is_future:
                    continue
                
                if ep_id in sees_ids: continue
                sees_ids.add(ep_id)
                
                offer_type = node.get('offerType', 'PAID')
                is_purchased = node.get('isPurchased', False)
                # 🟢 S-GRADE: Detect "Wait for free" (待てば無料) status
                is_wait_for_free = (offer_type == "WAIT_FREE") and not is_purchased
                is_locked = not (offer_type in ["FREE", "FIRST_TIME_FREE"] or is_purchased)

                is_new = ep_id in up_ids
                # Diagnostic log for EVERY chapter to find the mismatch
                
                page_chapters.append({
                    'id': ep_id,
                    'title': title,
                    'notation': notation,
                    'number': str(number) if number is not None else None,
                    'is_locked': is_locked,
                    'is_wait_for_free': is_wait_for_free,
                    'is_new': is_new
                })
            except: continue

        return page_chapters

    async def scrape_chapter(self, task, output_dir: str):
        """Phase 2: Extraction with S-Grade Concurrency and Robustness."""
        logger.info(f"[Jumptoon] 🕷️ Processing: {task.title}")
        auth_session = await self._get_authenticated_session()
        
        target_url = task.url if task.url.endswith('/') else f"{task.url}/"
        s_id = str(task.series_id_key).strip('/')
        ep_id = str(task.episode_id).strip('/')
        
        res = await auth_session.get(target_url, timeout=30)
        if res.status_code != 200:
             raise ScraperError(f"Access denied: {res.status_code}")

        await self.session_service.record_session_success("jumptoon")
        
        # Manifest parsing logic
        t = res.text.replace('\\\\', '\\').replace('\\/', '/').replace('\\u0026', '&').replace('\\"', '"')
        p = rf'https?://[a-z0-9.-]+\.jumptoon\.com/[^\"\s<>\\\[\]\(\)\'\;]*{re.escape(s_id)}[^\"\s<>\\\[\]\(\)\'\;]*'
        found_urls = list(dict.fromkeys(re.findall(p, t)))
        
        if not found_urls:
            raise ScraperError("Manifest not found. Account might lack access.")
        
        # 🎯 S-GRADE FILTERING: Restricted to Chapter Content only
        # Domain: contents.jumptoon.com | Path: /episode/{id_or_number}/
        # We try both ID and Number to be resilient against platform quirks.
        keys = [k for k in [str(task.episode_id), str(task.episode_number)] if k and k != "None"]
        filtered_urls = [u for u in found_urls if "contents.jumptoon.com" in u and any(f"/episode/{k}/" in u for k in keys)]
        
        if not filtered_urls:
            # Fallback: if no matches (e.g. platform update), fallback to standard exclusions
            filtered_urls = [u for u in found_urls if not any(x in u.lower() for x in ["preview", "thumb", "width="])]

        from app.services.image.optimizer import ImageOptimizer
        image_data = []
        for url in filtered_urls:
            start_pos = t.find(url)
            window = t[start_pos : start_pos + 600]
            width_match = re.search(r'\"width\":\s*(\d+)', window)
            
            seed = ImageOptimizer.calculate_jumptoon_seed(s_id, task.episode_number)
            image_data.append({
                'file': f"page_{len(image_data)+1:03d}.webp",
                'url': url, 'seed': seed, 'width': int(width_match.group(1)) if width_match else None
            })

        # Concurrent Download with S-Grade Semaphore
        total = len(image_data)
        stats = {"completed": 0}
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Jumptoon", total)
        progress.update(stats["completed"])

        async def download_one(item):
            # 🟢 S-GRADE STAGGER: Small random delay to prevent connection resets on mass-start
            await asyncio.sleep(random.uniform(0.1, 0.5))
            logger.log(5, f"🔗 [Jumptoon] Downloading {item['file']}: {item['url']}")
            async with self._download_semaphore:

                await self._download_image_robust(auth_session, item['url'], item['file'], output_dir, item['seed'], item['width'])
            stats["completed"] += 1
            progress.update(stats["completed"])

        await asyncio.gather(*(download_one(item) for item in image_data))
        progress.finish()
        return output_dir

    async def _download_image_robust(self, session, url, filename, out_dir, seed, requested_width=None):
        from app.services.image.optimizer import ImageOptimizer
        raw_path = os.path.join(out_dir, f"raw_{filename}")
        final_path = os.path.join(out_dir, filename)

        # Retry loop for transient network/curl errors
        max_retries = 5
        for attempt in range(max_retries):
            try:
                # 🟢 S-GRADE: Increased timeout to 180s for massive high-res files
                res = await session.get(url, timeout=180)
                res.raise_for_status()
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 10 * (attempt + 1)
                    logger.warning(f"⚠️ [{filename}] Download attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"❌ [{filename}] All {max_retries} download attempts failed: {e}")
                    raise
        
        with open(raw_path, 'wb') as f: f.write(res.content)
        
        try:
            # Unscrambling happens during download phase for S-Grade efficiency
            img = await asyncio.to_thread(ImageOptimizer.unscramble_jumptoon_v2, raw_path, seed, version="V2", requested_width=requested_width)
            if img:
                img.save(final_path, format="WEBP", quality=100)
                os.remove(raw_path)
            else: os.rename(raw_path, final_path)
        except Exception as e:
            logger.error(f"Unscramble failed: {e}")
            if os.path.exists(raw_path): os.rename(raw_path, final_path)

    async def fast_purchase(self, task) -> bool:
        """
        API-based rapid unlocking for Jumptoon (Wait-for-free).
        Usescaptured Next.js Server Action ID to trigger ticket consumption.
        """
        logger.info(f"[Jumptoon] 🎫 Attempting Wait-for-free API unlock for Ep {task.episode_id}")
        auth_session = await self._get_authenticated_session()
        
        # Captured Action ID: UpdateUserSeriesEpisode
        action_id = "7fec33a9d5a55ec238c028ee220a0f519ee94d5907"
        
        # 🟢 S-GRADE: Binary-Exact Headers
        headers = {
            'next-action': action_id,
            'Content-Type': 'text/plain;charset=UTF-8',
            'Origin': 'https://jumptoon.com',
            'Referer': task.url,
            'x-requested-with': 'XMLHttpRequest'
        }
        
        # 🟢 S-GRADE: Binary-Exact Payload
        payload = [{"input": {
            "seriesEpisodeId": str(task.episode_id),
            "lastViewedAt": True,
            "startViewing": True,
            "lastViewedPageNumber": "1",
            "viewerType": "NORMAL"
        }}]
        compact_payload = json.dumps(payload, separators=(',', ':'))
        
        try:
            # The POST target is the episode's viewer page itself
            res = await auth_session.post(task.url, data=compact_payload, headers=headers, timeout=30)
            
            if res.status_code == 200:
                # Next.js Server Actions return RSC (React Server Components) strings.
                # We check for markers indicating a successful purchase/session state.
                if '"isPurchased":true' in res.text or '"rentalFinishedAt"' in res.text:
                    logger.info(f"✅ [Jumptoon] Successfully unlocked Ep {task.episode_id}")
                    return True
                
                # If it didn't explicitly say purchased, maybe it failed due to no ticket
                if '"NOT_ENOUGH_TICKETS"' in res.text or '"error"' in res.text.lower():
                    logger.warning(f"❌ [Jumptoon] Unlock Ep {task.episode_id} failed: Insufficient tickets or error.")
                    return False
                    
                # Fallback: Treat as success if it looks like a valid RSC response and no errors
                if ':"$@1"' in res.text:
                    logger.info(f"✅ [Jumptoon] Potential success for Ep {task.episode_id} (RSC Response)")
                    return True
            else:
                logger.error(f"❌ [Jumptoon] API unlock failed with status {res.status_code}")
                
        except Exception as e:
            logger.error(f"❌ [Jumptoon] API unlock critical error: {e}")
            
        return False

    async def run_ritual(self, session):
        """S-Grade Ritual: Simulate a user browsing the ranking and checking 'My Toon'."""
        logger.info("[Jumptoon] Running behavioral ritual...")
        # 1. Visit Hero Section (Home)
        await session.get(self.BASE_URL)
        await asyncio.sleep(random.uniform(2, 4))
        # 2. Visit Rankings (Human curiosity)
        await session.get(f"{self.BASE_URL}/ranking/")
        await asyncio.sleep(random.uniform(3, 5))
        # 3. Check Account Status
        await session.get(f"{self.BASE_URL}/mypage")
        logger.info("[Jumptoon] Ritual complete. Session warmed.")
