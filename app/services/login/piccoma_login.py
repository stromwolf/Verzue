import logging
import asyncio
import random
import time
import uuid
import re
from curl_cffi.requests import AsyncSession
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("LoginService.Piccoma")

class PiccomaLoginHandler:
    def __init__(self, service):
        self.service = service

    async def login(self, creds: dict):
        """Headless Piccoma login using curl_cffi."""
        email = creds.get("email")
        password = creds.get("password")
        account_id = creds.get("account_id", "primary")
        
        base_url = "https://piccoma.com"
        login_page_url = f"{base_url}/web/acc/email/signin"
        
        proxy_url = Settings.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url}
        # Use Chrome 120 baseline
        async with AsyncSession(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            
            await asyncio.sleep(1.0)
            
            # --- Homepage Handshake ---
            logger.info("🏠 [Piccoma] Initializing Homepage Handshake...")
            try:
                ga_id = f"GA1.1.{random.randint(100000000, 999999999)}.{int(time.time())}"
                ga4_id = f"GS2.1.s{int(time.time())}$o103$g1$t{int(time.time())}$j37$l0$h0"
                clck_id = f"{uuid.uuid4().hex[:8]}%5E2%5Eg4y%5E0%5E{random.randint(1000, 9999)}"
                lt_cid = str(uuid.uuid4())
                ast_prm = f"__t_{int(time.time())}000_%7B%22uuid%22%3A%22{uuid.uuid4()}%22%7D"
                snex_id = str(uuid.uuid4())
                
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
                    c_domain = "piccoma.com" if k in ["snexid", "csrftoken"] else ".piccoma.com"
                    session.cookies.set(k, v, domain=c_domain, path="/")
                
                h_res = await session.get(f"{base_url}/web/", timeout=15)
                logger.info(f"   [Handshake] Homepage Status: {h_res.status_code} | Jar: {len(session.cookies.get_dict())} cookies")
            except Exception as e:
                logger.warning(f"   ⚠️ [Handshake] Homepage visit failed ({e}), continuing anyway.")
            
            # 1. Get CSRF Token
            res = await session.get(login_page_url)
            if res.status_code != 200:
                raise ScraperError(f"Failed to load login page: {res.status_code}")
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(res.text, 'html.parser')
            csrf_input = soup.find('input', {'name': 'csrfmiddlewaretoken'})
            csrf_token = csrf_input['value'] if csrf_input else None
            
            if not csrf_token:
                raise ScraperError("Could not find csrfmiddlewaretoken on Piccoma login page.")
            
            # 2. Perform Login POST
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
            post_res = await session.post(login_page_url, data=payload, headers=headers, allow_redirects=True)
            
            # --- Identity Handshake ---
            logger.info("🎭 [Identity Handshake] Warming up session...")
            await asyncio.sleep(1.0)
            await session.get(f"{base_url}/web", headers=headers)
            await asyncio.sleep(0.5)
            # /web/product/favorite may return 404 despite valid login on some accounts.
            # Probe a stable authenticated mypage route instead.
            favorite_res = await session.get(f"{base_url}/web/mypage/bookshelf", headers=headers)
            await asyncio.sleep(0.5)

            is_success = False
            rerendered = "ログイン｜ピッコマ" in post_res.text
            
            if not rerendered:
                try:
                    data = post_res.json()
                    is_success = data.get('result') == 'ok' or data.get('status') == 'success'
                except:
                    is_success = post_res.status_code in [200, 302]

            if is_success and not rerendered:
                cookies = []
                has_pksid = False
                
                try:
                    jar_obj = session.cookies.jar
                    for domain in jar_obj._cookies:
                        for path in jar_obj._cookies[domain]:
                            for name, cookie in jar_obj._cookies[domain][path].items():
                                if name == "pksid" and cookie.value:
                                    has_pksid = True
                                
                                cookies.append({
                                    "name": name, "value": cookie.value, "domain": domain,
                                    "path": path, "expires": getattr(cookie, 'expires', None)
                                })
                    
                    # Ensure injected trackers persist
                    current_names = [c.get('name') for c in cookies]
                    for k, v in injected_trackers.items():
                        if k not in current_names:
                            cookies.append({"name": k, "value": v, "domain": ".piccoma.com", "path": "/"})
                            
                except Exception as e:
                    logger.warning(f"  ⚠️ [Identity Trace] Jar iteration failed ({e}), using items fallback.")
                    for name, value in session.cookies.items():
                        if name == "pksid" and value: has_pksid = True
                        cookies.append({"name": name, "value": value, "domain": ".piccoma.com", "path": "/"})
                
                # Header Fallback for pksid
                if not has_pksid:
                    header_cookies = post_res.headers.get_list('Set-Cookie') if hasattr(post_res.headers, 'get_list') else post_res.headers.get('Set-Cookie', "").split(",")
                    for cookie_str in header_cookies:
                        if "pksid=" in cookie_str:
                            p_match = re.search(r'pksid=([^; ]+)', cookie_str)
                            if p_match:
                                p_val = p_match.group(1)
                                has_pksid = True
                                cookies.append({"name": "pksid", "value": p_val, "domain": ".piccoma.com", "path": "/"})

                favorite_final_url = str(getattr(favorite_res, "url", ""))
                favorite_text = favorite_res.text or ""
                favorite_signin = (
                    "/web/acc/signin" in favorite_final_url
                    or "ログイン｜ピッコマ" in favorite_text
                    or "PCM-loginMenu" in favorite_text
                    or "/acc/signin?next_url=" in favorite_text
                    or "PCM-headerLogin" in favorite_text
                )

                if cookies and has_pksid and (favorite_res.status_code == 200) and not favorite_signin:
                    await self.service.session_service.update_session_cookies("piccoma", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Piccoma ({email}) with pksid.")
                    return True
                else:
                    logger.error(
                        f"🛑 [Piccoma] Login considered invalid after handshake: "
                        f"cookies={len(cookies)}, has_pksid={has_pksid}, "
                        f"favorite_status={favorite_res.status_code}, favorite_signin={favorite_signin}"
                    )
                    return False
            else:
                reason = "Rerendered login page" if rerendered else f"HTTP {post_res.status_code}"
                logger.error(f"[Piccoma] Login failed: {reason}")
                return False
