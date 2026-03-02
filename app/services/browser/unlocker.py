import asyncio
import logging
import time
from selenium.webdriver.common.by import By
from .utils import BrowserUtils
from app.core.events import EventBus

logger = logging.getLogger("BatchUnlocker")

class BatchUnlocker:
    def __init__(self, browser_service):
        self.browser = browser_service
        self.queue = [] 
        self.queue_lock = asyncio.Lock()
        # Removed driver_lock as BrowserService is now thread-safe
        self.notifier = asyncio.Condition(self.queue_lock)
        self.workers = []
        self._started = False
        
        self.worker_stats = {i: {"service": None, "progress": 0, "purchase_status": "Idle", "task": None, "view": None, "busy": False} for i in range(3)}

    def start_workers(self):
        if self._started: return
        self._started = True
        
        for i in range(3):
            self.workers.append(asyncio.create_task(self._worker_loop(i)))
        logger.info("🚀 BatchUnlocker started with 3 dynamic workers.")

    def _get_service(self, url: str) -> str:
        url_lower = url.lower()
        if "kakao.com" in url_lower: return "kakao"
        if "mechacomic.jp" in url_lower: return "mecha"
        if "jumptoon.com" in url_lower: return "jumptoon"
        return "other"

    async def unlock_batch(self, tasks: list, view_ref=None):
        if not tasks: return []
        self.start_workers()

        if not self.browser.driver:
            # We call warmup/start directly. BrowserService lock handles safety.
            await asyncio.to_thread(self.browser.start)
            await asyncio.to_thread(self.browser.enable_mobile, True)

        futures = []
        async with self.queue_lock:
            for task in tasks:
                fut = asyncio.get_running_loop().create_future()
                service = self._get_service(task.url)
                self.queue.append((task, service, fut, view_ref))
                futures.append(fut)
            self.notifier.notify_all()
        
        logger.info(f"📥 Dispatcher: Queued {len(tasks)} chapters. Preferred workers notified.")
        results = await asyncio.gather(*futures, return_exceptions=True)
        
        failed = []
        for task, res in zip(tasks, results):
            if isinstance(res, Exception):
                failed.append(task)
        return failed

    async def _worker_loop(self, tab_index):
        logger.info(f"👷 Tab Worker {tab_index} online and awaiting tasks.")
        while True:
            async with self.queue_lock:
                while not self.queue:
                    self.worker_stats[tab_index] = {"service": None, "progress": 0, "purchase_status": "Idle", "task": None, "view": None, "busy": False}
                    await self.notifier.wait()
                
                task, service, fut, view = self.queue.pop(0)

            self.worker_stats[tab_index] = {
                "service": service, 
                "progress": 0, 
                "task": task, 
                "view": view, 
                "busy": True,
                "purchase_status": "Starting"
            }
            
            try:
                await self._process_task(tab_index, task, view)
                fut.set_result(True)
            except Exception as e:
                logger.error(f"Worker {tab_index} failed: {e}")
                fut.set_exception(e)
            finally:
                task.purchase_progress = 100
                task.purchase_status = "Done"
                if view: view.trigger_refresh()

    def _update_disk_cookies(self, platform, cookies):
        import json
        from config.settings import Settings
        path = Settings.SECRETS_DIR / platform / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(cookies, f, indent=4)

    async def _process_task(self, tab_index, task, view):
        def update_progress(p, s):
            task.purchase_progress = p
            task.purchase_status = s
            self.worker_stats[tab_index]["progress"] = p
            self.worker_stats[tab_index]["purchase_status"] = s
            if view: view.trigger_refresh()
            if p == 90: asyncio.create_task(EventBus.emit("purchase_near_completion", tab_index, task))

        service = self.worker_stats[tab_index]["service"]

        # 🚀 1. THE FAST PATH ONLY
        if service == "mecha":
            update_progress(15, "API Fast-Path Attempt")
            try:
                from app.scrapers.mecha.api import MechaApiScraper
                scraper = MechaApiScraper()
                success, new_cookies = await asyncio.to_thread(scraper.fast_purchase, task)
                
                if success:
                    update_progress(90, "API Purchase Successful")
                    if new_cookies: await asyncio.to_thread(self._update_disk_cookies, "mecha", new_cookies)
                    await asyncio.sleep(1)
                    return
                else:
                    raise Exception("API Purchase rejected. Selenium fallback is currently disabled.")
            except Exception as e:
                logger.error(f"API Fast Path failed: {e}")
                raise e
        else:
            raise Exception("Selenium fallback disabled. Only Mecha API supported currently.")