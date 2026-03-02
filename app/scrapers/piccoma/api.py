import re
import json
import logging
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from io import BytesIO

try:
    from PIL import Image
except ImportError:
    raise ImportError("Pillow is required for Piccoma unscrambling. Run: pip install Pillow")

try:
    from curl_cffi import requests as crequests
except ImportError:
    import requests as crequests

from app.scrapers.base import BaseScraper
from app.core.exceptions import ScraperError
from app.models.chapter import TaskStatus

logger = logging.getLogger("PiccomaApi")

# --- CUSTOM PRNG FOR UNSCRAMBLING ---
WIDTH = 256
CHUNKS = 6
DIGITS = 52
START_DENOM = WIDTH ** CHUNKS
SIGNIFICANCE = 2 ** DIGITS
OVERFLOW = SIGNIFICANCE * 2
MASK = WIDTH - 1

class ARC4:
    def __init__(self, key):
        self.i = 0
        self.j = 0
        self.S = list(range(WIDTH))
        keylen = len(key)
        if not keylen:
            key = [1]
            keylen = 1
        
        j = 0
        for i in range(WIDTH):
            t = self.S[i]
            j = (j + key[i % keylen] + t) & MASK
            self.S[i] = self.S[j]
            self.S[j] = t

    def g(self, count):
        r = 0
        for _ in range(count):
            self.i = (self.i + 1) & MASK
            t = self.S[self.i]
            self.j = (self.j + t) & MASK
            self.S[self.i], self.S[self.j] = self.S[self.j], self.S[self.i]
            r = r * WIDTH + self.S[(self.S[self.i] + self.S[self.j]) & MASK]
        return r

def mixkey(seed):
    stringseed = str(seed)
    key = []
    smear = 0
    j = 0
    while j < len(stringseed):
        idx = MASK & j
        while len(key) <= idx:
            key.append(0)
        val = key[idx]
        smear ^= (val * 19)
        key[idx] = MASK & (smear + ord(stringseed[j]))
        j += 1
    return key

def prng_generator(seed):
    key = mixkey(seed)
    arc4 = ARC4(key)
    
    def random_func():
        n = arc4.g(CHUNKS)
        d = START_DENOM
        x = 0
        while n < SIGNIFICANCE:
            n = (n + x) * WIDTH
            d *= WIDTH
            x = arc4.g(1)
        while n >= OVERFLOW:
            n /= 2
            d /= 2
            x = int(x / 2)
        return (n + x) / d
    return random_func

def shuffle_seed(arr, seed):
    size = len(arr)
    rng = prng_generator(seed)
    resp = []
    keys = list(range(size))
    for _ in range(size):
        r = int(rng() * len(keys))
        g = keys[r]
        keys.pop(r)
        resp.append(arr[g])
    return resp

def get_seed(url):
    if not url.startswith('http'):
        url = 'https:' + url
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    
    checksum = qs.get('q', [url.split('/')[-2]])[0]
    expires = qs.get('expires', [''])[0]
    
    if expires:
        total = sum(int(x) for x in expires if x.isdigit())
        ch = total % len(checksum)
        if ch > 0:
            checksum = checksum[-ch:] + checksum[:-ch]
    return checksum


