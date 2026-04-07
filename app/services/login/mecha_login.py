import logging
import asyncio
import time
from urllib.parse import urljoin
from curl_cffi.requests import AsyncSession
from app.core.exceptions import ScraperError, MechaException
from config.settings import Settings

logger = logging.getLogger("LoginService.Mecha")

class MechaLoginHandler:
    def __init__(self, service):
        self.service = service

    async def login(self, creds: dict):
        """Headless Mecha Comic login using curl_cffi."""
        email = creds.get("email")
        password = creds.get("password")
        account_id = creds.get("account_id", "primary")
        
        base_url = "https://mechacomic.jp"
        login_page_url = f"{base_url}/session/input"
        
        proxy_url = Settings.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
            await asyncio.sleep(1.0)
            
            # 1. Get Authenticity Token
            res = await session.get(login_page_url)
            if res.status_code != 200:
                raise ScraperError(f"Failed to load Mecha login page: {res.status_code}")
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            
            form = soup.select_one('form#login_form, form[action*="/session"], form[action*="login"]')
            if not form:
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
            post_res = await session.post(action_url, data=payload, headers=headers, allow_redirects=True)
            
            if post_res.status_code in [200, 302]:
                cookies = []
                try:
                    for name, value in session.cookies.get_dict().items():
                        cookies.append({
                            "name": name, "value": value, "domain": ".mechacomic.jp",
                            "path": "/", "expires": int(time.time() + 86400 * 30) 
                        })
                except Exception as e:
                    logger.warning(f"Metadata extraction failed for Mecha: {e}")
                    for name, value in session.cookies.items():
                        cookies.append({"name": name, "value": value, "domain": ".mechacomic.jp"})
                
                if cookies:
                    logger.info(f"[Mecha] Extracted {len(cookies)} cookies.")
                    await self.service.session_service.update_session_cookies("mecha", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Mecha Comic ({email})")
                    return True
                else:
                    logger.error("[Mecha] Login successful but no cookies extracted.")
                    return False
            else:
                logger.error(f"[Mecha] Login failed: HTTP {post_res.status_code}")
                return False
