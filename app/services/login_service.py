import logging
import json
import os
import asyncio
import time
import re
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
        # Use Chrome 130 or higher to match modern browser behavior
        async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
            # 🟢 S+ USER-AGENT MATCH
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            })
            
            # 🟢 S+ Stability Patch: Tiny warm-up delay for proxy tunnel
            await asyncio.sleep(1.0)
            
            # --- 🟢 S+ USER-REQUEST: Homepage Handshake ---
            # Visit the homepage first to initialize browser tracking cookies (_ga, _clck, snexid, etc.)
            logger.info("🏠 [Piccoma] Initializing Homepage Handshake...")
            try:
                # --- 🟢 S+ USER-REQUEST: Dense Tracking Identity ---
                # Using the 'Gold Standard' cookie list from user's VPS
                import uuid, random, time
                # GA & Site-Specific GA4
                ga_id = f"GA1.1.{random.randint(100000000, 999999999)}.{int(time.time())}"
                ga4_id = f"GS2.1.s{int(time.time())}$o103$g1$t{int(time.time())}$j37$l0$h0"
                # Clarity & Line/Kakao Tracking
                clck_id = f"{uuid.uuid4().hex[:8]}%5E2%5Eg4y%5E0%5E{random.randint(1000, 9999)}"
                lt_cid = str(uuid.uuid4())
                ast_prm = f"__t_{int(time.time())}000_%7B%22uuid%22%3A%22{uuid.uuid4()}%22%7D"
                snex_id = str(uuid.uuid4())
                
                # Injected trackers list (Dense Tracking)
                injected_trackers = {
                    "_ga": ga_id,
                    "_ga_9DBP9C6JX2": ga4_id,
                    "_clck": clck_id,
                    "snexid": snex_id,
                    "_ttp": str(uuid.uuid4()),
                    "_ebtd": f"1.{uuid.uuid4().hex[:10]}.{int(time.time())}",
                    "ttcsid": f"{int(time.time())}000::{uuid.uuid4().hex[:20]}.98.{int(time.time())}.0",
                    "_im_vid": f"01K{uuid.uuid4().hex[:20].upper()}",
                    "__ast_prm": ast_prm,
                    "__lt__cid": lt_cid,
                    "__lt__sid": f"{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:8]}",
                    "_yjsu_yjad": f"{int(time.time())}.{uuid.uuid4()}"
                }
                
                for k, v in injected_trackers.items():
                    # Correct domain scoping based on user's VPS log
                    c_domain = "piccoma.com" if k in ["snexid", "csrftoken"] else ".piccoma.com"
                    session.cookies.set(k, v, domain=c_domain, path="/")
                
                h_res = await session.get(f"{base_url}/web/", timeout=15)
                jar_len = len(session.cookies.get_dict())
                logger.info(f"   [Handshake] Homepage Status: {h_res.status_code} | Jar: {jar_len} cookies (Dense Identity Active)")
            except Exception as e:
                logger.warning(f"   ⚠️ [Handshake] Homepage visit failed ({e}), continuing anyway.")
            
            # 1. Get CSRF Token (Piccoma uses csrfmiddlewaretoken)
            res = await session.get(login_page_url)
            if res.status_code != 200:
                raise ScraperError(f"Failed to load login page: {res.status_code}")
            
            # --- Login Page Load ---
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            csrf_input = soup.find('input', {'name': 'csrfmiddlewaretoken'})
            csrf_token = csrf_input['value'] if csrf_input else None
            
            if not csrf_token:
                logger.info(f"🔎 [DEV-MODE Failure Trace] HTML Snippet: {res.text[:500]}")
                raise ScraperError("Could not find csrfmiddlewaretoken on Piccoma login page.")
            
            # 2. Perform Login POST (Form-encoded)
            payload = {
                "csrfmiddlewaretoken": csrf_token,
                "email": email,
                "password": password,
                "next_url": "/web"
            }
            
            headers = {
                "Referer": login_page_url,
                "Origin": base_url,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest"
            }
            
            logger.info(f"[Piccoma] Sending login request for {email}...")
            # S+ Aggression: Enable redirects to follow auth-callback chain
            post_res = await session.post(login_page_url, data=payload, headers=headers, allow_redirects=True)
            
            # --- 🟢 S+ Identity Handshake: Warm up the session ---
            # We visit multiple pages to ensure all tracking and session cookies are fully settled.
            # This is critical for capturing cross-domain cookies and pksid.
            logger.info("🎭 [Identity Handshake] Warming up session with multiple requests...")
            await asyncio.sleep(1.0)
            await session.get(f"{base_url}/web", headers=headers)
            await asyncio.sleep(0.5)
            await session.get(f"{base_url}/web/product/favorite", headers=headers)
            await asyncio.sleep(0.5)

            # Piccoma login success detection
            is_success = False
            rerendered = "ログイン｜ピッコマ" in post_res.text
            
            if not rerendered:
                # If not rerendered, check JSON or status
                try:
                    data = post_res.json()
                    is_success = data.get('result') == 'ok' or data.get('status') == 'success'
                except:
                    is_success = post_res.status_code in [200, 302]


            if is_success and not rerendered:
                cookies = []
                has_pksid = False
                
                # 🕵️ [DEVELOPER MODE]: Exhaustive Identity Audit
                logger.info("🕵️ [DEV-MODE] Login flow complete. Auditing resulting Jar state...")
                
                try:
                    jar_obj = session.cookies.jar
                    log_entries = []
                    for domain in jar_obj._cookies:
                        for path in jar_obj._cookies[domain]:
                            for name, cookie in jar_obj._cookies[domain][path].items():
                                if name == "pksid" and cookie.value:
                                    has_pksid = True
                                
                                # High level trace entry
                                val_preview = f"{cookie.value[:4]}...{cookie.value[-4:]}" if len(cookie.value) > 8 else cookie.value
                                log_entries.append(f"   🍪 {name:<12} | Dom: {domain:<18} | Path: {path:<5} | Val: {val_preview}")
                                
                                cookies.append({
                                    "name": name,
                                    "value": cookie.value,
                                    "domain": domain,
                                    "path": path,
                                    "expires": getattr(cookie, 'expires', None)
                                })
                    
                    if log_entries:
                        logger.info(f"🔎 [Identity Trace] Total {len(log_entries)} cookies captured from JAR:")
                        for entry in log_entries: logger.info(entry)
                        
                        # --- 🟢 S+ USER-REQUEST: Explicit Tracker Persistence ---
                        # Ensure the injected breadcrumbs (if they aren't already captured) are saved.
                        # This prevents 'SUSPICIOUS' warnings and 'Naked Bot' rejection on reload.
                        current_names = [c.get('name') for c in cookies]
                        for k, v in injected_trackers.items():
                            if k not in current_names:
                                cookies.append({"name": k, "value": v, "domain": ".piccoma.com", "path": "/"})
                    else:
                        logger.warning("⚠️ [Identity Trace] WARNING: Jar is EMPTY after handshake!")
                        
                except Exception as e:
                    logger.warning(f"  ⚠️ [Identity Trace] Jar iteration failed ({e}), using items fallback.")
                    for name, value in session.cookies.items():
                        if name == "pksid" and value: has_pksid = True
                        cookies.append({"name": name, "value": value, "domain": ".piccoma.com", "path": "/"})
                
                # --- 🟢 S+ GRADE: Header Fallback ---
                if not has_pksid:
                    logger.info("🔎 [DEV-MODE] pksid missing from jar. Scanning response headers...")
                    header_cookies = post_res.headers.get_list('Set-Cookie') if hasattr(post_res.headers, 'get_list') else post_res.headers.get('Set-Cookie', "").split(",")
                    for cookie_str in header_cookies:
                        logger.info(f"   [Header Scan] -> {cookie_str[:80]}...")
                        if "pksid=" in cookie_str:
                            p_match = re.search(r'pksid=([^; ]+)', cookie_str)
                            if p_match:
                                p_val = p_match.group(1)
                                logger.info(f"✅ [Header Scan] SUCCESS! Captured pksid from headers.")
                                has_pksid = True
                                cookies.append({"name": "pksid", "value": p_val, "domain": ".piccoma.com", "path": "/"})
                
                if cookies and has_pksid:
                    await self.session_service.update_session_cookies("piccoma", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Piccoma ({email}) with pksid.")
                    return True
                else:
                    logger.error(f"🛑 [DEV-MODE] Login returned success but pksid is MISSING. (Found cookies: {len(cookies)})")
                    return False
            else:
                reason = "Rerendered login page" if rerendered else f"HTTP {post_res.status_code}"
                logger.error(f"[Piccoma] Login failed: {reason}")
                
                # --- 🛠️ Diagnostic Fallback on Failure ---
                found_cookies = [f"{n} | {len(v)}" for n, v in session.cookies.items()]
                logger.info(f"🔎 [DEV-MODE Cookie Audit] Current Jar Keys: {', '.join(found_cookies)}")
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
