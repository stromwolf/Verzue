import asyncio
import logging
from app.models.chapter import ChapterTask, TaskStatus
from app.scrapers.registry import ScraperRegistry
from app.services.gdrive.uploader import GDriveUploader
from .worker import TaskWorker
from app.core.events import EventBus

logger = logging.getLogger("TaskQueue")

class TaskQueue:
    def __init__(self, browser_service=None, gdrive_client=None):
        self.queue: asyncio.Queue[ChapterTask] = asyncio.Queue()
        self.active_tasks_map: dict[str, ChapterTask] = {} # For local deduplication
        
        # We only need the registry and worker IF this instance is running as a Worker.
        # If it's just the Bot Gateway, these won't be heavily utilized.
        if browser_service:
            self.scraper_registry = ScraperRegistry(browser_service)
            self.uploader = GDriveUploader(gdrive_client) if gdrive_client else None
            self.worker = TaskWorker(self.scraper_registry, self.uploader)

    async def add_task(self, task: ChapterTask):
        """Producer: Bot pushes task to local queue."""
        key = f"{task.series_id_key}:{task.episode_id}" # No dedup_prefix needed for local map

        # 1. Check if task is already in progress via local map
        if key in self.active_tasks_map:
            # For now, we return the task status object.
            logger.info(f"🎫 TICKET: Attaching R-ID {task.req_id} to existing task for {task.title}")
            return self.active_tasks_map[key]

        # 2. Mark task as active in local map
        self.active_tasks_map[key] = task
        task.status = TaskStatus.QUEUED
        
        # 3. Push to local queue
        await self.queue.put(task) 
        logger.info(f"📥 Queued (Local): [{task.series_title}] {task.title}")
        
        return task

    # ==========================================
    # WORKER LOGIC (Runs within the Bot process)
    # ==========================================
    async def start_worker(self, num_workers=2):
        """Consumer: Spawns parallel worker loops that pop from the local queue."""
        logger.info(f"👷 Local Task Manager spawning {num_workers} parallel workers...")
        workers = [asyncio.create_task(self._worker_loop(i)) for i in range(num_workers)]
        await asyncio.gather(*workers)

    async def _worker_loop(self, worker_id):
        from app.core.logger import req_id_context
        logger.info(f"   👷 Local Worker {worker_id} online. Waiting for jobs...")
        
        while True:
            # Blocks until a job appears in the local queue
            task = await self.queue.get() # ChapterTask object directly
            
            token = req_id_context.set(task.req_id)
            dedup_key = f"{task.series_id_key}:{task.episode_id}"
            
            try:
                # Tell Discord UI we started
                await EventBus.emit("task_started", {"req_id": task.req_id, "title": task.title})
                
                logger.info(f"⚙️ Worker {worker_id} processing: {task.title}")
                await self.worker.process_task(task)
                
                # Tell Discord UI we finished
                await EventBus.emit("task_completed", {"req_id": task.req_id, "title": task.title})
                logger.info(f"✅ Worker {worker_id} finished: {task.title}")
                
            except Exception as e:
                logger.error(f"❌ Worker {worker_id} crashed on {task.title}: {e}")
                # EventBus.emit for task_failed is handled by app/tasks/worker.py
                # If task.py fails in process_task, it will emit task_failed.
                # However, if an error happens before process_task, or around it,
                # we should emit it here. For now, rely on worker.py's emit.
                # If worker.py fails to emit, this might be a silent failure.
                # I need to ensure the arguments match handle_task_failure in main.py
                await EventBus.emit("task_failed", task, str(e))
            finally:
                # Remove deduplication key so it can be requested again later
                if dedup_key in self.active_tasks_map:
                    del self.active_tasks_map[dedup_key]
                self.queue.task_done() # Mark task as done
                req_id_context.reset(token)
