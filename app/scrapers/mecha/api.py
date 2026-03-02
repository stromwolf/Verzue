import os
import json
import re
import math
import time
import binascii
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from PIL import Image
import io
import asyncio
from curl_cffi.requests import AsyncSession

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests

logger = logging.getLogger("MechaApiScraper")

class MechaApiScraper(BaseScraper):
    BASE_URL = "https://mechacomic.jp"

    def __init__(self, browser_service=None):
        # 100% Stateless. No more self.session or self.available_sessions.
        self.browser = browser_service

    def _create_stateless_session(self):
        """Creates a fresh, isolated HTTP session per request."""
        session = crequests.Session(impersonate="chrome120")
        
        # 🟢 EXACT CHROME 120 HEADERS TO MATCH TLS FINGERPRINT
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Upgrade-Insecure-Requests': '1'
        })
        return session

    def _load_available_accounts(self):
        """Reads accounts from disk dynamically at task execution time."""
        mecha_dir = Settings.SECRETS_DIR / "mecha"
        mecha_dir.mkdir(parents=True, exist_ok=True)
        
        cookie_paths = list(mecha_dir.glob("*.json"))
        if Settings.COOKIES_FILE.exists(): cookie_paths.append(Settings.COOKIES_FILE)
        
        accounts = []
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    cookies_data = json.load(f)
                if any(c.get('name') == '_comic_session' for c in cookies_data):
                    accounts.append({'cookies': cookies_data, 'name': path.name})
            except Exception:
                continue
        return accounts

    def _apply_session_cookies(self, session, cookie_list):
        session.cookies.clear()
        
        # Build a flat dictionary of cookies to bypass domain strictness
        cookie_dict = {}
        for c in cookie_list:
            if c.get('name') and c.get('value'):
                cookie_dict[c['name']] = c['value']
                
        session.cookies.update(cookie_dict)
        logger.info(f"   🍪 Force-Injected {len(cookie_dict)} cookies (Flat Dictionary).")

    def get_series_info(self, url: str):
        # Local session just for this lookup
        session = self._create_stateless_session()
        
        base_series_url = url.split('?')[0]
        target_start_url = f"{base_series_url}?page=1"
        
        response = session.get(target_start_url)
        if response.status_code != 200:
            raise ScraperError(f"Series page returned {response.status_code}")

        soup = BeautifulSoup(response.text, 'html.parser')
        
        title = "Unknown Title"
        og_t = soup.find("meta", property="og:title")
        if og_t: title = og_t["content"].split("|")[0].split("-")[0].split("–")[0].strip()
        
        title = re.sub(r'【.*?】', '', title) 
        title = re.sub(r'\s*([-|–]\s*(めちゃコミック|MechaComic)).*', '', title, flags=re.IGNORECASE).strip()
        
        image_url = None
        img_tag = soup.select_one("div.p-bookInfo_jacket img.jacket_image_l")
        if img_tag: image_url = img_tag.get('src')

        max_page = 1
        try:
            count_el = soup.select_one("div.p-search_chapterNo span")
            if count_el:
                max_page = math.ceil(int(re.search(r'(\d+)', count_el.get_text()).group(1)) / 10)
        except: pass

        all_chapters = []
        seen_ids = set()

        def fetch_page(p_num):
            res = session.get(f"{base_series_url}?page={p_num}")
            p_soup = BeautifulSoup(res.text, 'html.parser')
            page_items = []
            
            for item in p_soup.find_all('li', class_='p-chapterList_item'):
                chk = item.find('input', {'name': 'chapter_ids[]'})
                if not chk: continue
                
                cid = chk.get('value')
                # 1. チャプター番号の取得（不要なアイコン・統計データを削除）
                no_elem = item.find('dt', class_='p-chapterList_no')
                num_text = f"Ch.{cid}"
                if no_elem:
                    # 邪魔な「6.8万」や「131」が入っているdivタグを丸ごと削除
                    icons = no_elem.find('div', class_='p-chapterList_icons')
                    if icons:
                        icons.decompose()
                    num_text = no_elem.get_text(strip=True)

                # 2. タイトルの取得（純粋なテキストのみ取得）
                name_elem = item.find('dd', class_='p-chapterList_name')
                title_text = name_elem.get_text(strip=True) if name_elem else ""

                is_locked = True
                btn_area = item.find('div', class_='p-chapterList_btnArea')
                if btn_area and ("無料" in btn_area.get_text() or "読む" in btn_area.get_text()): 
                    is_locked = False

                page_items.append({
                    'id': cid, 'number_text': num_text, 'title_text': title_text,
                    'title': f"{num_text} {title_text}", 'url': f"{self.BASE_URL}/chapters/{cid}",
                    'is_locked': is_locked
                })
            return page_items

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_page, p) for p in range(1, max_page + 1)]
            for f in as_completed(futures):
                for item in f.result():
                    if item['id'] not in seen_ids:
                        seen_ids.add(item['id'])
                        all_chapters.append(item)

        all_chapters.sort(key=lambda x: int(re.search(r'\d+', x['number_text']).group()) if re.search(r'\d+', x['number_text']) else 999)
        return title, len(all_chapters), all_chapters, image_url, base_series_url.split('/')[-1]

    def fast_purchase(self, task):
        """Phase 3 Fast Path: High-Speed API Purchase (Bypasses Selenium)"""
        real_id = task.episode_id
        accounts = self._load_available_accounts()
        session = self._create_stateless_session()

        for acc in accounts:
            logger.info(f"[API Fast-Path] ⚡ Attempting purchase with account: {acc['name']}")
            self._apply_session_cookies(session, acc['cookies'])
            
            target_url = f"{self.BASE_URL}/chapters/{real_id}"
            
            # 1. Fetch the chapter page to grab the CSRF token
            res = session.get(target_url, timeout=10)
            logger.info(f"[API Fast-Path] -> Landed on URL: {res.url}") 
            if res.status_code != 200: continue
            
            # Check if it's already unlocked
            if 'contents_vertical' in res.text or 'viewer' in res.url:
                logger.info(f"[API Fast-Path] ✅ Chapter {real_id} is already unlocked.")
                return True, acc['cookies']

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # 🟢 NEW: Expand selectors to catch "Free Charge" and "Read End" buttons
            buy_btn = soup.select_one(
                "input.js-bt_buy_and_download, input.c-btn-buy, button.btn-purchase, "
                "input.c-btn-free, input.c-btn-read-end, button.js-bt_buy_and_download"
            )
            
            if not buy_btn: 
                logger.debug(f"[API Fast-Path] No buy button found for Ch.{real_id}")
                # 🟢 NEW: Save the HTML so we can see why it missed the button
                with open(f"failed_buy_page_{real_id}.html", "w", encoding="utf-8") as f:
                    f.write(res.text)
                continue
            
            form = buy_btn.find_parent("form")
            if not form: continue

            # Extract the exact action URL (e.g., /chapters/3715030/buy_and_download)
            action_url = urljoin(self.BASE_URL, form.get("action", f"/chapters/{real_id}/buy_and_download"))

            # 3. Extract the exact payload (CSRF token)
            payload = {}
            for hidden in form.find_all("input", type="hidden"):
                if hidden.get("name"):
                    payload[hidden.get("name")] = hidden.get("value", "")
            
            # Add the button's payload just in case Rails checks for the commit
            if buy_btn.get("name"):
                payload[buy_btn.get("name")] = buy_btn.get("value", "")
            else:
                payload["commit"] = buy_btn.get("value", "購入する")

            # 4. Execute the high-speed POST request
            headers = {
                "Referer": target_url,
                "Origin": self.BASE_URL,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            logger.info(f"[API Fast-Path] 📤 Submitting CSRF payload to {action_url}")
            post_res = session.post(action_url, data=payload, headers=headers, allow_redirects=True, timeout=15)
            
            # 5. Verify success
            if post_res.status_code in [200, 302] and ('contents_vertical' in post_res.text or 'viewer' in post_res.url):
                logger.info(f"[API Fast-Path] 🟢 Purchase successful for Ch.{real_id}!")
                # Extract refreshed cookies to save back to disk
                new_cookies = []
                for cookie in session.cookies.jar:
                    new_cookies.append({
                        'name': cookie.name, 'value': cookie.value,
                        'domain': cookie.domain, 'path': cookie.path
                    })
                return True, new_cookies
            else:
                logger.warning(f"[API Fast-Path] ❌ Purchase rejected. Status: {post_res.status_code}")
                
        return False, None

    def scrape_chapter(self, task, output_dir):
        real_id = task.episode_id
        accounts = self._load_available_accounts()
        
        # Local session dedicated entirely to this specific task
        session = self._create_stateless_session()

        selectors = [
            ".p-buyConfirm-currentChapter input.js-bt_buy_and_download",
            ".p-buyConfirm-currentChapter input.c-btn-read-end",
            ".p-buyConfirm-currentChapter input.c-btn-free",
            "input.js-bt_buy_and_download", "button.js-bt_buy_and_download",
            "input.c-btn-read-end", "input.c-btn-free", "input.c-btn-buy",
            "button.btn-purchase", "div.p-bookInfo_btn-read"
        ]

        for acc in accounts:
            logger.info(f"[API] 🔄 Trying Account: {acc['name']} for Chapter {task.chapter_str}")
            self._apply_session_cookies(session, acc['cookies'])
            
            # 1. Try to access the chapter normally
            viewer_url = self._check_chapter_access(session, real_id)
            
            # 🟢 2. AUTO-BUY FALLBACK: If denied, attempt an instant API purchase
            if not viewer_url:
                logger.info(f"   🔒 Access Denied. Attempting on-the-fly Fast Purchase for Ch.{real_id}...")
                success, new_cookies = self.fast_purchase(task)
                if success:
                    # Refresh our session with the newly purchased state
                    self._apply_session_cookies(session, new_cookies)
                    # Try to grab the viewer URL again now that we own it
                    viewer_url = self._check_chapter_access(session, real_id)
                else:
                    raise ScraperError("API Fast Purchase Failed. Selenium fallback is disabled.")

            if viewer_url:
                logger.info(f"   ✅ Access Granted via Account: {acc['name']}")
                return self._execute_extraction(session, viewer_url, real_id, output_dir, task)

        # Guest Fallback
        logger.info(f"[API] 👤 No account access. Attempting Guest Handshake...")
        session.cookies.clear()
        guest_viewer_url = self._check_chapter_access(session, real_id)
        
        if not guest_viewer_url and self.browser:
            new_cookies, guest_viewer_url = self.browser.run_isolated_handshake(task.url, [], selectors)
            if guest_viewer_url and new_cookies: 
                self._apply_session_cookies(session, new_cookies)

        if guest_viewer_url:
            return self._execute_extraction(session, guest_viewer_url, real_id, output_dir, task)

        raise ScraperError("Manifest not found. Chapter is either locked or server rejected handshake.")

    def _check_chapter_access(self, session, real_id):
        try:
            res = session.get(f"{self.BASE_URL}/chapters/{real_id}", timeout=15)
            logger.info(f"   [API] Access Check -> Landed on: {res.url}") 
            if res.status_code == 200 and 'contents_vertical' in res.text:
                match = re.search(r'\"(https?://mechacomic\.jp/viewer\?.*?contents_vertical=.*?)\"', res.text)
                if match: return match.group(1).replace('\\/', '/')

            res = session.get(f"{self.BASE_URL}/chapters/{real_id}/download?commit=read", allow_redirects=True)
            if "contents_vertical" in res.url: return res.url
        except Exception: pass
        return None

    def _execute_extraction(self, session, viewer_url, real_id, output_dir, task):
        logger.info(f"[API] 🏗️  Starting Extraction from: {viewer_url[:60]}...")
        
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        import binascii
        import os
        import io
        import json
        import time
        from urllib.parse import urlparse, parse_qs, urljoin
        from PIL import Image
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.backends import default_backend
        import concurrent.futures

        # 🟢 1. 大量アクセスに耐えられる強牢なセッションの構築 (自動リトライ付き)
        req_session = requests.Session()
        
        # サーバーが混雑・制限（429や500番台）を返した際、自動で間隔を空けて再試行する
        retry_strategy = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=retry_strategy)
        req_session.mount("https://", adapter)
        req_session.mount("http://", adapter)
        
        req_session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })

        for k, v in session.cookies.items():
            req_session.cookies.set(k, v, domain='.mechacomic.jp')
            req_session.cookies.set(k, v, domain='mechacomic.jp')

        qs = parse_qs(urlparse(viewer_url).query)
        contents_vertical_url = qs['contents_vertical'][0]
        directory_url = qs['directory'][0]
        version = qs.get('ver', [''])[0]

        # サーバー側で既読フラグを立てる（少しスリープを挟んで負担を減らす）
        download_url = f"{self.BASE_URL}/chapters/{real_id}/download?commit=read"
        logger.info("   [API] Triggering server-side read session...")
        req_session.get(download_url, allow_redirects=False, timeout=15)
        
        time.sleep(1) # サーバーの同期を待つためのクッション

        manifest_res = req_session.get(contents_vertical_url, timeout=15)
        manifest_res.raise_for_status()
        manifest = manifest_res.json()
        
        cryptokey_path = qs.get('cryptokey', [f"/viewer_cryptokey/chapter/{real_id}"])[0]
        key_url = urljoin(self.BASE_URL, cryptokey_path)
        
        key_res = req_session.get(key_url, timeout=15)
        key_res.raise_for_status()
        key_text = key_res.text.strip()
        logger.info(f"   🔑 Acquired Cryptokey: {key_text[:8]}...")
        key = binascii.unhexlify(key_text)

        img_tasks = []
        for pg in manifest.get('pages', []):
            formats = manifest.get('images', {}).get(pg['image'], [])
            if not formats: continue
            target = next((f for f in formats if f['format'] == 'png'), None) or formats[0]
            img_tasks.append({'src': target['src'], 'pg': pg['pageIndex'], 'filename': f"page_{pg['pageIndex']:03d}.png", 'pg_data': pg})

        # 🟢 2. レート制限で空データが返された時のクラッシュ防止
        if not img_tasks:
            raise Exception("Manifest returned 0 pages. Rate limited by server or chapter is empty.")

        def download_and_decrypt(t):
            time.sleep(0.3) # 🟢 PACING: 0.3s prevents IP blocks during bulk runs
            image_url = f"{directory_url.rstrip('/')}/{t['src']}?ver={version}"
            
            for attempt in range(4):
                try:
                    img_res = req_session.get(image_url, timeout=30)
                    img_res.raise_for_status()
                    encrypted_data = img_res.content
                    
                    iv = encrypted_data[:16]
                    ciphertext = encrypted_data[16:]
                    
                    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
                    decryptor = cipher.decryptor()
                    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
                    
                    unpadder = padding.PKCS7(128).unpadder()
                    plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()
                    
                    with open(os.path.join(output_dir, t['filename']), 'wb') as f:
                        f.write(plaintext)
                    
                    with Image.open(io.BytesIO(plaintext)) as img:
                        return t['pg'], img.size[0], img.size[1]
                except Exception as e:
                    if attempt == 3:
                        raise Exception(f"Failed after 3 retries: {e}")
                    time.sleep(2) # 失敗した場合は2秒待機して再試行

        # 🟢 3. PACED IMAGE DOWNLOADER (Native Requests)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_task = {executor.submit(download_and_decrypt, t): t for t in img_tasks}
            for future in concurrent.futures.as_completed(future_to_task):
                current_task = future_to_task[future]
                try:
                    # Execute and ignore dimensions (we don't need math.json anymore)
                    future.result()
                except Exception as e:
                    logger.error(f"[API] Sync Download failed for page {current_task.get('filename')}: {e}")

        # 🟢 REMOVED: All math.json, scaling, and gap calculations. 
        # The stitcher will now just sequentially stack the decrypted images with 0 gaps!

        return output_dir
