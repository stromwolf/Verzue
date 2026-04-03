import logging
import json
import os
import asyncio
import time
from urllib.parse import urljoin
from curl_cffi.requests import AsyncSession
from app.services.session_service import SessionService
from app.core.exceptions import ScraperError, MechaException
from config.settings import Settings

logger = logging.getLogger("LoginService")

class LoginService:
    def __init__(self):
        self.session_service = SessionService()
        self.base_secrets_path = os.path.join(os.getcwd(), "data", "secrets")

    async def get_credentials(self, platform: str, account_id: str = "primary"):
        """Retrieves stored credentials for a platform."""
        path = os.path.join(self.base_secrets_path, platform, "account.json")
        if not os.path.exists(path):
            return None
        
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read credentials for {platform}: {e}")
            return None

    async def save_credentials(self, platform: str, email: str, password: str, account_id: str = "primary"):
        """Saves credentials for a platform."""
        dir_path = os.path.join(self.base_secrets_path, platform)
        os.makedirs(dir_path, exist_ok=True)
        
        path = os.path.join(dir_path, "account.json")
        data = {"email": email, "password": password, "account_id": account_id}
        
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=4)
            logger.info(f"💾 Saved credentials for {platform}:{account_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save credentials for {platform}: {e}")
            return False

    async def auto_login(self, platform: str, account_id: str = "primary"):
        """Attempts to log in and refresh cookies with a 3-attempt retry bridge."""
        creds = await self.get_credentials(platform, account_id)
        if not creds:
            logger.warning(f"⚠️ No credentials found for {platform}:{account_id}. Automated login skipped.")
            return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"🔑 Attempting automated login for {platform}:{account_id} (Attempt {attempt}/{max_retries})...")
                
                if platform == "piccoma":
                    success = await self._login_piccoma(creds)
                elif platform == "mecha":
                    success = await self._login_mecha(creds)
                else:
                    logger.warning(f"🤷 No implementation for platform: {platform}")
                    return False

                if success:
                    return True
                
                # If we get here, the function returned False (e.g. bad password), don't retry logic errors
                logger.warning(f"⚠️ Login attempt {attempt} failed (Logic Error). Retrying...")

            except Exception as e:
                # This catches the (56) CONNECT tunnel failed error
                logger.error(f"❌ Attempt {attempt} failed with System/Proxy Error: {e}")
                if attempt < max_retries:
                    wait_time = attempt * 2 # Exponential backoff
                    logger.info(f"⏳ Waiting {wait_time}s before next retry...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.critical(f"💀 All {max_retries} attempts failed for {platform}.")
        
        return False

    async def _login_piccoma(self, creds: dict):
        """Headless Piccoma login using curl_cffi."""
        email = creds.get("email")
        password = creds.get("password")
        account_id = creds.get("account_id", "primary")
        
        base_url = "https://piccoma.com"
        login_page_url = f"{base_url}/web/acc/email/signin"
        
        proxy_url = Settings.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
            # 🟢 Stability Patch: Tiny warm-up delay for proxy tunnel
            await asyncio.sleep(1.0)
            
            # 1. Get CSRF Token (Piccoma uses csrfmiddlewaretoken)
            res = await session.get(login_page_url)
            if res.status_code != 200:
                raise ScraperError(f"Failed to load login page: {res.status_code}")
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            csrf_input = soup.find('input', {'name': 'csrfmiddlewaretoken'})
            csrf_token = csrf_input['value'] if csrf_input else None
            
            if not csrf_token:
                raise ScraperError("Could not find csrfmiddlewaretoken on Piccoma login page.")
            
            # 2. Perform Login POST (Form-encoded)
            payload = {
                "csrfmiddlewaretoken": csrf_token,
                "email": email,
                "password": password
            }
            
            headers = {
                "Referer": login_page_url,
                "Origin": base_url,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            logger.info(f"[Piccoma] Sending login request for {email}...")
            # We use allow_redirects=False to catch the 302 redirect manually if needed, 
            # but curl_cffi handles cookies on redirect by default.
            post_res = await session.post(login_page_url, data=payload, headers=headers, allow_redirects=False)
            
            # --- 🟢 Stability Patch: Cookie Settle Delay ---
            # Piccoma often performs background cookie rotations on first-redirect. 
            # We wait 2s to ensure the session jar captures the final pksid.
            await asyncio.sleep(2.0)

            # Piccoma login usually returns JSON on XMLHttpRequest
            is_success = False
            try:
                data = post_res.json()
                is_success = data.get('result') == 'ok' or data.get('status') == 'success'
            except:
                is_success = post_res.status_code in [200, 302]

            if is_success:
                # 3. Extract and Save Cookies with Metadata (Expiry, etc.)
                cookies = []
                has_pksid = False
                
                try:
                    for cookie in session.cookies:
                        name = getattr(cookie, 'name', None) or str(cookie)
                        value = getattr(cookie, 'value', "")
                        if name == "pksid" and value:
                            has_pksid = True
                            
                        cookies.append({
                            "name": name,
                            "value": value,
                            "domain": getattr(cookie, 'domain', ".piccoma.com"),
                            "path": getattr(cookie, 'path', "/"),
                            "expires": getattr(cookie, 'expires', None)
                        })
                except Exception as e:
                    logger.warning(f"Metadata extraction failed, falling back: {e}")
                    for name, value in session.cookies.items():
                        if name == "pksid" and value:
                            has_pksid = True
                        cookies.append({"name": name, "value": value, "domain": ".piccoma.com"})
                
                if cookies and has_pksid:
                    await self.session_service.update_session_cookies("piccoma", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Piccoma ({email}) with pksid.")
                    return True
                else:
                    logger.error(f"[Piccoma] Login returned success but {'pksid was MISSING' if not has_pksid else 'no cookies found'}.")
                    return False
            else:
                logger.error(f"[Piccoma] Login failed: HTTP {post_res.status_code}")
                return False

    async def _login_mecha(self, creds: dict):
        """Headless Mecha Comic login using curl_cffi."""
        email = creds.get("email")
        password = creds.get("password")
        account_id = creds.get("account_id", "primary")
        
        base_url = "https://mechacomic.jp"
        login_page_url = f"{base_url}/session/input"
        
        proxy_url = Settings.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
            # 🟢 Stability Patch: Tiny warm-up delay for proxy tunnel
            await asyncio.sleep(1.0)
            
            # 1. Get Authenticity Token
            res = await session.get(login_page_url)
            if res.status_code != 200:
                raise ScraperError(f"Failed to load Mecha login page: {res.status_code}")
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # Debug: What are we seeing?
            logger.debug(f"[Mecha] Page Title: {soup.title.string if soup.title else 'No Title'}")
            
            # Determine login action and token
            form = soup.select_one('form#login_form, form[action*="/session"], form[action*="login"]')
            if not form:
                logger.debug(f"[Mecha] DEBUG - Page Content Snippet: {res.text[:1000]}")
                raise ScraperError("Could not find login form on Mecha Comic.")
                
            action_url = urljoin(base_url, form['action'])
            token_elem = form.find('input', {'name': 'authenticity_token'})
            token = token_elem['value'] if token_elem else None
            
            if not token:
                raise ScraperError("Could not find authenticity_token for Mecha Comic.")
                
            # 2. Perform Login POST
            payload = {
                "authenticity_token": token,
                "email": email,
                "password": password,
                "remember_me": "1",
                "commit": "ログイン"
            }
            
            headers = {
                "Referer": login_page_url,
                "Origin": base_url,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            logger.info(f"[Mecha] Sending login request for {email}...")
            # Mecha usually redirects (302) on success. We MUST allow redirects to ensure 
            # all session initialization cookies are set and handled.
            post_res = await session.post(action_url, data=payload, headers=headers, allow_redirects=True)
            
            if post_res.status_code in [200, 302]:
                # 3. Extract and Save Cookies with Metadata (Expiry, etc.)
                cookies = []
                try:
                    # curl_cffi's jar is a RequestsCookieJar, iterating it yields Cookie objects in newer versions
                    # but we use list_cookies() to be absolutely sure we get standard Cookie metadata.
                    # Or we use get_dict() and supplement.
                    for name, value in session.cookies.get_dict().items():
                        cookies.append({
                            "name": name,
                            "value": value,
                            "domain": ".mechacomic.jp",
                            "path": "/",
                            # We set a default expiry if missing to ensure they aren't treated as session-only
                            "expires": int(time.time() + 86400 * 30) 
                        })
                except Exception as e:
                    logger.warning(f"Metadata extraction failed for Mecha: {e}")
                    for name, value in session.cookies.items():
                        cookies.append({"name": name, "value": value, "domain": ".mechacomic.jp"})
                
                if cookies:
                    logger.info(f"[Mecha] Extracted {len(cookies)} cookies: {', '.join([c['name'] for c in cookies])}")
                    await self.session_service.update_session_cookies("mecha", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Mecha Comic ({email})")
                    return True
                else:
                    logger.error("[Mecha] Login successful but no cookies extracted.")
                    return False
            else:
                logger.error(f"[Mecha] Login failed: HTTP {post_res.status_code}")
                return False
