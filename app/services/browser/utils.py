import json
import logging
from config.settings import Settings

logger = logging.getLogger("BrowserUtils")

class BrowserUtils:
    @staticmethod
    def load_cookies(driver):
        """Aggregates cookies from all account files into the browser."""
        current_url = driver.current_url.lower()
        platform = "mecha" if "mechacomic.jp" in current_url else \
                   "jumptoon" if "jumptoon.com" in current_url else \
                   "kakao" if "kakao.com" in current_url else None
        
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
                for c in cookies:
                    # Clean up expiry types if they are invalid for Selenium
                    if 'expiry' in c and not isinstance(c['expiry'], (int, float)):
                        del c['expiry']
                    
                    try: 
                        driver.add_cookie(c)
                        total_injected += 1
                    except: pass
            except Exception as e:
                logger.error(f"Error loading {path.name}: {e}")

        if total_injected > 0:
            logger.info(f"[Browser] 🍪 Injected {total_injected} cookies from {len(cookie_paths)} sources.")
            return True
        return False

    @staticmethod
    def save_cookies(driver):
        """Saves current browser cookies back to the account file."""
        current_url = driver.current_url.lower()
        platform = "mecha" if "mechacomic.jp" in current_url else \
                   "jumptoon" if "jumptoon.com" in current_url else \
                   "kakao" if "kakao.com" in current_url else None
        
        if not platform: return
        
        try:
            cookies = driver.get_cookies()
            # Save to default fallback or primary account file
            path = Settings.SECRETS_DIR / platform / "cookies.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(path, 'w') as f:
                json.dump(cookies, f, indent=4)
            logger.debug(f"[Browser] 💾 Cookies saved for {platform} to {path.name}")
        except Exception as e:
            logger.error(f"Failed to save cookies for {platform}: {e}")