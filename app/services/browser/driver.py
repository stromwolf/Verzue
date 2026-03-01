import logging
import base64
import time
import threading
import re
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from fake_useragent import UserAgent
from webdriver_manager.chrome import ChromeDriverManager
from config.settings import Settings
from .utils import BrowserUtils

logger = logging.getLogger("BrowserService")

class BrowserService:
    def __init__(self, headless: bool = None):
        self.driver = None
        self.default_headless = headless if headless is not None else Settings.HEADLESS
        self.active_sessions = 0 # TRACKS ACTIVE DASHBOARDS
        self.tab_handles = [] # [kakao_tab, mecha_tab, jumptoon_tab]
        
        # 🛡️ RAM Shield: Prevents multiple threads from using the browser simultaneously
        self.handshake_lock = threading.Lock() 

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
        
        # Use explicit location if provided, otherwise use webdriver_manager to bypass the blocked selenium-manager
        if Settings.DRIVER_LOCATION:
            service = ChromeService(executable_path=Settings.DRIVER_LOCATION)
        else:
            driver_path = ChromeDriverManager().install()
            service = ChromeService(executable_path=driver_path)

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

    # =========================================================
    # NEW METHOD: ISOLATED HANDSHAKE (ZERO-RAM SPIKE)
    # =========================================================
    def run_isolated_handshake(self, url: str, cookie_list: list, selectors: list):
        """
        購入処理やCloudflare回避のためだけにブラウザを同期的に実行します。
        Lock機能により、複数タスクが同時にブラウザを操作してRAMが枯渇するのを防ぎます。
        """
        with self.handshake_lock:
            logger.info(f"🛡️ [Handshake] Acquiring Browser Lock for {url}")
            if not self.driver:
                self.start()

            try:
                # 1. めちゃコミック専用タブ（インデックス1）に切り替え
                self.switch_to_tab(1)
                
                # 2. クッキーを設定するために軽量なページを開く
                domain = "https://mechacomic.jp"
                self.driver.get(domain + "/robots.txt")
                self.driver.delete_all_cookies()
                
                for c in cookie_list:
                    cookie_dict = {'name': c['name'], 'value': c['value']}
                    if 'domain' in c: cookie_dict['domain'] = c['domain']
                    if 'path' in c: cookie_dict['path'] = c['path']
                    try:
                        self.driver.add_cookie(cookie_dict)
                    except Exception as e:
                        logger.debug(f"Failed to add cookie {c.get('name')}: {e}")

                # 3. 目的のチャプターURLに移動
                self.driver.get(url)
                time.sleep(3) # 読み込み・Cloudflare通過待機

                # 4. 「無料で読む」や「購入」ボタンを探してクリック
                clicked = False
                for sel in selectors:
                    try:
                        btns = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        if btns and btns[0].is_displayed() and btns[0].is_enabled():
                            label = btns[0].get_attribute("value") or btns[0].text
                            logger.info(f"   👆 [Handshake] Clicking button: '{label}'")
                            self.driver.execute_script("arguments[0].click();", btns[0])
                            time.sleep(3) # リダイレクト・購入処理の完了待機
                            clicked = True
                            break
                    except: pass

                # 5. ビュワー（閲覧画面）のURLを取得できたか確認
                viewer_url = None
                html = self.driver.page_source
                current_url = self.driver.current_url
                
                if "contents_vertical" in current_url:
                    viewer_url = current_url
                else:
                    match = re.search(r'\"(https?://mechacomic\.jp/viewer\?.*?contents_vertical=.*?)\"', html)
                    if match:
                        viewer_url = match.group(1).replace('\\/', '/')

                # 6. 新しく更新されたクッキー（セッション維持用）を取得
                new_cookies = self.driver.get_cookies()
                
                logger.info(f"🏁 [Handshake] Complete. Viewer URL Found: {bool(viewer_url)}")
                return new_cookies, viewer_url

            except Exception as e:
                logger.error(f"❌ [Handshake] Error during browser fallback: {e}")
                return None, None