import os
import sys
import asyncio
import logging
import shutil
import re
import itertools
import urllib.parse
from pathlib import Path
from dataclasses import dataclass
from io import BytesIO

# Add root for app imports
sys.path.append(os.getcwd())

from config.settings import Settings
from app.providers.platforms.piccoma import PiccomaProvider
from app.services.image.stitcher import ImageStitcher
from app.lib.pycasso import Canvas

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PiccomaDiagnostic")

@dataclass
class DummyTask:
    url: str
    id: str = "diagnostic"
    req_id: str = "diag"
    service: str = "piccoma"
    series_title: str = "PiccomaDiag"

async def matrix_diagnostic(url: str):
    logger.info(f"🧪 Piccoma 16-Mode MATRIX Diagnostic: {url}")
    
    # 1. Setup
    match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', url)
    chapter_id = match.group(2) if match else "unknown"
    base_dir = Settings.DOWNLOAD_DIR / f"matrix_{chapter_id}"
    if base_dir.exists(): shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)
    
    provider = PiccomaProvider()
    
    # 🕵️ SESSION FALLBACK: Use local cookies
    import json
    cookie_path = Settings.DATA_DIR / "secrets" / "piccoma" / "cookies.json"
    cookies = {}
    if cookie_path.exists():
        with open(cookie_path, 'r') as f:
            cookie_list = json.load(f)
            cookies = {c['name']: c['value'] for c in cookie_list}
    
    # 🕵️ FETCH SINGLE IMAGE & MANIFEST:
    from curl_cffi import requests as c_requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://piccoma.com/",
        "Origin": "https://piccoma.com"
    }
    
    res = c_requests.get(url, cookies=cookies, headers=headers, impersonate="chrome120")
    if res.status_code != 200:
        logger.error(f"❌ Viewer page failed: {res.status_code}")
        return

    # Extract manifest heuristic - DIRECT CDN SEARCH
    img_urls = re.findall(r'https://pcm.kakaocdn.net/dna/[^"\'\s>]+', res.text)
    
    if not img_urls:
        # Fallback to NEXT_DATA
        try:
            next_data = re.search(r'script id="__NEXT_DATA__".*?>({.*?})</script>', res.text).group(1)
            data = json.loads(next_data)
            pdata = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('viewer', {}).get('pData', {})
            if isinstance(pdata, list): img_urls = [p['path'] for p in pdata]
            else: img_urls = [img.get('path', img.get('url', '')) for img in pdata.get('img', []) if isinstance(img, dict)]
        except: pass

    # Clean and make absolute
    img_urls = [u.replace('\\u0026', '&').replace('\\u003d', '=') for u in img_urls if u]
    if img_urls and not img_urls[0].startswith('http'):
        netloc = urllib.parse.urlparse(url).netloc
        img_urls = [f"https://{netloc}{u}" if u.startswith('/') else u for u in img_urls]

    if not img_urls:
        logger.error(f"❌ Could not find manifest ANYWHERE. HTML Length: {len(res.text)}")
        return

    # Download first image only
    img_url = img_urls[0]
    # Remove trailing junk if any
    img_url = img_url.split('"')[0].split("'")[0].split(' ')[0].split('>')[0]
    
    logger.info(f"📸 Downloading test image: {img_url}")
    img_res = c_requests.get(img_url, cookies=cookies, headers=headers, impersonate="chrome120")
    if img_res.status_code != 200:
        logger.error(f"❌ Image download failed: {img_res.status_code}")
        return
    img_bytes = img_res.content
    
    # Extract base seed and expires
    path_only = img_url.split('?')[0].rstrip('/')
    segments = [s for s in path_only.split('/') if s]
    chk_raw = segments[-2] if len(segments) >= 2 else segments[-1]
    expires_raw = re.search(r'expires=(\d+)', img_url).group(1) if 'expires=' in img_url else ""
    
    logger.info(f"🔑 Base Seed: {chk_raw}")
    logger.info(f"⏳ Expires: {expires_raw}")

    # 🧵 16-MODE MATRIX:
    for rotate_dir, use_dd, mode, exp_order in itertools.product(["R", "L"], [True, False], ["S", "U"], ["N", "R"]):
        label = f"ROT-{rotate_dir}_DD-{use_dd}_MODE-{mode}_EXP-{exp_order}"
        
        # Calculate seed for this variant
        chk = chk_raw
        exp_str = expires_raw if exp_order == "N" else expires_raw[::-1]
        
        if expires_raw and chk:
            for num in exp_str:
                if num.isdigit() and int(num) != 0:
                    shift = int(num)
                    if rotate_dir == "R":
                        chk = chk[-shift:] + chk[:-shift]
                    else: # Left Rotate
                        chk = chk[shift:] + chk[:shift]
        
        final_seed = provider._dd_transform(chk) if use_dd else chk
        p_mode = "scramble" if mode == "S" else "unscramble"
        
        try:
            canvas = Canvas(BytesIO(img_bytes), (50, 50), final_seed)
            out = canvas.export(mode=p_mode, format="png").getvalue()
            with open(base_dir / f"{label}.png", "wb") as f:
                f.write(out)
        except Exception as e:
            logger.error(f"   Failed {label}: {e}")

    logger.info(f"🎉 16-MODE MATRIX COMPLETE! Check the results in: {base_dir}")
    logger.info("One of these 16 PNGs is THE winner.")

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://piccoma.com/web/viewer/206094/6192831"
    asyncio.run(matrix_diagnostic(url))
