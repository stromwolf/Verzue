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
PROCESS_POOL = ProcessPoolExecutor(max_workers=1)

class TaskWorker:
    def __init__(self, scraper_registry, uploader):
        self.registry = scraper_registry
        self.uploader = uploader

    async def process_task(self, task: ChapterTask):
        start_time = time.time()
        logger.info(f"🚀 STARTING TASK: [{task.series_title}] - {task.title}")
        
        # 🟢 THE FORK: Start the Google Drive Folder creation IMMEDIATELY in the background
        # We wrap it in asyncio.create_task so it runs concurrently with scraping!
        drive_folder_task = None
        if self.uploader and not task.pre_created_folder_id:
            logger.info("☁️ [Parallel Track] Initiating Google Drive folder sync...")
            drive_folder_task = asyncio.create_task(self._ensure_drive_folder(task))

        safe_series = "".join([c for c in task.series_title if c.isalnum() or c in " -_"]).strip()
        task_dir_name = f"{safe_series}_{task.id}"
        raw_dir, final_dir = Settings.DOWNLOAD_DIR / f"raw_{task_dir_name}", Settings.DOWNLOAD_DIR / f"final_{task_dir_name}"

        self._clean_dirs(raw_dir, final_dir)
        raw_dir.mkdir(parents=True, exist_ok=True); final_dir.mkdir(parents=True, exist_ok=True)

        try:
            task.status = TaskStatus.DOWNLOADING
            scraper = self.registry.get_scraper(task.url, is_smartoon=task.is_smartoon)
            logger.info(f"🔍 STAGE 1/3: Engine: {scraper.__class__.__name__}")
            
            # --- LOCAL DOWNLOAD TRACK ---
            # Scraping blocks the thread but frees the async loop so Drive API can run
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
            stitch_func = partial(
                ImageStitcher.stitch_folder, 
                str(raw_dir), 
                str(final_dir), 
                15000, 
                episode_id=seed_string
            )
            await loop.run_in_executor(PROCESS_POOL, stitch_func)
            
            # --- STAGE 3: UPLOADING ---
            task.status = TaskStatus.UPLOADING
            if self.uploader:
                # 🟢 THE MERGE POINT: Wait for the Drive folder task to finish if it's still running
                # Usually, it finishes long before Stage 1 & 2 are done, returning instantly!
                if drive_folder_task:
                    logger.info("⏳ Synchronizing with Google Drive...")
                    task.pre_created_folder_id = await drive_folder_task
                
                await self._fast_upload(task, final_dir)
            
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

    async def _ensure_drive_folder(self, task: ChapterTask):
        """Runs concurrently in a thread to create Drive folders without blocking."""
        def sync_create():
            # Assume GDRIVE_ROOT_FOLDER_ID is in your Settings. Adjust if named differently.
            root_id = getattr(Settings, "GDRIVE_ROOT_FOLDER_ID", "root")
            
            # 1. Check/Create Series Folder
            series_id = self.uploader.find_folder(task.series_title, root_id)
            if not series_id:
                series_id = self.uploader.create_folder(task.series_title, root_id)
                
            # 2. Check/Create Chapter Folder
            chapter_id = self.uploader.find_folder(task.title, series_id)
            if not chapter_id:
                chapter_id = self.uploader.create_folder(task.title, series_id)
                
            logger.info(f"☁️ [Parallel Track] Folder '{task.title}' is ready! ID: {chapter_id}")
            return chapter_id
            
        return await asyncio.to_thread(sync_create)

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
            for attempt in range(5):
                try:
                    shutil.rmtree(path)
                    break
                except PermissionError:
                    logger.warning(f"⚠️ Directory locked ({path}), retrying cleanup in 2s... (Attempt {attempt+1})")
                    time.sleep(2)
                except Exception as e:
                    logger.warning(f"Could not clean {path}: {e}")
                    break