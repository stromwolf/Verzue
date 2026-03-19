import os, shutil, asyncio, logging, time, re
from pathlib import Path
from functools import partial
from concurrent.futures import ProcessPoolExecutor

from config.settings import Settings
from app.models.chapter import ChapterTask, TaskStatus
from app.services.image.stitcher import ImageStitcher
from app.core.events import EventBus
from app.providers.manager import ProviderManager

logger = logging.getLogger("TaskWorker")

# Create a global Process Pool for CPU-bound stitching. 
# 🟢 Increased to 4 to allow multiple chapters to stitch in parallel.
PROCESS_POOL = ProcessPoolExecutor(max_workers=4)

# 🟢 THE SEAMPHORE: Limits concurrent STITCHING operations to prevent RAM/CPU thrashing.
# Even if we have 15 workers downloading, only 3 will stitch at one time.
STITCH_SEMAPHORE = asyncio.Semaphore(3)

class TaskWorker:
    def __init__(self, provider_manager, uploader):
        self.provider_manager = provider_manager
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

        await self._clean_dirs(raw_dir, final_dir)
        raw_dir.mkdir(parents=True, exist_ok=True); final_dir.mkdir(parents=True, exist_ok=True)

        try:
            task.status = TaskStatus.DOWNLOADING
            provider = self.provider_manager.get_provider(task.service)
            if not provider:
                 raise Exception(f"No provider found for service: {task.service}")
            
            logger.info(f"🔍 STAGE 1/3: Provider: {provider.__class__.__name__}")
            
            # --- LOCAL DOWNLOAD TRACK ---
            # All providers are now async
            await provider.scrape_chapter(task, str(raw_dir))
            
            valid_imgs = [f for f in os.listdir(raw_dir) if f.lower().endswith(('.png', '.webp', '.jpg', '.jpeg'))]
            if not valid_imgs: raise Exception("No images found after scrape.")
            logger.info(f"✅ STAGE 1 COMPLETE: {len(valid_imgs)} images.")

            # --- STAGE 2: STITCHING (Semaphore-Controlled CPU Offloading) ---
            task.status = TaskStatus.STITCHING
            
            # Fetch early share link as soon as folder is ready (before stitching completes)
            if drive_folder_task and not task.share_link:
                try:
                    task.pre_created_folder_id = await drive_folder_task
                    task.share_link = await asyncio.to_thread(self.uploader.get_share_link, task.pre_created_folder_id)
                    logger.info(f"🔗 Early Link Generated: {task.share_link}")
                except Exception as e:
                    logger.warning(f"Failed to fetch early link: {e}")

            logger.info("🧵 STAGE 2/3: Stitching (Waiting for CPU Slot)...")
            
            seed_string = None
            if "jumptoon.com" in task.url.lower():
                # 🟢 UNSCRAMBLE AT DOWNLOAD: Jumptoon is now unscrambled in api.py
                # immediately after download. We pass None to the stitcher to avoid
                # double-processing or errors.
                seed_string = None
            elif "webtoon.kakao.com" in task.url.lower():
                seed_string = task.series_id_key

            loop = asyncio.get_running_loop()
            stitch_func = partial(
                ImageStitcher.stitch_folder, 
                str(raw_dir), 
                str(final_dir), 
                12000, 
                episode_id=seed_string,
                req_id=task.req_id,
                service_name=task.service
            )

            # 🟢 Use Semaphore to prevent too many dense CPU tasks from running at once
            async with STITCH_SEMAPHORE:
                logger.info("⚡ Slot Acquired! Stitching now...")
                await loop.run_in_executor(PROCESS_POOL, stitch_func)
            
            # --- STAGE 3: UPLOADING (Decoupled & Backgrounded) ---
            task.status = TaskStatus.UPLOADING
            if self.uploader:
                if drive_folder_task and not task.pre_created_folder_id:
                    logger.info("⏳ Finalizing Drive folder creation...")
                    task.pre_created_folder_id = await drive_folder_task
                
                # 🚀 FIRE AND FORGET: Move upload and cleanup to a background task
                # This frees up the worker IMMEDIATELY to start the next chapter.
                asyncio.create_task(self._background_upload_and_cleanup(task, final_dir, raw_dir))
            
            elapsed = time.time() - start_time
            logger.info(f"🏁 TASK DISPATCHED TO BACKGROUND in {elapsed:.2f}s")
            # We don't set COMPLETED here; the background task will do it.

        except Exception as e:
            task.status = TaskStatus.FAILED
            logger.error(f"❌ TASK FAILURE: {e}")
            await EventBus.emit("task_failed", task, str(e))
            await self._clean_dirs(raw_dir, final_dir)
            raise e

    async def _background_upload_and_cleanup(self, task: ChapterTask, final_dir, raw_dir):
        """Dispatches uploads and handles definitive cleanup without blocking the worker pool."""
        try:
            await self._fast_upload(task, final_dir)
            task.status = TaskStatus.COMPLETED
            logger.info(f"✅ BACKGROUND UPLOAD COMPLETE: [{task.series_title}] - {task.title}")
            await EventBus.emit("task_completed", task)
        except Exception as e:
            task.status = TaskStatus.FAILED
            logger.error(f"❌ BACKGROUND UPLOAD FAILED: {e}")
            await EventBus.emit("task_failed", task, str(e))
        finally:
            await self._clean_dirs(raw_dir, final_dir)

    async def _ensure_drive_folder(self, task: ChapterTask):
        """Runs concurrently in a thread to create Drive folders without blocking."""
        def sync_create():
            # 1. Check/Create the "[Uploading]" folder in MAIN
            folder_name = task.folder_name
            temp_name = f"[Uploading] {folder_name}"
            
            # Use main_folder_id provided by controller (e.g., the 'MAIN' folder ID)
            parent_id = task.main_folder_id or getattr(Settings, "GDRIVE_ROOT_FOLDER_ID", "root")
            
            # Check if it exists
            chapter_id = self.uploader.find_folder(temp_name, parent_id)
            if not chapter_id:
                chapter_id = self.uploader.create_folder(temp_name, parent_id)
            
            # 2. Create the shortcut in all client folders specified
            if task.client_folders:
                for cf in task.client_folders:
                    cf_id = cf.get('id')
                    if cf_id:
                        self.uploader.create_shortcut(chapter_id, cf_id, folder_name)
            elif task.client_folder_id:
                # Fallback for older tasks or direct calls
                self.uploader.create_shortcut(chapter_id, task.client_folder_id, folder_name)
                
            logger.info(f"☁️ [Parallel Track] Folder '{folder_name}' is ready! ID: {chapter_id}")
            return chapter_id
            
        return await asyncio.to_thread(sync_create)

    async def _fast_upload(self, task, local_path):
        # 🟢 UPDATE (09 March 2026): Concurrent uploading with Semaphore to maximize bandwidth.
        files = sorted([f for f in os.listdir(local_path) if f.lower().endswith('.jpg')])
        
        if not files: return

        # 1. Upload the LAST file first (High-Priority/Finale)
        last_file = files[-1]
        logger.info(f"🚀 [Priority] Uploading last page first: {last_file}")
        await asyncio.to_thread(self.uploader.upload_file, os.path.join(local_path, last_file), last_file, task.pre_created_folder_id)
        
        remaining_files = files[:-1]
        if remaining_files:
            # 🟢 SEMAPHORE: Limit GDrive API writes to 5 at a time to stay under quota
            upload_semaphore = asyncio.Semaphore(5)
            
            # 🟢 Standardized Progress Bar Tracking
            completed = 1 # We already uploaded one file (last_file)
            total = len(files)
            from app.core.logger import ProgressBar
            progress = ProgressBar(task.req_id, "Uploading", task.service.capitalize(), total)
            progress.update(completed)

            async def safe_upload(filename):
                nonlocal completed
                async with upload_semaphore:
                    full_path = os.path.join(local_path, filename)
                    await asyncio.to_thread(self.uploader.upload_file, full_path, filename, task.pre_created_folder_id)
                    completed += 1
                    progress.update(completed)

            # Fire off all uploads simultaneously (the semaphore controls the flow)
            await asyncio.gather(*(safe_upload(f) for f in remaining_files))
            progress.finish()
            
        if task.final_folder_name:
            await asyncio.to_thread(self.uploader.rename_file, task.pre_created_folder_id, task.final_folder_name)

    async def _clean_dirs(self, r, f):
        """Robust cleanup with retry to avoid WinError 32."""
        for path in [r, f]:
            if not path.exists(): continue
            for attempt in range(5):
                try:
                    shutil.rmtree(path)
                    break
                except PermissionError:
                    logger.warning(f"⚠️ Directory locked ({path}), retrying cleanup in 2s... (Attempt {attempt+1})")
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.warning(f"Could not clean {path}: {e}")
                    break