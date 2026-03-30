import logging
import base64
import asyncio
import re
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from config.settings import Settings

logger = logging.getLogger("BrowserService")

class BrowserService:
    def __init__(self, headless: bool = None):
        self.playwright = None
        self.browser: Browser = None
        self.context: BrowserContext = None
        self.default_headless = headless if headless is not None else Settings.HEADLESS
        self.active_sessions = 0
        
        # 🟢 Playwright is natively async; we don't need a threading Lock anymore.
        # Instead, we ensure the browser is initialized before use.
        self._init_lock = asyncio.Lock()

    async def start(self, headless: bool = None):
        """Initializes the Playwright engine and the main persistent context."""
        async with self._init_lock:
            if self.browser:
                return

            self.playwright = await async_playwright().start()
            is_headless = headless if headless is not None else self.default_headless

            # 🟢 Use Persistent Context for session stickiness (cookies, etc.)
            logger.info("🌐 Launching Playwright Chromium (Persistent Context)...")
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(Settings.BROWSER_PROFILE_DIR),
                headless=is_headless,
                channel="chrome", # Use installed Chrome if possible
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox"
                ]
            )
            self.browser = self.context.browser
            logger.info("🌐 Browser Service Online (Playwright).")

    async def stop(self):
        async with self._init_lock:
            if self.context:
                await self.context.close()
            if self.playwright:
                await self.playwright.stop()
            self.browser = None
            self.context = None
            self.playwright = None
            logger.info("🛑 Browser Service Shutdown.")

    async def get_new_page(self) -> Page:
        """Creates a new page within the shared persistent context."""
        if not self.context:
            await self.start()
        return await self.context.new_page()

    async def run_isolated_handshake(self, url: str, cookie_list: list, selectors: list):
        """
        Performs an async handshake to solve Cloudflare/Bot-Detections.
        Unlike Selenium, this can run concurrently with other pages!
        """
        if not self.context:
            await self.start()

        page = await self.get_new_page()
        try:
            logger.info(f"🛡️ [Handshake] Navigating to {url}...")
            
            # 1. Set Cookies
            from urllib.parse import urlparse
            parsed_uri = urlparse(url)
            domain = parsed_uri.netloc.replace("www.", "")
            
            formatted_cookies = []
            for c in cookie_list:
                formatted_cookies.append({
                    'name': c['name'], 
                    'value': c['value'], 
                    'domain': c.get('domain', f".{domain}"),
                    'path': c.get('path', '/')
                })
            
            if formatted_cookies:
                await self.context.add_cookies(formatted_cookies)

            # 2. Go to URL
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # 3. Handle Interactions (Select/Click)
            viewer_url = None
            for sel in selectors:
                try:
                    button = page.locator(sel).first
                    if await button.is_visible(timeout=2000):
                        label = await button.get_attribute("value") or await button.inner_text()
                        logger.info(f"   👆 [Handshake] Clicking: '{label.strip()}'")
                        await button.click()
                        await page.wait_for_timeout(3000)
                        break
                except:
                    continue

            # 4. Success Check based on URL
            if "contents_vertical" in page.url or "viewer" in page.url:
                viewer_url = page.url

            # 5. Extract Session Cookies
            new_cookies = await self.context.cookies()
            
            logger.info(f"🏁 [Handshake] Complete. Viewer Found: {bool(viewer_url)}")
            return new_cookies, viewer_url

        except Exception as e:
            logger.error(f"❌ [Handshake] Playwright Error: {e}")
            return None, None
        finally:
            await page.close()

    async def fetch_blob(self, page: Page, uri: str):
        """Fetches a blob URI from the page context and returns bytes."""
        if not uri or not uri.startswith("blob:"):
            return None
        
        try:
            # Playwright can evaluate script to fetch blob and return as base64
            script = """
                async (uri) => {
                    const response = await fetch(uri);
                    const blob = await response.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    });
                }
            """
            b64_data = await page.evaluate(script, uri)
            return base64.b64decode(b64_data) if b64_data else None
        except Exception as e:
            logger.debug(f"Failed to fetch blob {uri}: {e}")
            return None

    # REPLACEMENTS FOR LEGACY SELENIUM METHODS (To avoid crashes during migration)
    def enable_mobile(self, enabled=True): pass 
    def warmup(self): pass
    def inc_session(self): self.active_sessions += 1
    def dec_session(self): self.active_sessions = max(0, self.active_sessions - 1)