class PiccomaApiScraper(BaseScraper):
    def __init__(self):
        self.session = crequests.Session(impersonate="chrome120")
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        })
        self.base_url = "https://piccoma.com"

    def get_series_info(self, url: str):
        match = re.search(r'/web/product/(\d+)', url)
        if not match:
            raise ScraperError("Invalid Piccoma URL")
        
        series_id = match.group(1)
        if "jp.piccoma.com" in url:
            self.base_url = "https://jp.piccoma.com"

        # 1. Fetch Series Page for Title
        res = self.session.get(f"{self.base_url}/web/product/{series_id}")
        if res.status_code != 200:
            raise ScraperError("Failed to fetch Piccoma series page")
        
        soup = BeautifulSoup(res.text, 'html.parser')
        title_elem = soup.select_one('h1.PCM-productTitle')
        title = title_elem.text.strip() if title_elem else f"Piccoma_{series_id}"
        
        og_img = soup.find("meta", property="og:image")
        image_url = og_img["content"] if og_img else None

        # 2. Fetch Chapters (Episodes / Volumes)
        all_chapters = []
        
        # Try fetching episodes
        ep_res = self.session.get(f"{self.base_url}/web/product/{series_id}/episodes?etype=E")
        if ep_res.status_code == 200:
            ep_soup = BeautifulSoup(ep_res.text, 'html.parser')
            items = ep_soup.select('ul.PCM-epList li a[data-episode_id]')
            for item in items:
                ep_id = item['data-episode_id']
                title_node = item.select_one('div.PCM-epList_title h2')
                ep_title = title_node.text.strip() if title_node else f"Episode {ep_id}"
                
                # Check lock status based on button icons/classes
                is_locked = not bool(item.select_one('.PCM-epList_freeBtn')) 
                
                all_chapters.append({
                    'id': str(ep_id),
                    'title': ep_title,
                    'number_text': str(len(all_chapters) + 1),
                    'url': f"{self.base_url}/web/viewer/{series_id}/{ep_id}",
                    'is_locked': is_locked
                })

        return title, len(all_chapters), all_chapters, image_url, str(series_id)

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
            match = re.search(r'window\._pdata_\s*=\s*(\{.*?\});', res.text, re.DOTALL)
            if match:
                pdata = json.loads(match.group(1))
        
        if not pdata:
            raise ScraperError("Could not extract chapter data. Chapter might be strictly locked.")

        images_data = pdata.get('img', pdata.get('contents', []))
        is_scrambled = pdata.get('isScrambled', False)

        valid_images = [img for img in images_data if img.get('path')]
        if not valid_images:
            raise ScraperError("No images found. Chapter requires purchase.")

        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        dl_session = requests.Session()
        retry = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500])
        dl_session.mount("https://", HTTPAdapter(max_retries=retry))

        logger.info(f"   Mapped {len(valid_images)} pages.")
        task.status = TaskStatus.DOWNLOADING
        
        def process_piccoma(args):
            img_data, i = args
            time.sleep(0.5)
            self._download_and_unscramble_robust(dl_session, img_data, i+1, output_dir, is_scrambled)

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(process_piccoma, [(img, i) for i, img in enumerate(valid_images)]))
            
        return output_dir

    def _download_and_unscramble_robust(self, dl_session, img_data, idx, out_dir, is_scrambled):
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        
        res = dl_session.get(url, timeout=30)
        res.raise_for_status()
            
        out_path = f"{out_dir}/page_{idx:03d}.png"
        if is_scrambled:
            seed = get_seed(url)
            unscrambled_bytes = self._unscramble_image(res.content, seed)
            with open(out_path, "wb") as f: f.write(unscrambled_bytes)
        else:
            with open(out_path, "wb") as f: f.write(res.content)

    def _unscramble_image(self, image_bytes, seed):
        img = Image.open(BytesIO(image_bytes))
        canvas = Image.new('RGBA', (img.width, img.height))
        slice_size = 50

        # Calculate slices
        total_parts = math.ceil(img.width / slice_size) * math.ceil(img.height / slice_size)
        vertical_slices = math.ceil(img.width / slice_size)
        
        slices = {}
        for i in range(total_parts):
            row = i // vertical_slices
            col = i - row * vertical_slices
            x = col * slice_size
            y = row * slice_size
            width = slice_size if (x + slice_size <= img.width) else (img.width - x)
            height = slice_size if (y + slice_size <= img.height) else (img.height - y)
            
            key = f"{width}-{height}"
            if key not in slices:
                slices[key] = []
            slices[key].append({"x": x, "y": y, "width": width, "height": height})

        # Process each group of identically sized blocks
        for key, group_slices in slices.items():
            # Get group boundaries
            t = group_slices[0]['y']
            cols = next((i for i, s in enumerate(group_slices) if s['y'] != t), len(group_slices))
            
            group_x = group_slices[0]['x']
            group_y = group_slices[0]['y']
            
            shuffle_ind = list(range(len(group_slices)))
            shuffle_ind = shuffle_seed(shuffle_ind, seed)

            for i in range(len(group_slices)):
                s = shuffle_ind[i]
                row = s // cols
                col = s - row * cols
                
                target_x = col * group_slices[i]['width']
                target_y = row * group_slices[i]['height']
                
                src_box = (
                    group_slices[i]['x'],
                    group_slices[i]['y'],
                    group_slices[i]['x'] + group_slices[i]['width'],
                    group_slices[i]['y'] + group_slices[i]['height']
                )
                region = img.crop(src_box)
                
                dst_pos = (
                    int(group_x + target_x),
                    int(group_y + target_y)
                )
                canvas.paste(region, dst_pos)
        
        out_io = BytesIO()
        # Convert to RGB before saving as WebP or JPEG
        canvas.convert("RGB").save(out_io, format='WEBP')
        return out_io.getvalue()