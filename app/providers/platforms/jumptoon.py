import os
import re
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

logger = logging.getLogger("JumptoonProvider")

# Jumptoon releases at 00:00 JST = 15:00 UTC
JUMPTOON_RELEASE_TIME_UTC = "15:00"


class JumptoonProvider(BaseProvider):
    IDENTIFIER = "jumptoon"
    BASE_URL = "https://jumptoon.com"

    def __init__(self):
        self.session_service = SessionService()
        self.active_account_id = None
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        }
        # S-Grade Backpressure Control: Limit concurrent downloads per instance
        self._download_semaphore = asyncio.Semaphore(10)

    async def _get_authenticated_session(self):
        """Fetches a healthy session from the Vault and initializes an AsyncSession."""
        session_obj = await self.session_service.get_active_session("jumptoon")
        if not session_obj:
            logger.warning("[Jumptoon] No healthy sessions in vault. Using guest session.")
            return curl_requests.AsyncSession(impersonate="chrome")

        self.active_account_id = session_obj["account_id"]
        async_session = curl_requests.AsyncSession(impersonate="chrome")
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
            valid = res.status_code == 200
            if not valid and self.active_account_id:
                await self.session_service.report_session_failure("jumptoon", self.active_account_id, "Session invalidated @ /mypage")
            elif valid:
                await self.session_service.record_session_success("jumptoon")
            return valid
        except Exception as e:
            logger.error(f"Session validation error: {e}")
            return False

    async def get_series_info(self, url: str):
        """Phase 1: Intelligence. Extraction with Telemetry."""
        # Robust Series ID extraction
        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if series_id_match:
            series_id = series_id_match.group(1)
        else:
            series_id = url.split("?")[-1] if "?" in url else url.split("/")[-1]
            if not series_id or series_id == "episodes": 
                series_id = url.split("/")[-2]
        
        logger.info(f"[Jumptoon] 🔍 Intelligence Phase for: {series_id}")
        auth_session = await self._get_authenticated_session()
        
        fetch_url = f"{self.BASE_URL}/series/{series_id}/"
        logger.debug(f"[Jumptoon] Fetching metadata from: {fetch_url}")
        res = await auth_session.get(fetch_url, timeout=30)
        if res.status_code != 200:
            raise ScraperError(f"Failed to fetch series: {res.status_code}")
        
        await self.session_service.record_session_success("jumptoon")
        html_content = res.text
        # Super-Clean Metadata Extraction: Root-Anchored Version
        # Flatten all variations of JSON escaping (1 to 5+ backslashes) into simple quotes
        clean_html = re.sub(r'\\+"', '"', html_content).replace('\\/', '/')
        
        # 1. Total Chapter Count (Scan whole page for specific keys)
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
        
        # Fallback for HTML formatted count if JSON keys missing
        if total_chapters == 0:
            h2_count = re.search(r'<h2>全\s*(?:<!-- -->)?\s*(\d+)\s*(?:<!-- -->)?\s*話</h2>', html_content)
            if h2_count: total_chapters = int(h2_count.group(1))

        # 2. Title Extraction (Root-Anchored Priority)
        # The series title is always the first "name" property at the root of the "series" object.
        # This bypasses nested objects like "publisher":{"name":"集英社"} or "magazine":{"name":"ジャンプTOON"}
        title = series_id
        series_match = re.search(r'"series"\s*:\s*\{', clean_html)
        if series_match:
            # Look at a window immediately following the series start
            window = clean_html[series_match.end() : series_match.end() + 2000]
            # The FIRST "name" match in this window is the actual series title
            name_match = re.search(r'"name"\s*:\s*"([^"]+)"', window)
            if name_match:
                title = name_match.group(1)

        # Robust Unicode Decoding (handles \uXXXX from the stream)
        try:
            if "\\u" in title:
                title = title.encode('utf-8').decode('unicode_escape')
        except: pass
        
        # Final cleanup for title
        title = title.replace('&amp;', '&').strip()
        
        if not title or title == series_id:
            # Fallback to H1 (cleaned) or Page Title
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
        
        # 4. Release Day Extraction (V2 Feature)
        release_day = None
        day_map = {
            "日曜": "Saturday",
            "月曜": "Sunday",
            "火曜": "Monday",
            "水曜": "Tuesday",
            "木曜": "Wednesday",
            "金曜": "Thursday",
            "土曜": "Friday"
        }
        
        # Look for the mgvwcj0 span (e.g., 毎週日曜更新)
        schedule_match = re.search(r'class="mgvwcj0"[^>]*>([^<]+)</span>', html_content)
        if schedule_match:
            text = schedule_match.group(1)
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day = en_day
                    break
        
        # If schedule text failed, try parsing COMING SOON date from time tag
        if not release_day:
            # Look for 0000/00/00 or 00/00 in time tags with class _4s6uv40
            time_matches = re.findall(r'class="[^"]*_4s6uv40"[^>]*>([^<]+)</time>', html_content)
            for tm in time_matches:
                # Basic check: does it have non-zero digits and a slash?
                if "/" in tm and any(c.isdigit() and c != '0' for c in tm):
                    # We found a real date! (e.g., 2026/03/23(月))
                    for jp_day, en_day in day_map.items():
                        if jp_day in tm:
                            release_day = en_day
                            break
                    if release_day: break

        logger.info(f"[Jumptoon] Captured: {title} ({series_id}) - {total_chapters} chapters")
        
        pg_size = 10
        total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1
        
        all_chapters = []
        seen_ids = set()
        
        # CRITICAL: Extract UP/Coming Soon IDs from the initial HTML ONCE.
        # These sets are shared across ALL subsequent _parse_page_data calls
        # so that the is_new flag persists even when chapter JSON comes from
        # a different page than the one containing the <li> UP tags.
        up_ids = set()
        coming_soon_ids = set()
        self._extract_tag_ids(html_content, up_ids, coming_soon_ids)
        
        # Initial page extraction: Check main page first
        all_chapters.extend(self._parse_page_data(html_content, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids))
        
        # If we have less than a full page, or no chapters, we must fetch the first episode page explicitly
        if len(all_chapters) < pg_size or not all_chapters:
            p1_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page=1"
            p1_res = await auth_session.get(p1_url, timeout=30)
            if p1_res.status_code == 200:
                self._extract_tag_ids(p1_res.text, up_ids, coming_soon_ids)
                p1_chaps = self._parse_page_data(p1_res.text, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                all_chapters.extend(p1_chaps)
        
        # Fallback chapter count check
        if total_chapters == 0 and all_chapters:
            total_chapters = len(all_chapters)
            logger.warning(f"[Jumptoon] Fallback count used: {total_chapters}")

        # Subsequent pages
        for page_num in range(2, total_pages + 1):
            p_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={page_num}"
            p_res = await auth_session.get(p_url, timeout=30)
            if p_res.status_code == 200:
                self._extract_tag_ids(p_res.text, up_ids, coming_soon_ids)
                pg_chaps = self._parse_page_data(p_res.text, sees_ids=seen_ids, up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                if not pg_chaps: break 
                all_chapters.extend(pg_chaps)
            else: break

        # Chapter Sorting: Robust fallback
        def extract_sort_key(ch):
            # Try numeric 'number' first
            num = ch.get('number')
            if num and str(num).isdigit():
                return int(num)
            # Try parsing from notation (第1話 -> 1)
            not_match = re.search(r'(\d+)', ch.get('notation', ''))
            if not_match: return int(not_match.group(1))
            return 0

        all_chapters.sort(key=extract_sort_key)
        
        # CRITICAL: Retroactive is_new pass.
        # Chapters may have been parsed from the main page BEFORE the episodes 
        # page (which contains the <li> UP tags) was scanned. This ensures 
        # is_new is correctly set regardless of which page the data came from.
        if up_ids:
            for ch in all_chapters:
                if str(ch['id']) in up_ids:
                    ch['is_new'] = True
        
        release_time = JUMPTOON_RELEASE_TIME_UTC if release_day else None
        return title, total_chapters, all_chapters, image_url, series_id, release_day, release_time

    async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set, skip_pages: list = None):
        """Standard method for background scans to fill in gaps if any."""
        skip_pages = skip_pages or []
        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if not series_id_match: return []
        series_id = series_id_match.group(1)
        
        auth_session = await self._get_authenticated_session()
        extra_chapters = []
        for p in range(1, total_pages + 1):
            if p in skip_pages: continue
            p_res = await auth_session.get(f"{self.BASE_URL}/series/{series_id}/episodes/?page={p}", timeout=30)
            if p_res.status_code == 200:
                pg_chaps = self._parse_page_data(p_res.text, sees_ids=seen_ids)
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
        li_blocks = re.findall(r'<li[^>]*id=["\']?(\d+)["\']?[^>]*>(.*?)</li>', clean_html, re.S | re.I)
        for ep_id, block in li_blocks:
            # Broad but safe detection for 'UP' tag
            if re.search(r'>UP<|UP\s*</|[>{\s]UP[\s<}]', block, re.I):
                tag_id = str(ep_id).strip()
                up_ids.add(tag_id)
            
            block_upper = block.upper()
            if 'COMING SOON' in block_upper or '次回更新' in block_upper or 'に更新予定' in block_upper:
                coming_soon_ids.add(str(ep_id).strip())
                
        if up_ids:
            logger.info(f"[Jumptoon] Tag Scan: Detected {len(up_ids)} chapters with UP tag: {up_ids}")

    def _parse_page_data(self, html_str, sees_ids, up_ids=None, coming_soon_ids=None):
        """Extracts chapter data from a page's HTML/JSON. Uses shared up_ids/coming_soon_ids sets."""
        if up_ids is None: up_ids = set()
        if coming_soon_ids is None: coming_soon_ids = set()
        
        # Normalize the HTML locally for this page's data
        clean_html = re.sub(r'\\+"', '"', html_str).replace('\\/', '/')
        
        # 2. Extract Hydrated Data
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
                
                notation = (node.get('notation') or '').strip()
                title = (node.get('title') or '').strip()
                number = node.get('number')
                if not notation and number is None: continue
                
                raw_id = node.get('id')
                if not raw_id: continue
                
                # Normalization
                ep_id = str(raw_id)
                if not str(raw_id).isdigit():
                    try: decoded = base64.b64decode(raw_id).decode('utf-8')
                    except: decoded = ""
                    if ':' in decoded: ep_id = str(decoded.split(':')[-1])
                
                # COMING SOON FILTER: Unreleased chapters have no offerType or are marked in HTML
                if node.get('offerType') is None or ep_id in coming_soon_ids:
                    continue
                
                if ep_id in sees_ids: continue
                sees_ids.add(ep_id)
                
                offer_type = node.get('offerType', 'PAID')
                is_purchased = node.get('isPurchased', False)
                is_locked = not (offer_type in ["FREE", "FIRST_TIME_FREE"] or is_purchased)

                is_new = ep_id in up_ids
                # Diagnostic log for EVERY chapter to find the mismatch
                logger.info(f"[Jumptoon] Node: ID={ep_id} title={title[:20]}... is_new={is_new} (Matches in {up_ids})")
                
                page_chapters.append({
                    'id': ep_id,
                    'title': title,
                    'notation': notation,
                    'number': str(number) if number is not None else None,
                    'is_locked': is_locked,
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
        ep_id = str(task.episode_number or task.episode_id).strip('/')
        
        res = await auth_session.get(target_url, timeout=30)
        if res.status_code != 200:
             raise ScraperError(f"Access denied: {res.status_code}")

        await self.session_service.record_session_success("jumptoon")
        
        # Manifest parsing logic
        t = res.text.replace('\\\\', '\\').replace('\\/', '/').replace('\\u0026', '&').replace('\\"', '"')
        p = rf'https?://contents\.jumptoon\.com/[^\"\s<>\\\[\]\(\)\'\;]*{re.escape(s_id)}[^\"\s<>\\\[\]\(\)\'\;]*episode[^\"\s<>\\\[\]\(\)\'\;]*{re.escape(ep_id)}[^\"\s<>\\\[\]\(\)\'\;]*'
        found_urls = list(dict.fromkeys(re.findall(p, t)))
        
        if not found_urls:
            raise ScraperError("Manifest not found. Account might lack access.")

        from app.services.image.optimizer import ImageOptimizer
        image_data = []
        for url in found_urls:
            if any(x in url.lower() for x in ["preview", "thumb", "width="]): continue
            start_pos = t.find(url)
            window = t[start_pos : start_pos + 600]
            width_match = re.search(r'\"width\":\s*(\d+)', window)
            
            seed = ImageOptimizer.calculate_jumptoon_seed(s_id, ep_id)
            image_data.append({
                'file': f"page_{len(image_data)+1:03d}.webp",
                'url': url, 'seed': seed, 'width': int(width_match.group(1)) if width_match else None
            })

        # Concurrent Download with S-Grade Semaphore
        total = len(image_data)
        completed = 0
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Jumptoon", total)
        progress.update(completed)

        async def download_one(item):
            nonlocal completed
            async with self._download_semaphore:
                await self._download_image_robust(auth_session, item['url'], item['file'], output_dir, item['seed'], item['width'])
            completed += 1
            progress.update(completed)

        await asyncio.gather(*(download_one(item) for item in image_data))
        progress.finish()
        return output_dir

    async def _download_image_robust(self, session, url, filename, out_dir, seed, requested_width=None):
        from app.services.image.optimizer import ImageOptimizer
        raw_path = os.path.join(out_dir, f"raw_{filename}")
        final_path = os.path.join(out_dir, filename)

        # Retry loop for transient network/curl errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 🟢 Increased timeout to 60s for stability
                res = await session.get(url, timeout=60)
                res.raise_for_status()
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 3 * (attempt + 1)
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
        # Placeholder for future Coin-based API automation
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
