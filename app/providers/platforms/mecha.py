import os
import re
import json
import math
import asyncio
import binascii
import logging
import random
import time
import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from app.providers.curl_compat import AsyncSession, RequestsError, ProxyError
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.services.login_service import LoginService
from app.core.exceptions import ScraperError, MechaException
from config.settings import Settings

logger = logging.getLogger("MechaProvider")

class MechaProvider(BaseProvider):
    IDENTIFIER = "mecha"
    BASE_URL = "https://mechacomic.jp"
    SERIES_PATH = "/books/"

    def __init__(self):
        self.session_service = SessionService()
        self.login_service = LoginService()
        self.active_account_id = None
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
        }
        # S-Grade Backpressure: Rate-limited globally via app.services.rate_limiter.

    async def _get_authenticated_session(self):
        session_obj = await self.session_service.get_active_session("mecha")
        if not session_obj:
            # S+ GRADE: Automated Login Fallback
            async with self.session_service.get_refresh_lock("mecha"):
                # Double-check after lock
                session_obj = await self.session_service.get_active_session("mecha")
                
                if not session_obj:
                    logger.info("🔄 [Mecha] No healthy sessions in vault. Triggering automated login fallback...")
                    login_success = await self.login_service.auto_login("mecha")
                    
                    if login_success:
                        session_obj = await self.session_service.get_active_session("mecha")
            
            if not session_obj:
                logger.warning("[Mecha] No healthy sessions in vault and fallback failed. Using guest session.")
                return AsyncSession(impersonate="chrome120", proxy=Settings.get_proxy())

        self.active_account_id = session_obj["account_id"]
        async_session = AsyncSession(impersonate="chrome120", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)
        
        # 🟢 S-GRADE: Prune tracking/bloat cookies to prevent 400 errors
        # Only load essential auth cookies, skip tracking/analytics/per-book bloat
        ESSENTIAL_COOKIE_PREFIXES = ('_session', 'mechacomic', 'remember', '_csrf', '__Host', '__Secure')
        
        pruned_count = 0
        for c in session_obj["cookies"]:
            name, value = c.get('name'), c.get('value')
            if not name or not value: continue
            
            if not name.startswith(ESSENTIAL_COOKIE_PREFIXES):
                pruned_count += 1
                continue

            raw_domain = c.get('domain', 'mechacomic.jp').lstrip('.')
            async_session.cookies.set(name, value, domain=raw_domain)
            async_session.cookies.set(name, value, domain='.' + raw_domain)
        
        if pruned_count > 0:
            logger.debug(f"[Mecha] Pruned {pruned_count} tracking/bloat cookies for session {self.active_account_id}")
        
        return async_session

    async def is_session_valid(self, session) -> bool:
        try:
            # Mecha Comic uses /account or profile-related endpoints
            # We must follow redirects to see if we end up at /login
            res = await session.get(f"{self.BASE_URL}/account", timeout=15, allow_redirects=True)
            
            # If not logged in, it usually redirects to /login
            valid = res.status_code == 200 and "/login" not in str(res.url)
            
            if not valid and self.active_account_id:
                logger.warning(f"[Mecha] Session invalid for {self.active_account_id}. URL: {res.url}")
                await self.session_service.report_session_failure("mecha", self.active_account_id, "Session validation failed / Redirected to login")
            elif valid:
                await self.session_service.record_session_success("mecha")
            return valid
        except Exception as e:
            logger.debug(f"[Mecha] Session validity check exception: {e}")
            return False

    def _parse_page_chapters(self, soup, seen_ids):
        page_items = []
        for item in soup.find_all('li', class_='p-chapterList_item'):
            chk = item.find('input', {'name': 'chapter_ids[]'})
            if not chk: continue
            cid = chk.get('value')
            if cid in seen_ids: continue
            
            no_elem = item.find('dt', class_='p-chapterList_no')
            num_text = f"Ch.{cid}"
            if no_elem:
                icons = no_elem.find('div', class_='p-chapterList_icons')
                if icons: icons.decompose()
                num_text = no_elem.get_text(strip=True)

            name_elem = item.find('dd', class_='p-chapterList_name')
            title_text = name_elem.get_text(strip=True) if name_elem else ""

            is_locked = True
            btn_area = item.find('div', class_='p-chapterList_btnArea')
            chapter_url = f"{self.BASE_URL}/chapters/{cid}"
            
            if btn_area:
                btn_text = btn_area.get_text()
                if "無料" in btn_text or "読む" in btn_text:
                    is_locked = False
                
                # S-GRADE: Extract dynamic URL (e.g., guest download link)
                btn = btn_area.find('a')
                if btn and btn.get('href'):
                    chapter_url = urljoin(self.BASE_URL, btn.get('href'))

            seen_ids.add(cid)
            # 🟢 S-GRADE: Improved Title Formatting (Mar 27 Request)
            # Full Title: "001話 - プロローグ"
            full_title = f"{num_text} - {title_text}" if title_text else num_text
            
            page_items.append({
                'id': cid, 
                'title': full_title, # Combined for legacy and Drive
                'notation': num_text, # "001話"
                'title_only': title_text, # "プロローグ"
                'url': chapter_url,
                'is_locked': is_locked, 
                'is_new': False
            })
        return page_items

    async def get_series_info(self, url: str, fast: bool = False):
        auth_session = await self._get_authenticated_session()
        base_series_url = url.split('?')[0]
        logger.info(f"[Mecha] 🔍 Series Info Requested: {base_series_url} (mode={'fast' if fast else 'full'})")
        
        from app.services.rate_limiter import PlatformRateLimiter
        try:
            p1_url = f"{base_series_url}?page=1"
            logger.debug(f"[Mecha] Fetching Page 1: {p1_url}")
            async with PlatformRateLimiter.get("mecha").acquire():
                res = await auth_session.get(p1_url, timeout=30)
            if res.status_code != 200: 
                logger.error(f"[Mecha] ❌ HTTP {res.status_code} for URL: {p1_url} | Response preview: {res.text[:300]}")
                raise ScraperError(f"Mecha error: {res.status_code}", code="SC_002")
        except RequestsError as e:
            logger.error(f"[Mecha] Request Error (Potential Proxy): {e}")
            raise ScraperError("Scraping Proxy Denied Access (403). Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
        except Exception as e:
            if "ScraperError" in type(e).__name__: raise
            raise ScraperError(f"Request failed: {e}")
            
        await self.session_service.record_session_success("mecha")
        
        soup = BeautifulSoup(res.text, 'html.parser')
        title = "Unknown"
        og_t = soup.find("meta", property="og:title")
        if og_t: title = og_t["content"].split("|")[0].split("-")[0].strip()
        title = re.sub(r'【.*?】', '', title)
        
        image_url = None
        img_tag = soup.select_one("div.p-bookInfo_jacket img.jacket_image_l")
        if img_tag: image_url = img_tag.get('src', '').split('?')[0]

        total_reported = 0
        max_page = 1
        count_el = soup.select_one("div.p-search_chapterNo")
        if count_el:
            # Targets "／24話へ" text -> extracts 24
            m = re.search(r'／(\d+)話へ', count_el.get_text())
            if m:
                total_reported = int(m.group(1))
                max_page = math.ceil(total_reported / 10)
        
        logger.info(f"[Mecha] Metadata Parsed: Title='{title}', Total='{total_reported}', MaxPage='{max_page}', Image='{'Yes' if image_url else 'No'}'")

        all_chapters = []
        seen_ids = set()
        all_chapters.extend(self._parse_page_chapters(soup, seen_ids))
        
        if fast:
            logger.info(f"[Mecha] Fast Fetch (Page 1 Only): {title}")
            # Even in fast mode, we return what was on page 1
            # But we skip the 'last page' sync below
        elif max_page > 1:
            last_pg_url = f"{base_series_url}?page={max_page}"
            logger.debug(f"[Mecha] Fetching Last Page: {last_pg_url}")
            async with PlatformRateLimiter.get("mecha").acquire():
                res_last = await auth_session.get(last_pg_url, timeout=15)
            if res_last.status_code == 200:
                all_chapters.extend(self._parse_page_chapters(BeautifulSoup(res_last.text, 'html.parser'), seen_ids))
            else:
                logger.warning(f"[Mecha] Last Page returned HTTP {res_last.status_code}")

        all_chapters.sort(key=lambda x: int(x['id']))
        
        # ✅ Mecha has no UP/NEW badge — latest chapter = last in sorted list
        if all_chapters:
            all_chapters[-1]['is_new'] = True

        # 4. Release Day Extraction (V2 Feature)
        release_day = None
        release_time = None
        
        # Mecha updates at 00:00 JST which is 15:00 UTC (previous day)
        day_map = {
            "日曜": "Saturday",
            "月曜": "Sunday",
            "火曜": "Monday",
            "水曜": "Tuesday",
            "木曜": "Wednesday",
            "金曜": "Thursday",
            "土曜": "Friday"
        }
        
        # Look for the c-callout text (e.g., 毎週金曜0時更新！)
        schedule_match = re.search(r'class="c-callout[^"]*"[^>]*><p[^>]*>([^<]+)</p>', res.text)
        if schedule_match:
            text = schedule_match.group(1)
            for jp_day, en_day in day_map.items():
                if jp_day in text:
                    release_day = en_day
                    release_time = "15:00" # Midnight JST = 15:00 UTC
                    break

        # 5. Status Detection (Simplified)
        status_label = None
        if "完結" in res.text:
            if soup.find("span", class_="c-tag-completed") or soup.select_one("div.p-bookInfo_status"):
                 status_label = "Completed"

        genre_label = None

        series_id = base_series_url.split('/')[-1]
        
        # 🟢 S-GRADE: Ensure series is favorited for alerts (Mar 27 Request Integration)
        try:
            is_fav = soup.select_one("input.js-bt_favorite, button.js-bt_favorite")
            if is_fav and "登録済み" not in is_fav.get_text() and "解除" not in is_fav.get("value", ""):
                 logger.info(f"❤️ [Mecha] Automatically registering {title} for alerts...")
                 await self.toggle_alert(series_id, enable=True, soup=soup)
        except Exception as e:
            logger.debug(f"[Mecha] Failed to auto-favorite {title}: {e}")

        self._last_soup = soup
        return title, total_reported, all_chapters, image_url, str(series_id), release_day, release_time, status_label, genre_label

    async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set, skip_pages: list = None):
        auth_session = await self._get_authenticated_session()
        base_series_url = url.split('?')[0]
        extra_chapters = []
        skip_pages = skip_pages or []
        
        from app.services.rate_limiter import PlatformRateLimiter
        for p in range(1, total_pages + 1):
            if p in skip_pages: continue
            async with PlatformRateLimiter.get("mecha").acquire():
                res = await auth_session.get(f"{base_series_url}?page={p}", timeout=15)
            if res.status_code == 200:
                extra_chapters.extend(self._parse_page_chapters(BeautifulSoup(res.text, 'html.parser'), seen_ids))
        return extra_chapters

    async def scrape_chapter(self, task, output_dir: str):
        real_id = task.episode_id
        auth_session = await self._get_authenticated_session()
        
        # S-GRADE: Use the dynamic URL provided in the task (extracted during scan)
        target_url = task.url
        viewer_url = await self._check_chapter_access(auth_session, target_url, real_id)
        if not viewer_url:
            viewer_url = await self.fast_purchase(task)
        
        if not viewer_url: 
            raise ScraperError("Failed to access chapter. Check session or purchase status.", code="AC_001")
        await self.session_service.record_session_success("mecha")

        qs = parse_qs(urlparse(viewer_url).query)
        contents_vertical_url = qs['contents_vertical'][0]
        directory_url = qs['directory'][0]
        version = qs.get('ver', [''])[0]
        
        # Manifest
        from app.services.rate_limiter import PlatformRateLimiter
        try:
            async with PlatformRateLimiter.get("mecha").acquire():
                manifest_res = await auth_session.get(contents_vertical_url, timeout=15)
            manifest = manifest_res.json()
        except ProxyError as e:
            raise ScraperError("Proxy Access Denied (403) during manifest fetch.", code="PX_403")
        except Exception as e:
            raise ScraperError(f"Failed to fetch manifest: {e}")
        
        # CryptoKey
        key_url = urljoin(self.BASE_URL, qs.get('cryptokey', [f"/viewer_cryptokey/chapter/{real_id}"])[0])
        async with PlatformRateLimiter.get("mecha").acquire():
            key_res = await auth_session.get(key_url, timeout=15)
        key = binascii.unhexlify(key_res.text.strip())

        img_tasks = []
        for pg in manifest.get('pages', []):
            formats = manifest.get('images', {}).get(pg['image'], [])
            target = next((f for f in formats if f['format'] == 'png'), None) or formats[0]
            img_tasks.append({'src': target['src'], 'pg': pg['pageIndex'], 'filename': f"page_{pg['pageIndex']:03d}.png"})

        total = len(img_tasks)
        stats = {"completed": 0}
        from app.core.progress import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Mecha", total, episode_id=task.episode_id)
        progress.update(stats["completed"])

        # 🧤 S-Grade: Fixed Concurrency (10)
        async def fetch_one(t):
            from app.services.rate_limiter import PlatformRateLimiter
            async with PlatformRateLimiter.get("mecha").acquire(download=True):
                img_url = f"{directory_url.rstrip('/')}/{t['src']}?ver={version}"
                try:
                    t_get = auth_session.get(img_url, timeout=30)
                    img_res = await t_get
                    enc_data = img_res.content
                except ProxyError:
                    raise ScraperError("Proxy Access Denied (403) during image download.", code="PX_403")
                except Exception as e:
                    raise ScraperError(f"Image download failed: {e}")
                
                # Decrypt AES-CBC
                iv, ciphertext = enc_data[:16], enc_data[16:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                padded = cipher.decryptor().update(ciphertext) + cipher.decryptor().finalize()
                unpadder = padding.PKCS7(128).unpadder()
                plaintext = unpadder.update(padded) + unpadder.finalize()
                
                with open(os.path.join(output_dir, t['filename']), 'wb') as f: f.write(plaintext)
            
            stats["completed"] += 1
            progress.update(stats["completed"])

        await asyncio.gather(*(fetch_one(t) for t in img_tasks))
        progress.finish()
        return output_dir

    async def _check_chapter_access(self, session, target_url, real_id):
        try:
            # Ensure we have a referer to avoid bot detection on some endpoints
            headers = {"Referer": f"{self.BASE_URL}/books"}
            res = await session.get(target_url, timeout=15, headers=headers, allow_redirects=True)
            
            logger.debug(f"[Mecha] Chapter access check for {real_id}: Status {res.status_code} | URL {res.url}")
            
            # DETECT SESSION FAILURE: Redirected to login
            if "/login" in str(res.url) or "ログインする" in res.text:
                logger.info(f"[Mecha] Login required/Redirected for {real_id}. Text preview: {res.text[:100]}...")
                # If we are using a guest session (no account ID), this is expected if the chapter isn't free
                if self.active_account_id:
                    logger.warning(f"[Mecha] Redirected to login on chapter {real_id}. Reporting session failure for {self.active_account_id}")
                    await self.session_service.report_session_failure("mecha", self.active_account_id, "Redirected to login during chapter access")
                return None

            # S-GRADE: Follow redirects to the viewer
            # If the final URL is already a viewer URL, parse it
            if 'contents_vertical' in str(res.url):
                return str(res.url)

            # Assign response body for subsequent checks
            body = res.text

            # 🟢 S-GRADE FALLBACK: Try the /download endpoint directly if no viewer URL found
            if 'contents_vertical' not in body:
                download_url = f"{self.BASE_URL}/chapters/{real_id}/download"
                logger.info(f"[Mecha] Viewer URL not in page, trying fallback: {download_url}")
                dl_res = await session.get(download_url, timeout=10, allow_redirects=True)
                if 'contents_vertical' in dl_res.text or 'viewer' in str(dl_res.url):
                    body = dl_res.text
                    target_url = str(dl_res.url)
                    if 'contents_vertical' in target_url: return target_url

            # Final Regex Search in body
            if 'contents_vertical' in body:
                # Try multiple regex patterns for different JS/HTML layouts
                patterns = [
                    r'\"(https?://mechacomic\.jp/viewer\?.*?contents_vertical=.*?)\"',
                    r'\'(https?://mechacomic\.jp/viewer\?.*?contents_vertical=.*?)\'',
                    r'viewer_url\s*=\s*[\"\'](https?://.*?contents_vertical=.*?)[\"\']',
                    r'(https?://mechacomic\.jp/viewer\?[^"\'\s]*contents_vertical=[^"\'\s]*)'
                ]
                for p in patterns:
                    match = re.search(p, body.replace('\\/', '/'))
                    if match: return match.group(1)
            
            # 🟢 S-GRADE DEBUG: Save HTML to file for investigation if still failing
            try:
                from config.settings import Settings
                debug_path = Settings.LOG_DIR / f"mecha_fail_{real_id}.html"
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(body)
                logger.debug(f"[Mecha] Saved failed response HTML to {debug_path}")
            except: pass

            logger.warning(f"[Mecha] Chapter access check for {real_id} finished without finding viewer_url. Status: {res.status_code}")
            
        except Exception as e:
            logger.debug(f"[Mecha] Chapter access check failed for {real_id}: {e}")
        return None

    async def fast_purchase(self, task) -> str | None:
        """
        S-GRADE: Handles both POST purchase forms and GET 'Read' buttons.
        Returns: viewer_url (str) if successful, else None.
        """
        real_id = task.episode_id
        auth_session = await self._get_authenticated_session()
        res = await auth_session.get(f"{self.BASE_URL}/chapters/{real_id}", timeout=15)
        
        if res.status_code != 200: 
            logger.debug(f"[Mecha] Fast purchase fetch for {real_id} returned {res.status_code}")
            return None
            
        body = res.text
        viewer_url = await self._check_chapter_access(auth_session, f"{self.BASE_URL}/chapters/{real_id}", real_id)
        if viewer_url: return viewer_url

        soup = BeautifulSoup(body, 'html.parser')
        
        # Scope search to the current chapter container to avoid 'Next Chapter' link mix-ups
        current_container = soup.select_one(".p-buyConfirm-currentChapter")
        if not current_container: current_container = soup # Fallback to whole page
        
        # 1. Look for Anchor-based 'Read' buttons (often used for free/owned chapters)
        read_link = current_container.select_one("a.c-btn-read-end, a.c-btn-free, a.js-bt_read")
        if read_link and read_link.get("href"):
            target_url = urljoin(self.BASE_URL, read_link["href"])
            logger.info(f"[Mecha] Following GET link for {real_id}: {target_url}")
            follow_res = await auth_session.get(target_url, timeout=15, allow_redirects=True)
            if follow_res.status_code in [200, 302]:
                if 'contents_vertical' in str(follow_res.url): return str(follow_res.url)
                # Try to extract from body
                return await self._check_chapter_access(auth_session, target_url, real_id)

        # 2. Look for Form-based buttons
        buy_btn = current_container.select_one("input.js-bt_buy_and_download, input.c-btn-buy, input.c-btn-free, input.c-btn-read-end, button.c-btn-read-end")
        if not buy_btn:
            logger.debug(f"[Mecha] Fast purchase for {real_id}: No buy/read button found.")
            return None
        
        form = buy_btn.find_parent("form")
        if not form: return None
        
        method = form.get("method", "post").lower()
        action = urljoin(self.BASE_URL, form.get("action", f"/chapters/{real_id}/download"))
        
        payload = {h.get("name"): h.get("value", "") for h in form.find_all("input", type="hidden") if h.get("name")}
        if buy_btn.get("name"):
            payload[buy_btn["name"]] = buy_btn.get("value", "")
        
        headers = {"Referer": f"{self.BASE_URL}/chapters/{real_id}", "Origin": self.BASE_URL}
        
        if method == "get":
            logger.info(f"[Mecha] Attempting GET purchase/read for {real_id} via {action}...")
            post_res = await auth_session.get(action, params=payload, headers=headers, timeout=15)
        else:
            logger.info(f"[Mecha] Attempting POST purchase/read for {real_id} via {action}...")
            post_res = await auth_session.post(action, data=payload, headers=headers, timeout=15)
        
        success_url = None
        if post_res.status_code in [200, 302]:
            if 'contents_vertical' in str(post_res.url): 
                success_url = str(post_res.url)
            else:
                success_url = await self._check_chapter_access(auth_session, action, real_id)

        logger.info(f"[Mecha] Fast purchase for {real_id}: {'Success' if success_url else 'Failed'} (Status {post_res.status_code})")
        return success_url

    async def run_ritual(self, session):
        logger.info("[Mecha] Running behavioral ritual...")
        await session.get(self.BASE_URL)
        await asyncio.sleep(max(1, random.gauss(3, 1)))
        await session.get(f"{self.BASE_URL}/free")
        await asyncio.sleep(max(1, random.gauss(2, 0.5)))
        await session.get(f"{self.BASE_URL}/account")

    async def get_new_series_list(self) -> list[dict]:
        """Scrapes the 'Exclusive New' page for MechaComic (JP)."""
        try:
            auth_session = await self._get_authenticated_session()
            
            # Try multiple potential "New" endpoints to be robust against 404s
            endpoints = ["/books/exclusive", "/books/exclusive_top", "/books/new"]
            res = None
            
            for ep in endpoints:
                target = f"{self.BASE_URL}{ep}"
                if ep == "/books/exclusive": target += "?sort=new_book"
                
                logger.debug(f"[Mecha] Trying discovery endpoint: {target}")
                try:
                    res = await auth_session.get(target, timeout=15)
                    if res.status_code == 200: 
                        logger.info(f"[Mecha] Discovery successful via {ep}")
                        break
                except Exception:
                    continue
            
            if not res or res.status_code != 200:
                logger.error(f"[Mecha] All discovery endpoints failed. (Last Code: {res.status_code if res else 'None'})")
                return []
                
            soup = BeautifulSoup(res.text, 'html.parser')
            new_series = []
            
            # Selector for MechaComic book list items
            items = soup.select('.p-bookList_item')
            for item in items:
                # Title element is in .p-book_title a
                title_elem = item.select_one('.p-book_title a')
                if not title_elem: continue
                
                title = title_elem.get_text(strip=True)
                href = title_elem.get('href', '')
                
                match = re.search(r'/books/(\d+)', href)
                if not match: continue
                
                sid = match.group(1)
                
                # Image is in .p-book_jacket img
                img_elem = item.select_one('.p-book_jacket img.jacket_image_l')
                poster = img_elem.get('src') if img_elem else None
                
                new_series.append({
                    "series_id": sid,
                    "title": title,
                    "poster_url": poster,
                    "url": urljoin(self.BASE_URL, href)
                })
                
            logger.info(f"[Mecha] Found {len(new_series)} potential new series.")
            return new_series
        except Exception as e:
            logger.error(f"[Mecha] Error fetching new series: {e}")
            return []

    async def get_alerts_list(self) -> list[dict]:
        """
        🟢 ALERTS INTEGRATION (Apr 2 Request)
        Fetches the MechaComic alerts page and returns a list of updated series.
        URL: https://mechacomic.jp/alerts?content=chapter&type=book
        """
        try:
            auth_session = await self._get_authenticated_session()
            url = f"{self.BASE_URL}/alerts?content=chapter&type=book"
            res = await auth_session.get(url, timeout=20)
            
            if res.status_code != 200:
                logger.error(f"❌ [Mecha] Failed to fetch alerts: {res.status_code}")
                return []

            soup = BeautifulSoup(res.text, 'html.parser')
            updated_series = []
            
            # Use JST for date parsing (Mecha updates at 00:00 JST)
            JST = ZoneInfo("Asia/Tokyo")
            now_jst = datetime.datetime.now(JST)

            items = soup.select('.p-bookList_item')
            for item in items:
                title_elem = item.select_one('.p-book_title a')
                if not title_elem: continue
                
                title = title_elem.get_text(strip=True)
                href = title_elem.get('href', '')
                book_id = href.split('/')[-1]        # series/book ID
                chapter_id = title_elem.get('data-id')  # actual chapter ID
                
                arrival_day_elem = item.select_one('.p-book_arrivalDay')
                day_text = arrival_day_elem.get_text(strip=True) if arrival_day_elem else None  # e.g., "4/24"
                
                # ── Derive release_day from arrival_day date string ──
                release_day = None
                if day_text:
                    try:
                        month, day = map(int, day_text.split('/'))
                        year = now_jst.year
                        candidate = datetime.date(year, month, day)
                        # If date is in future (e.g., Dec 31 vs Jan 1), it belongs to previous year
                        if candidate > now_jst.date():
                            candidate = datetime.date(year - 1, month, day)
                        release_day = candidate.strftime("%A")  # e.g., "Friday"
                    except Exception:
                        pass

                update_info = item.select_one('.p-book_update')
                notation = update_info.get_text(strip=True).replace("続話：", "") if update_info else None
                
                updated_series.append({
                    "series_id": book_id,
                    "chapter_id": chapter_id,
                    "title": title,
                    "url": urljoin(self.BASE_URL, href),
                    "notation": notation,
                    "arrival_day": day_text,
                    "release_day": release_day
                })
                
            return updated_series
        except Exception as e:
            logger.error(f"❌ [Mecha] Alerts Fetch Error: {e}")
            return []

    async def toggle_alert(self, series_id: str, enable: bool = True, soup: BeautifulSoup = None):
        try:
            auth_session = await self._get_authenticated_session()

            # ALWAYS re-fetch fresh — stale soup = stale CSRF = 403
            res = await auth_session.get(
                f"{self.BASE_URL}/books/{series_id}", timeout=15
            )
            soup = BeautifulSoup(res.text, 'html.parser')

            # CSRF from meta tag
            token = None
            token_elem = soup.find("meta", {"name": "csrf-token"})
            if token_elem:
                token = token_elem.get("content")
            if not token:
                token_elem = soup.find("input", {"name": "authenticity_token"})
                if token_elem:
                    token = token_elem.get("value")

            if not token:
                logger.error(f"[Mecha] toggle_alert: No CSRF token found for {series_id}")
                return False

            endpoint = "switch_on_book" if enable else "switch_off_book"
            url = f"{self.BASE_URL}/alerts/{endpoint}?book_id={series_id}"

            headers = {
                "Referer": f"{self.BASE_URL}/books/{series_id}",
                "Origin": self.BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRF-Token": token,
                # Rails UJS exact Accept header
                "Accept": "text/javascript, application/javascript, application/ecmascript, application/x-ecmascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }

            res = await auth_session.post(url, headers=headers, timeout=15)
            success = res.status_code in [200, 302]

            logger.info(
                f"[Mecha] Alert toggle (enable={enable}) for {series_id}: "
                f"HTTP {res.status_code} → {'✅' if success else '❌'}"
            )

            if not success:
                logger.error(
                    f"[Mecha] toggle_alert FAILED DETAIL:\n"
                    f"  URL      : {url}\n"
                    f"  Status   : {res.status_code}\n"
                    f"  CSRF     : {'present' if token else 'MISSING'}\n"
                    f"  Session  : {self.active_account_id}\n"
                    f"  Res Headers: {dict(res.headers)}\n"
                    f"  Body     : {res.text[:500]}"
                )
                # Also dump HTML to file for deep inspection
                try:
                    from config.settings import Settings
                    import time
                    dump_path = Settings.LOG_DIR / f"mecha_toggle_fail_{series_id}_{int(time.time())}.html"
                    with open(dump_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                    logger.error(f"[Mecha] Full 403 response dumped → {dump_path}")
                except Exception as dump_err:
                    logger.warning(f"[Mecha] Failed to dump 403 body: {dump_err}")

            return success

        except Exception as e:
            logger.error(f"[Mecha] toggle_alert error: {e}")
            return False
