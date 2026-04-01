import os
import sys
import asyncio
import logging
import shutil
import re
from pathlib import Path
from dataclasses import dataclass

# Add root for app imports
sys.path.append(os.getcwd())

# Configuration
from config.settings import Settings

# Provider and Services
from app.providers.platforms.piccoma import PiccomaProvider
from app.services.image.stitcher import ImageStitcher

# Setup professional logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("PiccomaLocal")

@dataclass
class DummyTask:
    url: str
    id: str = "local_test"
    req_id: str = "local"
    service: str = "piccoma"
    series_title: str = "PiccomaChapter"
    title: str = "Chapter"
    scan_group: str = "Local"
    chapter_str: str = "Test"

async def mock_session_service(platform: str = "piccoma"):
    """Mocks SessionService if Redis is unavailable, using local JSON cookies if possible."""
    import json
    cookie_path = Settings.DATA_DIR / "secrets" / "piccoma" / "cookies.json"
    
    if cookie_path.exists():
        try:
            with open(cookie_path, 'r') as f:
                cookies = json.load(f)
            logger.info("🍪 Using local cookies from data/secrets/piccoma/cookies.json")
            return {
                "account_id": "local",
                "cookies": cookies,
                "status": "HEALTHY"
            }
        except: pass
    
    logger.warning("⚠️ No healthy sessions in Redis and no local cookies found.")
    return None

async def download_chapter(url: str):
    logger.info(f"🚀 Starting Local Piccoma Download: {url}")
    
    # Extract IDs for naming
    match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', url)
    series_id = match.group(1) if match else "unknown"
    chapter_id = match.group(2) if match else "unknown"
    
    task = DummyTask(url=url, id=chapter_id, series_title=f"Piccoma_{series_id}")
    
    # Clean and Prepare Output Directories
    raw_dir = Settings.DOWNLOAD_DIR / f"raw_piccoma_{chapter_id}"
    final_dir = Settings.DOWNLOAD_DIR / f"final_piccoma_{chapter_id}"
    
    if raw_dir.exists(): shutil.rmtree(raw_dir)
    if final_dir.exists(): shutil.rmtree(final_dir)
    
    raw_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)
    
    provider = PiccomaProvider()
    
    # 🕵️ SESSION FALLBACK: Mock the service to bypass Redis during local usage
    provider.session_service.get_active_session = mock_session_service
    
    # 🕵️ AUTH LOOP:
    try:
        logger.info(f"🔍 Step 1: Downloading images to {raw_dir}...")
        # PiccomaProvider.scrape_chapter uses auth_session internally
        await provider.scrape_chapter(task, str(raw_dir))
        logger.info(f"✅ Images downloaded and unscrambled successfully.")
    except Exception as e:
        logger.error(f"❌ Failed during scraping/unscrambling: {e}")
        return None

    # 🧵 Step 2: Stitching
    logger.info(f"🧵 Step 2: Stitching folder into final output...")
    try:
        final_path = ImageStitcher.stitch_folder(
            str(raw_dir),
            str(final_dir),
            max_slice_height=7000, 
            service_name="Piccoma"
        )
        logger.info(f"🎉 SUCCESS! Stitched image saved in: {final_dir}")
        
        # Cleanup raw images to save space
        shutil.rmtree(raw_dir)
        return final_dir
    except Exception as e:
        logger.error(f"❌ Stitching failed: {e}")
        return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python piccoma_local.py <piccoma_viewer_url>")
        print("Example: python piccoma_local.py https://piccoma.com/web/viewer/206094/6192831")
        sys.exit(1)
        
    url = sys.argv[1]
    asyncio.run(download_chapter(url))
