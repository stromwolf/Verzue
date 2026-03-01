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

            if p == 90:
                asyncio.create_task(EventBus.emit("purchase_near_completion", tab_index, task))

        service = self.worker_stats[tab_index]["service"]

        # 🚀 1. THE FAST PATH (API PURCHASING)
        if service == "mecha":
            update_progress(15, "API Fast-Path Attempt")
            try:
                from app.scrapers.mecha.api import MechaApiScraper
                scraper = MechaApiScraper()
                # Run the synchronous API call in a thread to keep the async loop free
                success, new_cookies = await asyncio.to_thread(scraper.fast_purchase, task)
                
                if success:
                    update_progress(90, "API Purchase Successful")
                    if new_cookies:
                        await asyncio.to_thread(self._update_disk_cookies, "mecha", new_cookies)
                    await asyncio.sleep(1) # Let the UI catch up
                    return # Exit! We bought it instantly, no Selenium required.
            except Exception as e:
                logger.warning(f"API Fast Path failed: {e}. Falling back to Browser.")

        # 🐢 2. THE SLOW PATH (SELENIUM FALLBACK)
        # A. NAVIGATION (10-30%)
        update_progress(10, "Navigating Browser")
        
        # Atomic Navigation via BrowserService
        await asyncio.to_thread(self.browser.run_on_tab, tab_index, 
             lambda d: d.execute_script(f"window.location.href = '{task.url}';"))
        
        update_progress(30, "Waiting for Page")
        
        # B. POLLING & ACTION (30-90%)
        start_time = time.time()
        timeout = 45
        is_success = False

        while time.time() - start_time < timeout:
            await asyncio.sleep(1.5)
            
            # Define interaction logic to run on the browser thread
            def check_and_interact(driver):
                current_url = driver.current_url.lower()
                
                # 1. Cookie Injection for Login/Limited pages
                if "login" in current_url or "limited" in current_url:
                    BrowserUtils.load_cookies(driver)
                    driver.execute_script(f"window.location.href = '{task.url}';")
                    return "reload"
                
                # 2. Search for Purchase Buttons
                selectors = ["input.js-bt_buy_and_download", "input.c-btn-read-end", 
                             "a.c-btn-read-end", "input.c-btn-free", "input.c-btn-buy", 
                             "button.btn-purchase"]
                
                for sel in selectors:
                    btns = driver.find_elements(By.CSS_SELECTOR, sel)
                    if btns and btns[0].is_displayed():
                        driver.execute_script("arguments[0].click();", btns[0])
                        return "clicked"
                
                # 3. Passive Unlock Verification (Jumptoon)
                if "jumptoon.com" in current_url:
                     page_source = driver.page_source
                     if "無料" in page_source or "view" in current_url or "viewer" in current_url:
                         return "success"

                # 4. Standard Success
                if "view" in current_url or "viewer" in current_url:
                    return "success"
                
                return "waiting"

            # Execute interaction atomically on the correct tab
            result = await asyncio.to_thread(self.browser.run_on_tab, tab_index, check_and_interact)

            if result == "reload":
                update_progress(40, "Injecting Cookies")
            elif result == "clicked":
                update_progress(80, "Clicking Purchase")
                is_success = True
                break
            elif result == "success":
                is_success = True
                break
            else:
                update_progress(60, "Searching for Buttons")

        if is_success:
            update_progress(90, "Verifying Purchase")
            await asyncio.to_thread(self.browser.run_on_tab, tab_index, lambda d: BrowserUtils.save_cookies(d))
            await asyncio.sleep(2)
        else:
            update_progress(0, "Timeout/Failed")
            raise Exception("Purchase Timeout")

        update_progress(100, "Purchased")