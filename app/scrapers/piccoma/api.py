import re
import json
import logging
import math
import time
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from config.settings import Settings
 
try:
    from PIL import Image
except ImportError:
    raise ImportError("Pillow is required for Piccoma unscrambling. Run: pip install Pillow")

try:
    from pycasso import Canvas
except ImportError:
    Canvas = None # Handle in robust method

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests

from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

logger = logging.getLogger("PiccomaApi")

# --- DD TRANSFORMATION (Match pyccoma-0.7.2) ---
def dd(input_string):
    result_bytearray = bytearray()
    for index, byte in enumerate(bytes(input_string, 'utf-8')):
        if index < 3:
            byte = byte + (1 - 2 * (byte % 2))
        elif 2 < index < 6 or index == 8:
            pass
        elif index < 10:
            byte = byte + (1 - 2 * (byte % 2))
        elif 12 < index < 15 or index == 16:
            byte = byte + (1 - 2 * (byte % 2))
        elif index == len(input_string[:-1]) or index == len(input_string[:-2]):
            byte = byte + (1 - 2 * (byte % 2))
        else:
            pass
        result_bytearray.append(byte)
    return str(result_bytearray, 'utf-8')




class PiccomaApiScraper(BaseScraper):
    def __init__(self):
        self.session = crequests.Session(impersonate="chrome120")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        })
        self.base_url = "https://piccoma.com"
        self.region = "jp" # Default
        self.master_seed = None
        self._load_cookies()

    def _load_cookies(self):
        """Loads and deduplicates cookies from piccoma secrets directory."""
        jt_dir = Settings.SECRETS_DIR / "piccoma"
        if not jt_dir.exists(): return
        
        cookie_paths = sorted(list(jt_dir.glob("*.json")))
        
        cookie_dict = {}
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                clist = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                for c in clist:
                    name, value = c.get('name'), c.get('value')
                    if name and value:
                        cookie_dict[name] = value
            except Exception: continue
            
        for name, value in cookie_dict.items():
            domain = '.piccoma.com' if self.region == 'jp' else '.fr.piccoma.com'
            self.session.cookies.set(name, value, domain=domain)
        
        if cookie_dict:
            logger.debug(f"[Piccoma] 🍪 Loaded {len(cookie_dict)} deduplicated cookies.")

    def is_session_valid(self):
        """Checks if the current Piccoma session is still authenticated."""
        try:
            # Hit the favorites page which requires auth
            test_url = f"{self.base_url}/web/product/favorite"
            res = self.session.get(test_url, timeout=15, allow_redirects=False)
            # If we get a 200, we are good. If 302, we are likely redirected to login.
            return res.status_code == 200
        except Exception:
            return False

    def _get_regional_checksum(self, url):
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        
        if self.region == "fr":
            # FR uses 'q' parameter
            return qs.get('q', [''])[0]
        else:
            # JP uses path segment before filename (Episode ID)
            # Match pyccoma: img_url.split('/')[-2]
            return url.split('?')[0].split('/')[-2]

    def _calculate_pyccoma_seed(self, url):
        checksum = self._get_regional_checksum(url)
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        expires = qs.get('expires', [''])[0]
        
        # Match pyccoma.Scraper.get_seed literal loop:
        # for num in expiry_key: checksum = checksum[-int(num):] + checksum[:len(checksum)-int(num)]
        for num in expires:
            if num.isdigit():
                n = int(num)
                if n != 0:
                    checksum = checksum[-n:] + checksum[:len(checksum)-n]
        return checksum

    def get_series_info(self, url: str):
        match = re.search(r'/web/product/(\d+)', url)
        if not match:
            raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        if "jp.piccoma" in url or "piccoma.com/web" in url:
            self.base_url = "https://piccoma.com"
            self.region = "jp"
        elif "fr.piccoma" in url or "piccoma.com/fr" in url:
            self.base_url = "https://fr.piccoma.com"
            self.region = "fr"
        res = self.session.get(f"{self.base_url}/web/product/{series_id}")
        if res.status_code != 200:
            raise ScraperError("Failed to fetch Piccoma series page")
        
        soup = BeautifulSoup(res.text, 'html.parser')
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        # Extract series poster
        thumb_img = soup.select_one('img.PCM-productThum_img')
        if thumb_img and thumb_img.get('src'):
            image_url = thumb_img['src']
            if image_url.startswith('//'):
                image_url = 'https:' + image_url
            if 'cover_x2' in image_url:
                # Route through proxy to fix application/octet-stream Content-Type for Discord
                image_url = f"https://wsrv.nl/?url={urllib.parse.quote(image_url)}"
        else:
            og_img = soup.find("meta", property="og:image")
            image_url = og_img["content"] if og_img else None
            if image_url and 'cover_x2' in image_url:
                image_url = f"https://wsrv.nl/?url={urllib.parse.quote(image_url)}"

        # 2. Fetch Chapters (Episodes / Volumes)
        all_chapters = []
        
        # Try fetching episodes
        ep_res = self.session.get(f"{self.base_url}/web/product/{series_id}/episodes?etype=E")
        if ep_res.status_code == 200:
            ep_soup = BeautifulSoup(ep_res.text, 'html.parser')
            # 🟢 Check for the "UP" tag on the list item (li)
            li_items = ep_soup.select('ul.PCM-epList li')
            for li in li_items:
                link = li.select_one('a[data-episode_id]')
                if not link: continue
                
                ep_id = link['data-episode_id']
                title_node = link.select_one('div.PCM-epList_title h2')
                ep_title = title_node.text.strip() if title_node else f"Episode {ep_id}"
                
                # Check lock status based on button icons/classes
                is_locked = not bool(link.select_one('.PCM-epList_status_free'))
                
                # 🟢 Check for "UP" tag (PCM-stt_up class on li)
                is_new = 'PCM-stt_up' in li.get('class', [])
                
                all_chapters.append({
                    'id': str(ep_id),
                    'title': ep_title,
                    'number_text': str(len(all_chapters) + 1),
                    'url': f"{self.base_url}/web/viewer/{series_id}/{ep_id}",
                    'is_locked': is_locked,
                    'is_new': is_new
                })

        return title, len(all_chapters), all_chapters, image_url, str(series_id)

    def _load_available_accounts(self):
        """Loads and deduplicates cookies from piccoma secrets directory."""
        jt_dir = Settings.SECRETS_DIR / "piccoma"
        if not jt_dir.exists(): return []
        
        cookie_paths = sorted(list(jt_dir.glob("*.json")))
        
        accounts = []
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                clist = data if isinstance(data, list) else [{"name": k, "value": v} for k, v in data.items()]
                if any(c.get('name') == 'p_session' for c in clist): # Piccoma session cookie
                    accounts.append({'cookies': clist, 'name': path.name})
            except Exception: continue
        return accounts

    def _apply_session_cookies(self, session, cookie_list):
        session.cookies.clear()
        domain = '.piccoma.com' if self.region == 'jp' else '.fr.piccoma.com'
        for c in cookie_list:
            if c.get('name') and c.get('value'):
                session.cookies.set(c['name'], c['value'], domain=domain)
        logger.info(f"   🍪 Injected {len(cookie_list)} cookies.")

    def fast_purchase(self, task):
        """High-Speed API Purchase for Piccoma."""
        match = re.search(r'/web/viewer/(\d+)/(\d+)', task.url)
        if not match:
            logger.error(f"[Piccoma API] Invalid URL for purchase: {task.url}")
            return False, None
        
        series_id, episode_id = match.groups()
        accounts = self._load_available_accounts()
        
        # Determine base URL if not already set
        if "fr.piccoma" in task.url:
            self.base_url = "https://fr.piccoma.com"
            self.region = "fr"
        else:
            self.base_url = "https://piccoma.com"
            self.region = "jp"

        for acc in accounts:
            logger.info(f"[Piccoma API] ⚡ Attempting purchase with account: {acc['name']}")
            self._apply_session_cookies(self.session, acc['cookies'])
            
            # 1. Fetch the viewer page to check if it's already unlocked or find purchase triggers
            try:
                res = self.session.get(task.url, timeout=15)
                if res.status_code != 200: continue
                
                # 🟢 IMPROVED DETECTION: Coin vs Wait-Free vs Already Unlocked
                
                # 1. Check if already unlocked (will have _pdata_ with img list)
                if '_pdata_' in res.text and '"img":[' in res.text:
                    logger.info(f"[Piccoma API] ✅ Episode {episode_id} is already unlocked.")
                    return True, acc['cookies']
                
                soup = BeautifulSoup(res.text, 'html.parser')
                
                # 2. Look for "Wait Until Free" button (User's TikTok log showed .btn-waitfree)
                wait_free_btn = soup.select_one('.btn-waitfree, a[data-gtm-event="CLK_VIEWER_WAITFREE_BUTTON"]')
                
                if wait_free_btn:
                    logger.info(f"[Piccoma API] ⏳ Wait-Free button detected for E.{episode_id}. Attempting unlock...")
                    
                    # Based on user analysis, a standard redirect/GET might trigger it.
                    # We'll hit the URL again to confirm.
                    self.session.get(task.url, timeout=15)
                    time.sleep(1)
                    final_res = self.session.get(task.url, timeout=15)
                    
                    if '_pdata_' in final_res.text and '"img":[' in final_res.text:
                        logger.info(f"[Piccoma API] 🟢 Wait-Free unlock successful via GET trigger.")
                        return True, acc['cookies']
                    
                    # Fallback: Try a POST if it's part of a form
                    wf_form = wait_free_btn.find_parent('form')
                    if wf_form:
                         action = urljoin(self.base_url, wf_form.get('action', ''))
                         if action:
                            logger.info(f"[Piccoma API] 📤 Submitting Wait-Free form to {action}")
                            self.session.post(action, timeout=15)
                            final_res = self.session.get(task.url, timeout=15)
                            if '_pdata_' in final_res.text and '"img":[' in final_res.text:
                                return True, acc['cookies']
                
                # 3. Look for standard Purchase button
                purchase_btn = soup.select_one('a[data-user_access="require"], .PCM-viewer2ReadBtn[data-user_access="require"]')
                
                if purchase_btn:
                    pid = purchase_btn.get('data-product_id', series_id)
                    eid = purchase_btn.get('data-episode_id', episode_id)
                    
                    # Piccoma's web purchase API is often a POST to /web/episode/buy
                    buy_url = f"{self.base_url}/web/episode/buy"
                    payload = {
                        "episode_id": eid,
                        "product_id": pid,
                        "ticket_type": "RT03" # Default ticket type often seen in pdata or requests
                    }
                    
                    # CSRF usually handled via cookies or a meta tag if it were a form, 
                    # but Piccoma's API calls often just need the session cookie.
                    headers = {
                        "Referer": task.url,
                        "Origin": self.base_url,
                        "X-Requested-With": "XMLHttpRequest",
                        "Content-Type": "application/json"
                    }
                    
                    logger.info(f"[Piccoma API] 📤 Submitting purchase request for E.{eid}...")
                    buy_res = self.session.post(buy_url, json=payload, headers=headers, timeout=15)
                    
                    if buy_res.status_code == 200:
                        buy_data = buy_res.json()
                        if buy_data.get('status') == 'success' or buy_data.get('code') == 200:
                            logger.info(f"[Piccoma API] 🟢 Purchase successful for E.{eid}!")
                            return True, acc['cookies']
                        else:
                            logger.warning(f"[Piccoma API] ❌ Purchase rejected: {buy_data.get('message', 'Unknown error')}")
                    else:
                        logger.warning(f"[Piccoma API] ❌ Purchase request failed: {buy_res.status_code}")
                else:
                    logger.warning(f"[Piccoma API] 🔒 No purchase button found on viewer page.")
                    
            except Exception as e:
                logger.error(f"[Piccoma API] ❌ Error during purchase attempt: {e}")
                continue
                
        return False, None

    def scrape_chapter(self, task, output_dir):
        # Match series_id and chapter_id from the task URL
        match = re.search(r'/web/viewer/(\d+)/(\d+)', task.url)
        if not match:
            raise ScraperError("Invalid Piccoma Viewer URL")
        
        series_id, chapter_id = match.groups()
        
        res = self.session.get(task.url)
        if res.status_code != 200:
            raise ScraperError(f"Failed to access viewer page: {res.status_code}")
        
        # Find JSON payload in page (handles both JP and FR variants)
        soup = BeautifulSoup(res.text, 'html.parser')
        next_data = soup.select_one('script#__NEXT_DATA__')
        
        pdata = None
        if next_data:
            data = json.loads(next_data.string)
            pdata = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('viewer', {}).get('pData')
        
        if not pdata:
            # Fallback for older/different formats
            match = re.search(r'var\s+_pdata_\s*=\s*(\{.*?\})\s*(?:var\s+|</script>)', res.text, re.DOTALL)
            if match:
                pdata_str = match.group(1)
                try:
                    pdata = json.loads(pdata_str)
                except json.JSONDecodeError:
                    # _pdata_ is a JS object literal natively, so we must clean it for python evaluation
                    import ast
                    clean_str = pdata_str.replace('true', 'True').replace('false', 'False').replace('null', 'None')
                    try:
                        pdata = ast.literal_eval(clean_str)
                    except Exception as e:
                        logger.error(f"Failed to literal_eval _pdata_: {e}")
                        # Extreme fallback: Regex extract only what we need
                        pdata = {'img': [], 'isScrambled': False}
                        scrambled_match = re.search(r"['\"]isScrambled['\"]\s*:\s*(true|false)", pdata_str, re.IGNORECASE)
                        if scrambled_match:
                            pdata['isScrambled'] = scrambled_match.group(1).lower() == 'true'
                        
                        slice_match = re.search(r"['\"]sliceSize['\"]\s*:\s*(\d+)", pdata_str)
                        if slice_match:
                            pdata['sliceSize'] = int(slice_match.group(1))
                            
                        paths = re.findall(r"['\"]path['\"]\s*:\s*['\"]([^'\"]+)['\"]", pdata_str)
                        if paths:
                            pdata['img'] = [{'path': p} for p in paths]
        
        if not pdata:
            raise ScraperError("Could not extract chapter data. Chapter might be strictly locked.")

        images_data = pdata.get('img', pdata.get('contents', []))
        is_scrambled = pdata.get('isScrambled', False)
        slice_size = pdata.get('sliceSize', 50)  # Default for Piccoma is 50
        
        logger.info(f"   [Piccoma] is_scrambled: {is_scrambled} (sliceSize: {slice_size})")

        valid_images = [img for img in images_data if img.get('path')]
        if not valid_images:
            raise ScraperError("No images found. Chapter requires purchase.")

        # Match pyccoma: Calculate seed ONCE per chapter using the first page
        first_url = valid_images[0]['path']
        self.master_seed = self._calculate_pyccoma_seed(first_url)
        logger.debug(f"   [Piccoma] Master Seed: {self.master_seed} (Region: {self.region})")

        # Use a session with retries for robust downloads
        dl_session = requests.Session()
        retry_strategy = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry_strategy))

        logger.info(f"   Mapped {len(valid_images)} pages.")
        
        task.status = TaskStatus.DOWNLOADING
        total_pages = len(valid_images)
        downloaded = 0
        lock = threading.Lock()
        
        def process_piccoma(args):
            nonlocal downloaded
            img_data, i = args
            time.sleep(0.3) # 🟢 Standardized pacing
            self._download_and_unscramble_robust(dl_session, img_data, i+1, output_dir, is_scrambled, slice_size, task.chapter_str)
            
            with lock:
                downloaded += 1
                percent = int((downloaded / total_pages) * 100)
                bar_length = 20
                filled_length = int(bar_length * downloaded // total_pages)
                bar = '▰' * filled_length + '▱' * (bar_length - filled_length)
                import sys
                sys.stdout.write(f"\r[INFO] [{task.req_id}] - Downloading: [{task.service}] {bar} {downloaded}/{total_pages} ({percent}%)")
                sys.stdout.flush()

        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(process_piccoma, [(img, i) for i, img in enumerate(valid_images)]))
        
        print() # Newline after progress
            
        return output_dir

    def _download_and_unscramble_robust(self, dl_session, img_data, idx, out_dir, is_scrambled, slice_size, chapter_str="1"):
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        
        res = dl_session.get(url, timeout=30)
        res.raise_for_status()
            
        out_path = f"{out_dir}/page_{idx:03d}.png"
        
        # Match pyccoma: Unscrambling is triggered by an UPPERCASE seed
        seed = self.master_seed
        if seed and seed.isupper():
            logger.debug(f"   [Piccoma] P{idx}: Unscrambling triggered (Seed: {seed})")
            try:
                # Use pycasso.Canvas exactly as pyccoma does
                img_io = BytesIO(res.content)
                
                canvas = Canvas(img_io, (50, 50), dd(seed))
                unscrambled = canvas.export(
                    mode="scramble",
                    format="png"
                )
                with open(out_path, "wb") as f: f.write(unscrambled.getvalue())
                logger.info(f"   [Piccoma] P{idx}: Unscrambled successfully.")
            except Exception as e:
                logger.error(f"   [Piccoma] P{idx}: Unscrambling failed: {e}", exc_info=True)
                with open(out_path, "wb") as f: f.write(res.content)
        else:
            logger.debug(f"   [Piccoma] P{idx}: Saving raw (No scrambling detected).")
            with open(out_path, "wb") as f: f.write(res.content)
