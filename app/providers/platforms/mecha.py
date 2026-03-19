import os
import re
import json
import math
import asyncio
import binascii
import logging
import random
import time
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from curl_cffi.requests import AsyncSession
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError

logger = logging.getLogger("MechaProvider")

class MechaProvider(BaseProvider):
    IDENTIFIER = "mecha"
    BASE_URL = "https://mechacomic.jp"

    def __init__(self):
        self.session_service = SessionService()
        self.active_account_id = None
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
        }
        # S-Grade Backpressure
        self._download_semaphore = asyncio.Semaphore(10)

    async def _get_authenticated_session(self):
        session_obj = await self.session_service.get_active_session("mecha")
        if not session_obj:
            logger.warning("[Mecha] No healthy sessions in vault. Using guest session.")
            return AsyncSession(impersonate="chrome120")

        self.active_account_id = session_obj["account_id"]
        async_session = AsyncSession(impersonate="chrome120")
        async_session.headers.update(self.default_headers)
        
        for c in session_obj["cookies"]:
            name, value = c.get('name'), c.get('value')
            if not name or not value: continue
            raw_domain = c.get('domain', 'mechacomic.jp').lstrip('.')
            async_session.cookies.set(name, value, domain=raw_domain)
            async_session.cookies.set(name, value, domain='.' + raw_domain)
        
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
            page_items.append({
                'id': cid, 'title': f"{num_text} {title_text}",
                'url': chapter_url,
                'is_locked': is_locked, 'is_new': False
            })
        return page_items

    async def get_series_info(self, url: str):
        auth_session = await self._get_authenticated_session()
        base_series_url = url.split('?')[0]
        res = await auth_session.get(f"{base_series_url}?page=1", timeout=30)
        if res.status_code != 200: raise ScraperError(f"Mecha error: {res.status_code}")
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
        count_el = soup.select_one("div.p-search_chapterNo span")
        if count_el:
            m = re.search(r'(\d+)', count_el.get_text())
            if m:
                total_reported = int(m.group(1))
                max_page = math.ceil(total_reported / 10)

        all_chapters = []
        seen_ids = set()
        all_chapters.extend(self._parse_page_chapters(soup, seen_ids))
        
        if max_page > 1:
            res_last = await auth_session.get(f"{base_series_url}?page={max_page}", timeout=15)
            if res_last.status_code == 200:
                all_chapters.extend(self._parse_page_chapters(BeautifulSoup(res_last.text, 'html.parser'), seen_ids))

        all_chapters.sort(key=lambda x: int(x['id']))
        
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

        series_id = base_series_url.split('/')[-1]
        return title, total_reported, all_chapters, image_url, str(series_id), release_day, release_time

    async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set, skip_pages: list = None):
        auth_session = await self._get_authenticated_session()
        base_series_url = url.split('?')[0]
        extra_chapters = []
        skip_pages = skip_pages or []
        
        for p in range(1, total_pages + 1):
            if p in skip_pages: continue
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
            if await self.fast_purchase(task):
                viewer_url = await self._check_chapter_access(auth_session, target_url, real_id)
        
        if not viewer_url: raise ScraperError("Failed to access chapter.")
        await self.session_service.record_session_success("mecha")

        qs = parse_qs(urlparse(viewer_url).query)
        contents_vertical_url = qs['contents_vertical'][0]
        directory_url = qs['directory'][0]
        version = qs.get('ver', [''])[0]
        
        # Manifest
        manifest_res = await auth_session.get(contents_vertical_url, timeout=15)
        manifest = manifest_res.json()
        
        # CryptoKey
        key_url = urljoin(self.BASE_URL, qs.get('cryptokey', [f"/viewer_cryptokey/chapter/{real_id}"])[0])
        key_res = await auth_session.get(key_url, timeout=15)
        key = binascii.unhexlify(key_res.text.strip())

        img_tasks = []
        for pg in manifest.get('pages', []):
            formats = manifest.get('images', {}).get(pg['image'], [])
            target = next((f for f in formats if f['format'] == 'png'), None) or formats[0]
            img_tasks.append({'src': target['src'], 'pg': pg['pageIndex'], 'filename': f"page_{pg['pageIndex']:03d}.png"})

        total = len(img_tasks)
        completed = 0
        from app.core.logger import ProgressBar
        progress = ProgressBar(task.req_id, "Downloading", "Mecha", total)
        progress.update(completed)

        async def fetch_one(t):
            nonlocal completed
            async with self._download_semaphore:
                img_url = f"{directory_url.rstrip('/')}/{t['src']}?ver={version}"
                img_res = await auth_session.get(img_url, timeout=30)
                enc_data = img_res.content
                
                # Decrypt AES-CBC
                iv, ciphertext = enc_data[:16], enc_data[16:]
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                padded = cipher.decryptor().update(ciphertext) + cipher.decryptor().finalize()
                unpadder = padding.PKCS7(128).unpadder()
                plaintext = unpadder.update(padded) + unpadder.finalize()
                
                with open(os.path.join(output_dir, t['filename']), 'wb') as f: f.write(plaintext)
            
            completed += 1
            progress.update(completed)

        await asyncio.gather(*(fetch_one(t) for t in img_tasks))
        progress.finish()
        return output_dir

    async def _check_chapter_access(self, session, target_url, real_id):
        try:
            # Ensure we have a referer to avoid bot detection on some endpoints
            headers = {"Referer": f"{self.BASE_URL}/books"}
            res = await session.get(target_url, timeout=15, headers=headers, allow_redirects=True)
            
            # DETECT SESSION FAILURE: Redirected to login
            if "/login" in str(res.url) or "ログインする" in res.text:
                # If we are using a guest session (no account ID), this is expected if the chapter isn't free
                if self.active_account_id:
                    logger.warning(f"[Mecha] Redirected to login on chapter {real_id}. Reporting session failure for {self.active_account_id}")
                    await self.session_service.report_session_failure("mecha", self.active_account_id, "Redirected to login during chapter access")
                return None

            # S-GRADE: Follow redirects to the viewer
            # If the final URL is already a viewer URL, parse it
            if 'contents_vertical' in str(res.url):
                return str(res.url)

            # If we get a login page (redirected but 200), look for viewer info anyway
            # Mecha sometimes embeds the 'Free' viewer URL in a different way
            body = res.text
            if 'contents_vertical' in body:
                match = re.search(r'\"(https?://mechacomic\.jp/viewer\?.*?contents_vertical=.*?)\"', body)
                if not match:
                    match = re.search(r'(https?://mechacomic\.jp/viewer\?[^"]*contents_vertical=[^"]*)', body.replace('\\/', '/'))
                if match: return match.group(1).replace('\\/', '/')
            
            # Fallback: Sometimes the URL is in a different format or script
            match = re.search(r'viewer_url\s*=\s*[\"\'](https?://.*?contents_vertical=.*?)[\"\']', body)
            if match: return match.group(1).replace('\\/', '/')
            
        except Exception as e:
            logger.debug(f"[Mecha] Chapter access check failed for {real_id}: {e}")
        return None

    async def fast_purchase(self, task) -> bool:
        real_id = task.episode_id
        auth_session = await self._get_authenticated_session()
        res = await auth_session.get(f"{self.BASE_URL}/chapters/{real_id}", timeout=15)
        if res.status_code != 200: return False
        if 'contents_vertical' in res.text: return True

        soup = BeautifulSoup(res.text, 'html.parser')
        buy_btn = soup.select_one("input.js-bt_buy_and_download, input.c-btn-buy, input.c-btn-free, input.c-btn-read-end")
        if not buy_btn: return False
        
        form = buy_btn.find_parent("form")
        if not form: return False
        action = urljoin(self.BASE_URL, form.get("action", f"/chapters/{real_id}/buy_and_download"))
        payload = {h.get("name"): h.get("value", "") for h in form.find_all("input", type="hidden") if h.get("name")}
        payload[buy_btn.get("name", "commit")] = buy_btn.get("value", "購入する")
        
        headers = {"Referer": f"{self.BASE_URL}/chapters/{real_id}", "Origin": self.BASE_URL}
        post_res = await auth_session.post(action, data=payload, headers=headers, timeout=15)
        return post_res.status_code in [200, 302] and ('contents_vertical' in post_res.text or 'viewer' in post_res.url)

    async def run_ritual(self, session):
        logger.info("[Mecha] Running behavioral ritual...")
        await session.get(self.BASE_URL)
        await asyncio.sleep(max(1, random.gauss(3, 1)))
        await session.get(f"{self.BASE_URL}/free")
        await asyncio.sleep(max(1, random.gauss(2, 0.5)))
        await session.get(f"{self.BASE_URL}/account")
