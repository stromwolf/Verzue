import logging
import base64
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from fake_useragent import UserAgent
from config.settings import Settings
from .utils import BrowserUtils

logger = logging.getLogger("BrowserService")

class BrowserService:
    def __init__(self, headless: bool = None):
        self.driver = None
        self.default_headless = headless if headless is not None else Settings.HEADLESS
        self.active_sessions = 0 # TRACKS ACTIVE DASHBOARDS
        self.tab_handles = [] # [kakao_tab, mecha_tab, jumptoon_tab]

    def inc_session(self):
        self.active_sessions += 1
        logger.info(f"➕ Browser Session Added ({self.active_sessions} total)")

    def dec_session(self):
        self.active_sessions = max(0, self.active_sessions - 1)
        logger.info(f"➖ Browser Session Removed ({self.active_sessions} total)")
        if self.active_sessions == 0:
            logger.info("🧹 Zero active sessions. Stopping browser...")
            self.stop()

    def start(self, headless: bool = None):
        if self.driver:
            try:
                self.driver.current_url
                return 
            except: self.driver = None

        is_headless = headless if headless is not None else self.default_headless
        opts = Options()
        opts.page_load_strategy = 'eager'
        if is_headless: opts.add_argument("--headless=new")

        try: ua = UserAgent(browsers=['chrome']).random
        except: ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
        opts.add_argument(f"user-agent={ua}")
        opts.add_argument(f"--user-data-dir={Settings.BROWSER_PROFILE_DIR}") # PERSISTENT IDENTITY
        opts.add_argument("--profile-directory=Default") # Use the default profile
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        
        if Settings.BINARY_LOCATION: opts.binary_location = Settings.BINARY_LOCATION
        service = ChromeService(executable_path=Settings.DRIVER_LOCATION) if Settings.DRIVER_LOCATION else ChromeService()

        try:
            self.driver = webdriver.Chrome(service=service, options=opts)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            self.driver.set_window_size(1920, 1080)
            
            # INITIALIZE 3 TABS (Sequential tracking)
            self.tab_handles = [self.driver.current_window_handle]
            for _ in range(2):
                old_handles = set(self.driver.window_handles)
                self.driver.execute_script("window.open('about:blank', '_blank');")
                new_tab = list(set(self.driver.window_handles) - old_handles)[0]
                self.tab_handles.append(new_tab)
            
            logger.info(f"🌐 Browser Started with {len(self.tab_handles)} tabs (PID: {self.driver.service.process.pid})")
        except Exception as e:
            logger.error(f"Browser Init Failed: {e}")
            raise e

    def stop(self):
        if self.driver:
            try: self.driver.quit()
            except: pass
            self.driver = None
            self.tab_handles = []

    def switch_to_tab(self, index: int):
        """Switches the driver focus to a specific tab index (0=Kakao, 1=Mecha, 2=Jumptoon)"""
        if not self.driver or index >= len(self.tab_handles): return False
        try:
            self.driver.switch_to.window(self.tab_handles[index])
            return True
        except Exception as e:
            logger.error(f"Failed to switch to tab {index}: {e}")
            return False

    def warmup(self):
        if not self.driver: self.start()

    def enable_mobile(self, enabled=True):
        if not self.driver: return
        if enabled:
            self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True})
            self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True})
        else:
            self.driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
            self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})

    def fetch_blob(self, uri):
        if not uri or not uri.startswith("blob:"): return None
        script = "var uri=arguments[0];var callback=arguments[1];var xhr=new XMLHttpRequest();xhr.responseType='blob';xhr.onload=function(){var reader=new FileReader();reader.onloadend=function(){callback(reader.result.split(',')[1]);};reader.readAsDataURL(xhr.response);};xhr.open('GET',uri);xhr.send();"
        try:
            res = self.driver.execute_async_script(script, uri)
            return base64.b64decode(res) if res else None
        except: return None