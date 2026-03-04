import asyncio
import logging
import psutil
import os
from collections import deque
from app.models.chapter import ChapterTask, TaskStatus
from app.scrapers.registry import ScraperRegistry
from app.services.gdrive.uploader import GDriveUploader
from .worker import TaskWorker
from app.core.events import EventBus

logger = logging.getLogger("TaskQueue")

class TaskQueue:
    def __init__(self, browser_service=None, gdrive_client=None):
        # ==========================================
        # 🃏 THE DEALER (Round-Robin State)
        # ==========================================
        self.queues: dict[str, deque] = {}  # Maps req_id to a deque of tasks
        self.req_order: list[str] = []      # The "circle" of active users
        self.req_index: int = 0             # Whose turn it is
        self.total_tasks: int = 0
        self.task_condition = asyncio.Condition() # Thread-safe waker for workers
        
        self.active_tasks_map: dict[str, ChapterTask] = {} # For local deduplication

        # ==========================================
        # ⚖️ THE PIT BOSS (RAM Auto-Scaler)
        # ==========================================
        self.max_workers = 6        # Max parallel threads (Adjust based on CPU limits)
        self.min_workers = 1        # Never drop below this
        self.active_worker_count = 0
        self.workers_to_kill = 0    # Hit-list for when RAM is critical
        
        self.ram_threshold_high_mb = 2048  # If free RAM > 2GB, spawn more workers!
        self.ram_threshold_low_mb = 500    # If free RAM < 500MB, start killing workers!

        if browser_service:
            self.scraper_registry = ScraperRegistry(browser_service)
            self.uploader = GDriveUploader(gdrive_client) if gdrive_client else None
            self.worker = TaskWorker(self.scraper_registry, self.uploader)

    async def add_task(self, task: ChapterTask):
        """Producer: Bot pushes task to the Round-Robin dealer."""
        key = f"{task.series_id_key}:{task.episode_id}"

        if key in self.active_tasks_map:
            logger.info(f"🎫 TICKET: Attaching R-ID {task.req_id} to existing task for {task.title}")
            return self.active_tasks_map[key]

        self.active_tasks_map[key] = task
        task.status = TaskStatus.QUEUED
        
        # --- Deal the card to the user's specific deck ---
        async with self.task_condition:
            if task.req_id not in self.queues:
                self.queues[task.req_id] = deque()
                self.req_order.append(task.req_id)
            
            self.queues[task.req_id].append(task)
            self.total_tasks += 1
            self.task_condition.notify() # Wake up an idle worker!
        
        logger.info(f"📥 Queued (Round-Robin): [{task.series_title}] {task.title} | User: {task.req_id}")
        return task

    async def _get_next_task(self) -> ChapterTask:
        """The Dealer: Grabs one task from the next active user in the circle."""
        async with self.task_condition:
            while self.total_tasks == 0:
                # If we were woken up just to be killed, return None to trigger exit
                if self.workers_to_kill > 0: return None
                
                await self.task_condition.wait() # Sleep until a task arrives
                
                if self.workers_to_kill > 0: return None 

            # Loop through the decks fairly
            while True:
                if self.req_index >= len(self.req_order):
                    self.req_index = 0

                req_id = self.req_order[self.req_index]
                q = self.queues[req_id]

                if len(q) > 0:
                    task = q.popleft()
                    self.total_tasks -= 1
                    
                    # Clean up empty decks immediately so we don't deal to ghosts
                    if len(q) == 0:
                        del self.queues[req_id]
                        self.req_order.pop(self.req_index)
                        # Do not increment req_index, as next element shifted left
                    else:
                        self.req_index += 1
                        
                    return task
                else:
                    self.req_index += 1

    # ==========================================
    # WORKER & PIT BOSS LOGIC
    # ==========================================
    async def start_worker(self, num_workers=2): # Name fixed to match app/bot/main.py
        logger.info("🎰 Casino Doors Open: RAM Pit Boss & Dealer online.")
        asyncio.create_task(self._pit_boss_loop())

    async def _pit_boss_loop(self):
        """Monitors RAM and scales workers dynamically."""
        worker_id_counter = 0
        
        # 1. Spawn Initial Minimum Workers
        for _ in range(self.min_workers):
            asyncio.create_task(self._worker_loop(worker_id_counter))
            worker_id_counter += 1

        # 2. Infinite Monitoring Loop
        while True:
            await asyncio.sleep(10) # Check RAM every 10 seconds
            mem = psutil.virtual_memory()
            avail_mb = mem.available / (1024 * 1024)
            
            # Do we need more workers? (Lots of RAM + Idle tasks waiting)
            if avail_mb > self.ram_threshold_high_mb and self.total_tasks > self.active_worker_count:
                if self.active_worker_count < self.max_workers:
                    logger.info(f"📈 [Pit Boss] RAM Healthy ({avail_mb:.0f}MB). Table is busy. Spawning Worker {worker_id_counter}.")
                    asyncio.create_task(self._worker_loop(worker_id_counter))
                    worker_id_counter += 1
            
            # Are we choking on RAM?
            elif avail_mb < self.ram_threshold_low_mb:
                if self.active_worker_count - self.workers_to_kill > self.min_workers:
                    logger.warning(f"📉 [Pit Boss] RAM CRITICAL ({avail_mb:.0f}MB)! Tapping a worker to cash out.")
                    self.workers_to_kill += 1
                    
                    # Wake up a sleeping worker just in case they need to die
                    async with self.task_condition:
                        self.task_condition.notify()

    async def _worker_loop(self, worker_id):
        from app.core.logger import req_id_context
        self.active_worker_count += 1
        logger.info(f"   👷 Worker {worker_id} joined the table.")
        
        try:
            while True:
                # 1. Did the Pit Boss tap us on the shoulder?
                if self.workers_to_kill > 0:
                    self.workers_to_kill -= 1
                    logger.warning(f"👋 Worker {worker_id} cashing out to free up RAM.")
                    break # Exit the loop and kill this thread entirely
                    
                # 2. Wait for the Dealer to hand us a card (task)
                task = await self._get_next_task()
                
                # If task is None, it means the Pit Boss woke us up specifically to die
                if task is None:
                    self.workers_to_kill -= 1
                    logger.warning(f"👋 Worker {worker_id} cashing out from idle state to free up RAM.")
                    break
                
                token = req_id_context.set(task.req_id)
                dedup_key = f"{task.series_id_key}:{task.episode_id}"
                
                try:
                    await EventBus.emit("task_started", {"req_id": task.req_id, "title": task.title})
                    
                    # 🟢 TELEMETRY START: Snapshot memory before the task
                    process = psutil.Process(os.getpid())
                    mem_before = process.memory_info().rss / (1024 * 1024)
                    
                    await self.worker.process_task(task)
                    
                    # 🟢 TELEMETRY END: Snapshot memory after the task
                    mem_after = process.memory_info().rss / (1024 * 1024)
                    ram_used = max(0, mem_after - mem_before) # Prevent negative if GC runs
                    
                    logger.info(f"📊 [Telemetry] Worker {worker_id} used ~{ram_used:.2f} MB for '{task.title}'")
                    
                    await EventBus.emit("task_completed", {"req_id": task.req_id, "title": task.title})
                    
                except Exception as e:
                    logger.error(f"❌ Worker {worker_id} crashed on {task.title}: {e}")
                    await EventBus.emit("task_failed", task, str(e))
                finally:
                    if dedup_key in self.active_tasks_map:
                        del self.active_tasks_map[dedup_key]
                    req_id_context.reset(token)
                    
        finally:
            self.active_worker_count -= 1