import asyncio
import logging
import time
from app.core.events import EventBus
from app.providers.manager import ProviderManager
from app.services.session_service import SessionService

logger = logging.getLogger("BatchUnlocker")

class BatchUnlocker:
    def __init__(self):
        self.browser = None
        self.queue = [] 
        self.queue_lock = asyncio.Lock()
        self.notifier = asyncio.Condition(self.queue_lock)
        self.workers = []
        self._started = False
        self.provider_manager = ProviderManager()
        self.session_service = SessionService()
        
        # We can handle more concurrent unlocking tasks now.
        self.worker_stats = {i: {"service": None, "progress": 0, "purchase_status": "Idle", "task": None, "view": None, "busy": False} for i in range(5)}

    def start_workers(self):
        if self._started: return
        self._started = True
        
        for i in range(5):
            self.workers.append(asyncio.create_task(self._worker_loop(i)))
        logger.info("🚀 BatchUnlocker started with 5 dynamic workers.")

    def _get_service(self, url: str) -> str:
        url_lower = url.lower()
        if "kakao.com" in url_lower: return "kakao"
        if "mechacomic.jp" in url_lower: return "mecha"
        if "jumptoon.com" in url_lower: return "jumptoon"
        if "piccoma.com" in url_lower: return "piccoma"
        return "other"

    async def unlock_batch(self, tasks: list, view_ref=None):
        if not tasks: return []
        self.start_workers()
        
        # 🟢 S-GRADE: Selective Warmup.
        # No platforms strictly require Playwright now for unlocking.
        requires_browser = False 
        if requires_browser:
            pass

        futures = []
        async with self.queue_lock:
            for task in tasks:
                fut = asyncio.get_running_loop().create_future()
                service = self._get_service(task.url)
                self.queue.append((task, service, fut, view_ref))
                futures.append(fut)
            self.notifier.notify_all()
        
        logger.info(f"📥 Dispatcher: Queued {len(tasks)} chapters. {len(self.workers)} workers notified.")
        results = await asyncio.gather(*futures, return_exceptions=True)
        
        failed = []
        for task, res in zip(tasks, results):
            if isinstance(res, Exception):
                failed.append(task)
        return failed

    async def _worker_loop(self, context_id):
        logger.info(f"👷 Unlocking Context {context_id} online and awaiting tasks.")
        while True:
            async with self.queue_lock:
                while not self.queue:
                    self.worker_stats[context_id] = {"service": None, "progress": 0, "purchase_status": "Idle", "task": None, "view": None, "busy": False}
                    await self.notifier.wait()
                
                task, service, fut, view = self.queue.pop(0)

            self.worker_stats[context_id] = {
                "service": service, 
                "progress": 0, 
                "task": task, 
                "view": view, 
                "busy": True,
                "purchase_status": "Starting"
            }
            
            try:
                await self._process_task(context_id, task, view)
                fut.set_result(True)
            except Exception as e:
                logger.error(f"Worker {context_id} failed: {e}")
                fut.set_exception(e)
            finally:
                task.purchase_progress = 100
                task.purchase_status = "Done"
                if view: view.trigger_refresh()


    async def _process_task(self, context_id, task, view):
        def update_progress(p, s):
            task.purchase_progress = p
            task.purchase_status = s
            self.worker_stats[context_id]["progress"] = p
            self.worker_stats[context_id]["purchase_status"] = s
            if view: view.trigger_refresh()
            if p == 90: asyncio.create_task(EventBus.emit("purchase_near_completion", context_id, task))

        service = self.worker_stats[context_id]["service"]

        if service == "mecha":
            update_progress(15, "API Fast-Path Attempt")
            try:
                provider = self.provider_manager.get_provider("mecha")
                success = await provider.fast_purchase(task)
                
                if success:
                    update_progress(90, "API Purchase Successful")
                    return
                
                raise Exception("Mecha API fast-purchase failed. Session might be expired or chapter requires purchase.")
                    
            except Exception as e:
                logger.error(f"Worker {context_id} Mecha task failed: {e}")
                raise e
        elif service == "piccoma":
            update_progress(15, "API Coin Purchase Attempt")
            try:
                provider = self.provider_manager.get_provider("piccoma")
                success = await provider.fast_purchase(task)
                
                if success:
                    update_progress(90, "Coin Purchase Successful")
                    return
                
                raise Exception("Piccoma coin purchase failed via API")
                    
            except Exception as e:
                logger.error(f"Worker {context_id} Piccoma purchase failed: {e}")
                raise e
        elif service == "jumptoon":
            update_progress(15, "API Ticket Unlock Attempt")
            try:
                provider = self.provider_manager.get_provider("jumptoon")
                success = await provider.fast_purchase(task)
                
                if success:
                    update_progress(90, "Ticket Unlock Successful")
                    return
                
                raise Exception("Jumptoon ticket unlock failed via API")
                    
            except Exception as e:
                logger.error(f"Worker {context_id} Jumptoon unlock failed: {e}")
                raise e
        else:
            raise Exception(f"Service {service} not supported in BatchUnlocker yet.")