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
        self.queue = [] # List of (task, service_name, future, view_ref)
        self.queue_lock = asyncio.Lock()
        self.driver_lock = asyncio.Lock()
        self.notifier = asyncio.Condition(self.queue_lock)
        self.workers = []
        self._started = False
        
        # 📊 HEURISTIC MONITOR: Track worker readiness (0=Ready, 90=Near-Free, 100=Busy)
        self.worker_stats = {i: {"service": None, "progress": 0, "purchase_status": "Idle", "task": None, "view": None, "busy": False} for i in range(3)}

    def start_workers(self):
        if self._started: return
        self._started = True
        services = ["kakao", "mecha", "jumptoon"]
        for i, service in enumerate(services):
            self.workers.append(asyncio.create_task(self._worker_loop(i, service)))
        logger.info(f"🚀 BatchUnlocker started with {len(services)} workers.")

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
            async with self.driver_lock:
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

    async def _worker_loop(self, tab_index, preferred_service):
        logger.info(f"👷 Tab Worker {tab_index} ({preferred_service}) online.")
        while True:
            async with self.queue_lock:
                while not self.queue:
                    self.worker_stats[tab_index] = {"service": None, "progress": 0, "task": None, "view": None, "busy": False}
                    await self.notifier.wait()
                
                # HEURISTIC SCHEDULING (PRE-COMPLETION SIGNAL AWARE)
                # We pick the oldest task from the queue for our tab or for work-stealing
                task_idx = -1
                for i, (task, service, fut, view) in enumerate(self.queue):
                    if service == preferred_service:
                        task_idx = i
                        break
                
                if task_idx == -1: task_idx = 0
                
                task, service, fut, view = self.queue.pop(task_idx)

            # 3. START WORK
            self.worker_stats[tab_index] = {
                "service": service, 
                "progress": 0, 
                "task": task, 
                "view": view,
                "busy": True
            }
            
            try:
                await self._process_task(tab_index, task, view)
                fut.set_result(True)
            except Exception as e:
                fut.set_exception(e)
            finally:
                task.purchase_progress = 100
                task.purchase_status = "Done"
                if view: view.trigger_refresh()

    async def _process_task(self, tab_index, task, view):
        def update_progress(p, s):
            task.purchase_progress = p
            task.purchase_status = s
            self.worker_stats[tab_index]["progress"] = p
            self.worker_stats[tab_index]["purchase_status"] = s
            if view: view.trigger_refresh()

            # 🚨 THE SIGNAL: At 90%, we broadcast that the tab is effectively free.
            if p == 90:
                asyncio.create_task(EventBus.emit("purchase_near_completion", tab_index, task))

        # A. NAVIGATION (10-30%)
        update_progress(10, "Navigating")
        async with self.driver_lock:
            await asyncio.to_thread(self.browser.switch_to_tab, tab_index)
            self.browser.driver.execute_script(f"window.location.href = '{task.url}';")
        
        update_progress(30, "Waiting for Page")
        
        # B. POLLING & ACTION (30-90%)
        start_time = time.time()
        timeout = 45
        is_success = False

        while time.time() - start_time < timeout:
            await asyncio.sleep(1.5)
            
            async with self.driver_lock:
                await asyncio.to_thread(self.browser.switch_to_tab, tab_index)
                
                current_url = self.browser.driver.current_url.lower()
                if "login" in current_url or "limited" in current_url:
                    update_progress(40, "Injecting Cookies")
                    await asyncio.to_thread(BrowserUtils.load_cookies, self.browser.driver)
                    self.browser.driver.execute_script(f"window.location.href = '{task.url}';")
                    continue
                
                update_progress(60, "Searching for Buttons")
                # Removed Jumptoon selectors as no buttons appear for that service.
                selectors = ["input.js-bt_buy_and_download", "input.c-btn-read-end", 
                            "a.c-btn-read-end", "input.c-btn-free", "input.c-btn-buy", 
                            "button.btn-purchase"]
                
                for sel in selectors:
                    btns = self.browser.driver.find_elements(By.CSS_SELECTOR, sel)
                    if btns and btns[0].is_displayed():
                        update_progress(80, "Clicking Purchase")
                        self.browser.driver.execute_script("arguments[0].click();", btns[0])
                        is_success = True
                        break
                
                # 🟢 JUMPTOON PASSIVE UNLOCK (V5)
                # For Jumptoon, navigation alone is often the unlock.
                # No buttons to click. We verify by checking for '無料' in source or viewer URL.
                if not is_success and "jumptoon.com" in current_url:
                    page_source = self.browser.driver.page_source
                    if "無料" in page_source or "view" in current_url or "viewer" in current_url:
                        update_progress(90, "Verifying Purchase")
                        is_success = True

                if is_success or "view" in current_url or "viewer" in current_url:
                    # 🎯 90% MARK: THE CLICK IS DONE (OR ALREADY IN VIEWER)
                    is_success = True
                    update_progress(90, "Verifying Purchase")
                    
                    # 💾 SAVE UPDATED SESSION COOKIES
                    await asyncio.to_thread(BrowserUtils.save_cookies, self.browser.driver)
                    
                    # During this 2s wait, the tab's next job is already being prepared by the Manager!
                    await asyncio.sleep(2)
                    break

        if not is_success:
            update_progress(0, "Timeout/Failed")
            raise Exception("Purchase Timeout")

        update_progress(100, "Purchased")
