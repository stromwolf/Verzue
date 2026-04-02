import asyncio
import os
import sys
import shutil
import re
from pathlib import Path
from dataclasses import dataclass
import logging

# Add root for app imports
sys.path.append(os.getcwd())

from config.settings import Settings
from app.providers.platforms.piccoma import PiccomaProvider
from app.services.image.stitcher import ImageStitcher

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PiccomaTestFinal")

@dataclass
class DummyTask:
    url: str
    id: str = "finaltest"
    req_id: str = "test"
    service: str = "piccoma"
    series_title: str = "PiccomaFinal"

async def download_chapter(url: str):
    logger.info(f"🚀 Starting FINAL Local Piccoma Download: {url}")
    
    match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', url)
    series_id = match.group(1) if match else "unknown"
    chapter_id = match.group(2) if match else "unknown"
    
    task = DummyTask(url=url, id=chapter_id, series_title=f"Piccoma_{series_id}")
    raw_dir = Settings.DOWNLOAD_DIR / f"raw_piccoma_final_{chapter_id}"
    final_dir = Settings.DOWNLOAD_DIR / f"final_piccoma_final_{chapter_id}"
    
    if raw_dir.exists(): shutil.rmtree(raw_dir)
    if final_dir.exists(): shutil.rmtree(final_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)
    
    provider = PiccomaProvider()
    
    import json
    cookie_path = Settings.DATA_DIR / "secrets" / "piccoma" / "cookies.json"
    if cookie_path.exists():
        with open(cookie_path, 'r') as f:
            cookies = json.load(f)
        async def mock_session(platform="piccoma"):
            return {"account_id": "local", "cookies": {c['name']: c['value'] for c in cookies}, "status": "HEALTHY"}
        provider.session_service.get_active_session = mock_session
    
    try:
        logger.info(f"🔍 Downloading images to {raw_dir}...")
        await provider.scrape_chapter(task, str(raw_dir))
        logger.info(f"✅ Images downloaded and unscrambled successfully.")
    except Exception as e:
        logger.error(f"❌ Failed during scraping/unscrambling: {e}")
        return None

    logger.info(f"🧵 Stitching folder into final output...")
    try:
        final_path = ImageStitcher.stitch_folder(str(raw_dir), str(final_dir), max_slice_height=7000, service_name="Piccoma")
        logger.info(f"🎉 FINAL SUCCESS! Stitched images saved in: {final_dir}")
        shutil.rmtree(raw_dir)
        return final_dir
    except Exception as e:
        logger.error(f"❌ Stitching failed: {e}")
        return None

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://piccoma.com/web/viewer/206094/6192831"
    asyncio.run(download_chapter(url))
