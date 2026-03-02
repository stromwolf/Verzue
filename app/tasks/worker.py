import os, shutil, asyncio, logging, time, re
from pathlib import Path
from functools import partial
from concurrent.futures import ProcessPoolExecutor

from config.settings import Settings
from app.models.chapter import ChapterTask, TaskStatus
from app.services.image.stitcher import ImageStitcher
from app.core.events import EventBus

logger = logging.getLogger("TaskWorker")

# Create a single global Process Pool with exactly 1 worker.
# Since you have 2 cores: Core 1 runs the bot, Core 2 runs this worker.
PROCESS_POOL = ProcessPoolExecutor(max_workers=1)

class TaskWorker:
    def __init__(self, scraper_registry, uploader):
        self.registry = scraper_registry
        self.uploader = uploader

    async def process_task(self, task: ChapterTask):
        start_time = time.time()
        logger.info(f"🚀 STARTING TASK: [{task.series_title}] - {task.title}")
        
        safe_series = "".join([c for c in task.series_title if c.isalnum() or c in " -_"]).strip()
        task_dir_name = f"{safe_series}_{task.id}"
        raw_dir, final_dir = Settings.DOWNLOAD_DIR / f"raw_{task_dir_name}", Settings.DOWNLOAD_DIR / f"final_{task_dir_name}"

        self._clean_dirs(raw_dir, final_dir)
        raw_dir.mkdir(parents=True, exist_ok=True); final_dir.mkdir(parents=True, exist_ok=True)

        try:
            task.status = TaskStatus.DOWNLOADING
            scraper = self.registry.get_scraper(task.url, is_smartoon=task.is_smartoon)
            logger.info(f"🔍 STAGE 1/3: Engine: {scraper.__class__.__name__}")
            
            # Scraping remains in a thread (for now) as it relies on sync requests/playwright
            await asyncio.to_thread(scraper.scrape_chapter, task, str(raw_dir))
            
            valid_imgs = [f for f in os.listdir(raw_dir) if f.lower().endswith(('.png', '.webp', '.jpg'))]
            if not valid_imgs: raise Exception("No images found after scrape.")
            logger.info(f"✅ STAGE 1 COMPLETE: {len(valid_imgs)} images.")

            # --- STAGE 2: STITCHING (Offloaded to Core 2 via ProcessPool) ---
            logger.info("🧵 STAGE 2/3: Stitching (Offloading to dedicated CPU core)...")
            
            seed_string = None
            if "jumptoon.com" in task.url.lower():
                seed_string = f"{task.series_id_key}:{int(task.id)}"
            elif "webtoon.kakao.com" in task.url.lower():
                seed_string = task.series_id_key

            loop = asyncio.get_running_loop()
            
            # Pack the function and arguments to send across the process boundary
            stitch_func = partial(
                ImageStitcher.stitch_folder, 
                str(raw_dir), 
                str(final_dir), 
                15000, 
                episode_id=seed_string
            )
            
            # Execute on Core 2. The bot event loop on Core 1 remains 100% free!
            await loop.run_in_executor(PROCESS_POOL, stitch_func)
            
            task.status = TaskStatus.UPLOADING
            if self.uploader:
                if task.pre_created_folder_id: await self._fast_upload(task, final_dir)
                else: await self._handle_upload_hierarchy(task, final_dir)
            
            elapsed = time.time() - start_time
            logger.info(f"🏁 TASK FINISHED in {elapsed:.2f}s")
            task.status = TaskStatus.COMPLETED

        except Exception as e:
            task.status = TaskStatus.FAILED
            logger.error(f"❌ TASK FAILURE: {e}")
            await EventBus.emit("task_failed", task, str(e))
            raise e
        finally:
            if self.uploader: self._clean_dirs(raw_dir, final_dir)

    async def _fast_upload(self, task, local_path):
        files = sorted([f for f in os.listdir(local_path) if f.endswith('.jpg')])
        for f in files:
            await asyncio.to_thread(self.uploader.upload_file, os.path.join(local_path, f), f, task.pre_created_folder_id)
        if task.final_folder_name:
            await asyncio.to_thread(self.uploader.rename_file, task.pre_created_folder_id, task.final_folder_name)

    def _clean_dirs(self, r, f):
        """Robust cleanup with retry to avoid WinError 32."""
        for path in [r, f]:
            if not path.exists(): continue
            for attempt in range(3):
                try:
                    shutil.rmtree(path)
                    break
                except PermissionError:
                    # Wait for system to release file handles (Common on Windows)
                    logger.warning(f"⚠️ Directory locked ({path}), retrying cleanup in 1s... (Attempt {attempt+1})")
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"Could not clean {path}: {e}")
                    break


# --- EXECUTION BLOCK (No indentation at the start of this line) ---
if __name__ == "__main__":
    from app.core.logger import setup_logging
    import json
    import psutil
    
    # Setup logger for the worker terminal
    setup_logging("WorkerMain")
    
    async def run_worker():
        logger.info("👷 Background Worker starting up...")
        
        # 1. Import and Initialize Services
        from app.services.redis_manager import RedisManager
        from app.scrapers.registry import ScraperRegistry
        from app.services.browser.driver import BrowserService
        from app.services.gdrive.client import GDriveClient
        from app.services.gdrive.uploader import GDriveUploader
        
        redis = RedisManager()
        browser = BrowserService(headless=True) # Workers should run headless
        registry = ScraperRegistry(browser)
        
        try:
            gdrive = GDriveClient()
            uploader = GDriveUploader(gdrive)
            logger.info("☁️ GDrive connected for worker.")
        except Exception as e:
            logger.warning(f"⚠️ GDrive connection failed: {e}")
            uploader = None
            
        worker = TaskWorker(registry, uploader)
        queue_name = "verzue:task_queue" 
        
        MIN_RAM_MB = 600  # Set your safety limit (e.g., 600 MB free)
        logger.info(f"💾 RAM Manager active. Minimum required: {MIN_RAM_MB}MB")
        
        logger.info(f"🎧 Listening to Redis queue '{queue_name}' for tasks...")
        
        # 2. Continuous Polling Loop
        while True:
            try:
                # Check actual system memory available
                mem = psutil.virtual_memory()
                available_ram_mb = mem.available / (1024 * 1024)
                
                if available_ram_mb < MIN_RAM_MB:
                    logger.warning(f"⚠️ Low Memory ({available_ram_mb:.0f}MB free). Waiting 10s before checking queue...")
                    await asyncio.sleep(10)
                    continue # Skips the rest of the loop and checks again 

                result = await redis.client.blpop(queue_name, timeout=1)
                if not result:
                    continue 
                    
                _, task_json = result
                task_dict = json.loads(task_json)
                task = ChapterTask(**task_dict)
                
                await worker.process_task(task)
                
            except Exception as e:
                logger.error(f"❌ Worker loop error: {e}")
                await asyncio.sleep(5) 

    # Execute the async loop
    asyncio.run(run_worker())