import os
import json
import re
import time
import logging
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests

logger = logging.getLogger("AcQqApiScraper")

class AcqqApiScraper(BaseScraper):
    BASE_URL = "https://ac.qq.com"

    def __init__(self, browser_service=None):
        # 1. MIMIC YOUR CHROME OS ENVIRONMENT
        # We use a real browser fingerprint to stop the ISP from resetting the connection
        self.session = crequests.Session(impersonate="chrome120")
        
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://m.ac.qq.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'Sec-Ch-Ua-Mobile': '?1',
            'Sec-Ch-Ua-Platform': '"Chrome OS"',
            'Upgrade-Insecure-Requests': '1'
        })
        self.browser = None

    def get_series_info(self, url: str):
        """Phase 1: Metadata Extraction (Mobile Priority)"""
        match = re.search(r'id/(\d+)', url)
        if not match: raise ScraperError("Invalid Tencent URL.")
        series_id = match.group(1)
        
        content = None
        
        # 1. Try Mobile URL First (Bypasses ISP Block more reliably)
        target_url = f"https://m.ac.qq.com/comic/index/id/{series_id}"
        logger.info(f"🔍 Fetching Metadata: {target_url}")
        
        try:
            res = self.session.get(target_url, timeout=10)
            if res.status_code == 200:
                res.encoding = 'utf-8'
                content = res.text
            else:
                logger.warning(f"⚠️ API Status {res.status_code}")
        except Exception as e:
            # If Mobile fails, we have no fallback that works on your network
            raise ScraperError(f"Connection failed (ISP Block). Error: {e}")

        # 2. Parse HTML
        soup = BeautifulSoup(content, 'html.parser')

        # Title & Cover (Meta tags are universal)
        title = soup.find("meta", property="og:title")
        title = title["content"] if title else "Unknown"
        
        cover = soup.find("meta", property="og:image")
        image_url = cover["content"] if cover else None

        # 3. Chapter List
        all_chapters = []
        # Mobile selectors
        items = soup.select(".chapter-item, .works-chapter-item, .chapter-link")
        
        for idx, item in enumerate(items):
            link = item if item.name == 'a' else item.find("a")
            if not link: continue
            
            href = link.get('href')
            if not href: continue
            
            cid_match = re.search(r'cid/(\d+)', href)
            if not cid_match: continue
            
            # Check for lock icon
            is_locked = bool(item.select_one(".lock, .vip, .ui-icon-pay"))
            
            # Clean Title
            raw_text = link.get_text(strip=True)
            # Remove "道詭異仙" prefix if present
            clean_title = raw_text.split(title)[-1].strip() if title in raw_text else raw_text
            clean_title = clean_title.replace('：', '').strip()

            all_chapters.append({
                'id': cid_match.group(1),
                'title': clean_title,
                'url': urljoin("https://m.ac.qq.com", href), # Use Mobile URL for extraction
                'is_locked': is_locked,
                'number_text': str(idx + 1)
            })

        all_chapters.sort(key=lambda x: int(x['id']))
        return title, len(all_chapters), all_chapters, image_url, series_id

    def scrape_chapter(self, task, output_dir):
        """Phase 5: Execution (Mobile Reader Parsing)"""
        logger.info(f"⚡ API Download: {task.url}")
        
        try:
            # Ensure we are using the Mobile URL
            if "m.ac.qq.com" not in task.url:
                task.url = task.url.replace("ac.qq.com/ComicView", "m.ac.qq.com/chapter")
            
            res = self.session.get(task.url, timeout=20)
            res.encoding = 'utf-8'
            html = res.text
            
            image_urls = []
            
            # Strategy A: Mobile often puts images in simple <img> tags with lazy-load attributes
            soup = BeautifulSoup(html, 'html.parser')
            imgs = soup.select(".comic-pic-list img, #comic-pic-list img, .comic-contain img")
            
            for img in imgs:
                src = img.get('src') or img.get('data-src')
                if src and 'http' in src and '.gif' not in src:
                    image_urls.append(src)

            # Strategy B: Javascript Data
            if not image_urls:
                json_match = re.search(r'var\s+(?:DATA|_v)\s*=\s*({.*?});', html, re.DOTALL)
                if json_match:
                    try:
                        data = json.loads(json_match.group(1))
                        if 'picture' in data:
                            image_urls = [p.get('url') for p in data['picture']]
                    except: pass

            if not image_urls:
                raise ScraperError("No images found. Chapter might be locked.")

            logger.info(f"✅ Extracted {len(image_urls)} images.")

            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            dl_session = requests.Session()
            retry = Retry(total=5, backoff_factor=1.5, status_forcelist=[429, 503])
            dl_session.mount("https://", HTTPAdapter(max_retries=retry))
            dl_session.headers.update({'Referer': 'https://m.ac.qq.com/'})

            task.status = TaskStatus.DOWNLOADING
            with ThreadPoolExecutor(max_workers=5) as executor:
                list(executor.map(lambda x: self._download_image_robust(dl_session, x[1], x[0]+1, output_dir), enumerate(image_urls)))

            return output_dir

        except Exception as e:
            logger.error(f"Scrape failed: {e}")
            raise ScraperError(str(e))

    def _download_image_robust(self, dl_session, url, idx, out_dir):
        time.sleep(0.4)
        res = dl_session.get(url, timeout=30)
        if res.status_code == 200:
            with open(os.path.join(output_dir, f"page_{idx:03d}.jpg"), "wb") as f:
                f.write(res.content)
