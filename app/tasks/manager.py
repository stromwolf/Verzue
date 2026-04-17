import asyncio
import logging
import psutil
import os
from collections import deque
from app.models.chapter import ChapterTask, TaskStatus
from app.providers.manager import ProviderManager
from app.services.gdrive.uploader import GDriveUploader
from .worker import TaskWorker
from app.core.events import EventBus
logger = logging.getLogger("TaskQueue")

class TaskQueue:
    def __init__(self, gdrive_client=None):
        # ==========================================
        # 🃏 THE DEALER (Round-Robin State)
        # ==========================================
        from config.settings import Settings
        self.total_tasks = 0
        self.busy_workers = 0 # 👷 Current active processing workers
        self.is_draining = False # 🚦 Maintenance lock for reboots
        self.task_condition = asyncio.Condition() # Thread-safe waker for workers
        
        from app.services.redis_manager import RedisManager
        self.redis = RedisManager()

        # ==========================================
        # ⚖️ THE PIT BOSS (RAM Auto-Scaler)
        # ==========================================
        self.min_workers = 1        # Never drop below this
        self.active_worker_count = 0
        self.workers_to_kill = 0    # Hit-list for when RAM is critical

        # VPS / Browserless Mode Initialization (Playwright Purged)
        self.browser_service = None
        self.provider_manager = ProviderManager()
        self.uploader = GDriveUploader(gdrive_client) if gdrive_client else None
        self.worker = TaskWorker(self.provider_manager, self.uploader)
        
        # 🟢 S-GRADE: Initialize the BatchUnlocker (Now API-Driven Only)
        from app.services.browser.unlocker import BatchUnlocker
        self.unlocker = BatchUnlocker()

    async def add_task(self, task: ChapterTask):
        """Producer: Bot pushes task to the Redis global queue."""
        if self.is_draining:
            raise RuntimeError("Bot is currently preparing for maintenance/restart. Please try again in 1-2 minutes.")

        key = f"{task.series_id_key}:{task.episode_id}"

        # 🟢 S-GRADE: Cross-Process Deduplication via Redis
        active_id = await self.redis.get_active_task(key)
        if active_id:
            logger.info(f"🎫 TICKET: Task {key} is already active/queued. Attaching R-ID {task.req_id}.")
            # Register this user as a waiter in Redis
            await self.redis.register_waiter(key, {
                "req_id": task.req_id, 
                "channel_id": task.channel_id, 
                "user_id": task.requester_id
            })
            return task

        task.status = TaskStatus.QUEUED
        
        # --- Push to Redis Global Queue ---
        await self.redis.set_active_task(key, str(task.id))
        await self.redis.push_task(task.to_dict())
        
        async with self.task_condition:
            self.total_tasks += 1
            self.task_condition.notify() # Wake up an idle local worker loop
        
        logger.info(f"📥 Queued (Redis Global): [{task.series_title}] {task.title} | User: {task.req_id}")
        return task

    async def _get_next_task(self) -> ChapterTask:
        """The Dealer: Grabs one task from Redis global queue."""
        while True:
            try:
                # 1. Check if we have tasks in Redis
                task_data = await self.redis.pop_task(timeout=5)
                
                if task_data:
                    # Convert back to object
                    task = ChapterTask.from_dict(task_data)
                    async with self.task_condition:
                        if self.total_tasks > 0: self.total_tasks -= 1
                    return task
                
                # Check for hibernation/redis drop (RedisManager returns None on ConnectionError)
                if not self.redis._is_connected:
                    logger.warning("😴 [TaskQueue] Redis is offline. Worker Hibernating...")
                    await asyncio.sleep(5) # Longer wait during outage
                    continue

                # 2. If no tasks, check if we should shut down
                if self.workers_to_kill > 0: return None
                
                # 3. Brief wait if queue was empty
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"⚠️ [TaskQueue] Error in retrieval loop: {e}")
                await asyncio.sleep(2)

    # ==========================================
    # WORKER & PIT BOSS LOGIC
    # ==========================================
    async def start_worker(self, num_workers=2): # Name fixed to match app/bot/main.py
        logger.info("🎰 Casino Doors Open: RAM Pit Boss & Dealer online.")
        asyncio.create_task(self._pit_boss_loop())

    async def _pit_boss_loop(self):
        """Monitors RAM and scales workers dynamically based on real telemetry."""
        worker_id_counter = 0
        
        # 🟢 TELEMETRY-BASED CONFIGURATION
        RAM_PER_WORKER_MB = 200     # Based on the ~181MB spike per chapter
        OS_SAFETY_BUFFER_MB = 700   # 🟢 UPDATED: Keep 700MB free for the OS and Discord
        ABSOLUTE_MAX_WORKERS = 15   # CPU bottleneck ceiling (prevents CPU thrashing)
        
        # 1. Spawn Initial Minimum Workers
        for _ in range(self.min_workers):
            asyncio.create_task(self._worker_loop(worker_id_counter))
            worker_id_counter += 1

        # 2. Infinite Monitoring Loop
        while True:
            await asyncio.sleep(10) # Check RAM every 10 seconds
            mem = psutil.virtual_memory()
            avail_mb = mem.available / (1024 * 1024)
            
            # 🟢 THE DYNAMIC FORMULA
            # Example: (8000MB free - 1024MB buffer) / 200MB = 34 affordable workers
            affordable_workers = int((avail_mb - OS_SAFETY_BUFFER_MB) / RAM_PER_WORKER_MB)
            target_workers = max(self.min_workers, min(affordable_workers, ABSOLUTE_MAX_WORKERS))
            
            # Do we need more workers? (We have RAM + Idle tasks waiting)
            if self.active_worker_count < target_workers and self.total_tasks > self.active_worker_count:
                logger.debug(f"📈 [Pit Boss] RAM: {avail_mb:.0f}MB. Target: {target_workers} workers. Spawning Worker {worker_id_counter}.")
                asyncio.create_task(self._worker_loop(worker_id_counter))
                worker_id_counter += 1
            
            # Are we choking on RAM? (Current workers exceed what we can currently afford)
            elif self.active_worker_count > target_workers:
                if self.active_worker_count - self.workers_to_kill > self.min_workers:
                    logger.warning(f"📉 [Pit Boss] RAM Dropping ({avail_mb:.0f}MB)! Tapping a worker to cash out.")
                    self.workers_to_kill += 1
                    
                    # Wake up a sleeping worker just in case they need to die
                    async with self.task_condition:
                        self.task_condition.notify()

    async def _worker_loop(self, worker_id):
        from app.core.logger import req_id_context
        self.active_worker_count += 1
        logger.debug(f"   👷 Worker {worker_id} joined the table.")
        
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
                    # Note: Workers_to_kill decrement already handled in _get_next_task or _worker_loop 141
                    logger.warning(f"👋 Worker {worker_id} cashing out from idle state to free up RAM.")
                    break
                
                token = req_id_context.set(task.req_id)
                dedup_key = f"{task.series_id_key}:{task.episode_id}"
                
                try:
                    self.busy_workers += 1
                    await EventBus.emit("task_started", {"req_id": task.req_id, "title": task.title})
                    
                    # Process the task
                    await self.worker.process_task(task)
                    
                    await EventBus.emit("task_completed", {"req_id": task.req_id, "title": task.title})
                    
                except Exception as e:
                    logger.error(f"❌ Worker {worker_id} crashed on {task.title}: {e}")
                    await EventBus.emit("task_failed", task, str(e))
                finally:
                    self.busy_workers -= 1
                    await self.redis.remove_active_task(dedup_key)
                    req_id_context.reset(token)
                    
        finally:
            self.active_worker_count -= 1