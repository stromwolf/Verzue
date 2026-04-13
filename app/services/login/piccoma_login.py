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


def _piccoma_response_denies_auth(res) -> bool:
    """True if response clearly indicates guest / sign-in required."""
    u = str(getattr(res, "url", "") or "")
    t = res.text or ""
    if "/web/acc/signin" in u or "/acc/email/signin" in u or "/acc/signin?" in u:
        return True
    if "ログイン｜ピッコマ" in t:
        return True
    if "PCM-loginMenu" in t:
        return True
    if "/acc/signin?next_url=" in t:
        return True
    # Large episode/viewer HTML includes this class name inside JS — only treat as login shell when small
    if len(t) < 70000 and "PCM-headerLogin" in t:
        return True
    return False


def _piccoma_probe_confirms_auth(res) -> bool:
    """200 OK and no explicit guest/signin markers."""
    if res.status_code != 200:
        return False
    return not _piccoma_response_denies_auth(res)


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
        # Use Chrome 142 baseline (Server Compatibility)
        async with AsyncSession(impersonate="chrome142", proxies=proxies) as session:
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
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
            
            # S-Grade: Separate Navigation vs XHR headers
            page_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Referer": login_page_url,
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="142", "Google Chrome";v="142"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1"
            }
            
            xhr_headers = page_headers.copy()
            xhr_headers.update({
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": base_url,
                "Accept": "application/json, text/plain, */*"
            })
            
            logger.info(f"[Piccoma] Sending login request for {email}...")
            post_res = await session.post(login_page_url, data=payload, headers=xhr_headers, allow_redirects=False)
            
            # --- Cookie Interception ---
            # Piccoma often sets pksid on the initial 302 redirect response.
            found_pksid_on_post = False
            sc_headers = post_res.headers.get_list('Set-Cookie') if hasattr(post_res.headers, 'get_list') else post_res.headers.get('Set-Cookie', "").split(",")
            for sc in sc_headers:
                if "pksid=" in sc:
                    p_match = re.search(r'pksid=([^; ]+)', sc)
                    if p_match:
                        p_val = p_match.group(1)
                        logger.info("🔑 [Piccoma] Intercepted pksid on initial POST response.")
                        session.cookies.set("pksid", p_val, domain=".piccoma.com", path="/")
                        found_pksid_on_post = True

            # If it's a redirect, follow it manually to see where we land
            if post_res.status_code in [302, 301]:
                next_loc = post_res.headers.get('Location')
                if next_loc:
                    if not next_loc.startswith('http'):
                        next_loc = f"{base_url}{next_loc}"
                    logger.debug(f"📡 [Piccoma] Following login redirect: {next_loc}")
                    post_res = await session.get(next_loc, headers=page_headers)

            # --- Identity Handshake ---
            logger.info("🎭 [Identity Handshake] Warming up session...")
            await asyncio.sleep(1.0)
            web_res = await session.get(f"{base_url}/web/", headers=page_headers, timeout=15)
            await asyncio.sleep(0.5)
            # S-Grade: Probes must use navigation headers (no XHR)
            shelf_res = await session.get(f"{base_url}/web/bookshelf", headers=page_headers, timeout=15)
            await asyncio.sleep(0.3)
            hist_res = await session.get(f"{base_url}/web/history", headers=page_headers, timeout=15)
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

                auth_ok = (
                    _piccoma_probe_confirms_auth(shelf_res)
                    or _piccoma_probe_confirms_auth(hist_res)
                )
                
                # S-Grade: If shelf/history fail (404), but web=200 and has_pksid is set, 
                # we perform one last strict check on the 'web' page content.
                if not auth_ok and has_pksid and web_res.status_code == 200:
                    # Look for logout links or profile indicators instead of just 'not denies_auth'
                    html = web_res.text
                    is_logged_in_html = "/acc/signout" in html or "PCM-header_user" in html or "本棚" in html
                    auth_ok = is_logged_in_html

                probe_summary = (
                    f"web={web_res.status_code}, shelf={shelf_res.status_code}, hist={hist_res.status_code}"
                )

                if cookies and has_pksid and auth_ok:
                    await self.service.session_service.update_session_cookies("piccoma", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Piccoma ({email}) with pksid ({probe_summary}).")
                    return True
                else:
                    logger.error(
                        f"🛑 [Piccoma] Login considered invalid after handshake: "
                        f"cookies={len(cookies)}, has_pksid={has_pksid}, auth_ok={auth_ok}, probes=({probe_summary})"
                    )
                    return False
            else:
                reason = "Rerendered login page" if rerendered else f"HTTP {post_res.status_code}"
                logger.error(f"[Piccoma] Login failed: {reason}")
                return False
