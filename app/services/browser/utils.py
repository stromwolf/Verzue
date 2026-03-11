import json
import logging
from playwright.async_api import Page, BrowserContext
from config.settings import Settings

logger = logging.getLogger("BrowserUtils")

class BrowserUtils:
    @staticmethod
    async def load_cookies(context: BrowserContext, url: str):
        """Aggregates cookies from all account files into the Playwright context."""
        url_lower = url.lower()
        platform = "mecha" if "mechacomic.jp" in url_lower else \
                   "jumptoon" if "jumptoon.com" in url_lower else \
                   "kakao" if "kakao.com" in url_lower else None
        
        cookie_paths = []
        if platform:
            platform_dir = Settings.SECRETS_DIR / platform
            if platform_dir.exists():
                cookie_paths.extend(list(platform_dir.glob("*.json")))
        
        # Add legacy fallback
        if Settings.COOKIES_FILE.exists():
            cookie_paths.append(Settings.COOKIES_FILE)

        total_injected = 0
        for path in cookie_paths:
            try:
                with open(path, 'r') as f:
                    cookies = json.load(f)
                
                # Format for Playwright
                formatted = []
                for c in cookies:
                    if not c.get('name') or not c.get('value'): continue
                    
                    cookie_dict = {
                        'name': c['name'],
                        'value': c['value'],
                        'url': f"https://{c['domain'].lstrip('.')}" if c.get('domain') else url
                    }
                    if 'path' in c: cookie_dict['path'] = c['path']
                    # Playwright handles domain better via 'url' or 'domain'
                    if 'domain' in c: cookie_dict['domain'] = c['domain']
                    
                    formatted.append(cookie_dict)
                
                if formatted:
                    await context.add_cookies(formatted)
                    total_injected += len(formatted)
            except Exception as e:
                logger.error(f"Error loading {path.name}: {e}")

        if total_injected > 0:
            logger.info(f"[Browser] 🍪 Injected {total_injected} cookies from {len(cookie_paths)} sources.")
            return True
        return False

    @staticmethod
    async def save_cookies(context: BrowserContext, url: str):
        """Saves current Playwright context cookies back to the account file."""
        url_lower = url.url.lower() if hasattr(url, 'url') else url.lower()
        platform = "mecha" if "mechacomic.jp" in url_lower else \
                   "jumptoon" if "jumptoon.com" in url_lower else \
                   "kakao" if "kakao.com" in url_lower else None
        
        if not platform: return
        
        try:
            cookies = await context.cookies()
            # Save to default fallback or primary account file
            path = Settings.SECRETS_DIR / platform / "cookies.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(path, 'w') as f:
                json.dump(cookies, f, indent=4)
            logger.debug(f"[Browser] 💾 Cookies saved for {platform} to {path.name}")
        except Exception as e:
            logger.error(f"Failed to save cookies for {platform}: {e}")