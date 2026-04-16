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
        from app.providers.platforms.piccoma.helpers import PiccomaHelpers
        email = creds.get("email")
        password = creds.get("password")
        account_id = creds.get("account_id", "primary")
        
        base_url = "https://piccoma.com"
        login_page_url = f"{base_url}/web/acc/email/signin"
        
        proxy_url = Settings.get_proxy()
        proxies = {"http": proxy_url, "https": proxy_url}
        # Use Chrome 120 baseline (Supported Preset)
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
            
            # S-Grade: Separate Navigation vs XHR headers
            page_headers = PiccomaHelpers.get_navigation_headers(referer=login_page_url)
            
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

            # [PHASE 8] Capture pksid from the FINAL response Set-Cookie before warmup:
            if not found_pksid_on_post:
                hdr_val = post_res.headers.get('Set-Cookie', '') or ''
                # Split cookies correctly if joined by comma (common in some clients, though Set-Cookie is technically multi-header)
                for sc in hdr_val.split(','):
                    m = re.search(r'pksid=([^; ]+)', sc)
                    if m and m.group(1):
                        logger.info("🔑 [Piccoma] Captured pksid from final response Set-Cookie.")
                        session.cookies.set("pksid", m.group(1), domain=".piccoma.com", path="/")
                        found_pksid_on_post = True
                        break

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
                    # Using get_dict() is domain-agnostic for the names, but we need to ensure pksid is found.
                    flat = session.cookies.get_dict()
                    for name, value in flat.items():
                        if name == "pksid" and value:
                            has_pksid = True
                        cookies.append({
                            "name": name, "value": value,
                            "domain": ".piccoma.com", "path": "/", "expires": None
                        })
                    
                    # Belt-and-suspenders: direct .get() in case get_dict missed a domain-scoped cookie
                    if not has_pksid:
                        for domain_key in (".piccoma.com", "piccoma.com", ""):
                            try:
                                pv = session.cookies.get("pksid", domain=domain_key) if domain_key else session.cookies.get("pksid")
                            except TypeError:
                                pv = session.cookies.get("pksid")
                            if pv:
                                has_pksid = True
                                if not any(c["name"] == "pksid" for c in cookies):
                                    cookies.append({"name": "pksid", "value": pv, "domain": ".piccoma.com", "path": "/"})
                                break

                    # Ensure injected trackers persist (existing logic, unchanged)
                    current_names = {c["name"] for c in cookies}
                    for k, v in injected_trackers.items():
                        if k not in current_names:
                            cookies.append({"name": k, "value": v, "domain": ".piccoma.com", "path": "/"})
                            
                except Exception as e:
                    logger.warning(f"  ⚠️ [Identity Trace] Cookie collection failed ({e}), falling back to items()")
                    for name, value in session.cookies.items():
                        if name == "pksid" and value:
                            has_pksid = True
                        cookies.append({"name": name, "value": value, "domain": ".piccoma.com", "path": "/"})

                # [PHASE 9] Hardened Probe evaluation
                from app.providers.platforms.piccoma.auth import PiccomaAuth
                probe_handler = PiccomaAuth(None)
                probe = await probe_handler.auth_probe(session, strictness="normal")
                auth_ok = (probe["verdict"] != "INVALID")
                probes_str = " | ".join(probe["details"])
                
                if probe["verdict"] == "HEALTHY":
                    logger.info(
                        f"✅ [Piccoma] Login HEALTHY. cookies={len(session.cookies)}, "
                        f"probes=({probes_str})"
                    )
                elif probe["verdict"] == "DEGRADED":
                    logger.warning(
                        f"⚠️ [Piccoma] Login DEGRADED but acceptable. cookies={len(session.cookies)}, "
                        f"probes=({probes_str})"
                    )
                else:  # INVALID
                    logger.error(
                        f"🛑 [Piccoma] Login INVALID. cookies={len(session.cookies)}, "
                        f"has_pksid={probe['pksid_present']}, probes=({probes_str})"
                    )

                if cookies and has_pksid and auth_ok:
                    await self.service.session_service.update_session_cookies("piccoma", account_id, cookies)
                    logger.info(f"✅ Automated login successful for Piccoma ({email}) [{probes_str}].")
                    return True
                else:
                    return False
            else:
                reason = "Rerendered login page" if rerendered else f"HTTP {post_res.status_code}"
                logger.error(f"[Piccoma] Login failed: {reason}")
                return False
