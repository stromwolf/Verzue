import logging
import os
import random
import re
import time
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from app.core.exceptions import ScraperError

logger = logging.getLogger("PiccomaHelpers")

class PiccomaHelpers:
    def __init__(self, provider):
        self.provider = provider

    def _format_poster_url(self, url: str | None) -> str | None:
        """S+ Refinement: Unified poster formatting logic for Discord embeds via wsrv.nl proxy."""
        if not url: return None
        if url.startswith('//'): url = 'https:' + url
        
        # Consistent Proxying for Discord Embed reliability
        if any(domain in url for domain in ['piccoma.com', 'piccoma-static.com', 'piccoma.jp', 'kakaocdn.net']):
            return f"https://wsrv.nl/?url={urllib.parse.quote(url)}&w=600&fit=cover"
        return url

    def _get_context_from_url(self, url: str):
        """S+ Refinement: Stateless context derivation."""
        if "fr.piccoma" in url or "/fr" in url:
            raise ScraperError("Piccoma France (.fr) is not supported at this time. Please use a Piccoma Japan (.com) link.")
        return "https://piccoma.com", "jp", ".piccoma.com"

    def session_has_pksid(self, auth_session) -> bool:
        """True when the jar has a non-empty pksid session cookie."""
        try:
            jar = getattr(auth_session, "cookies", None)
            if jar is not None and hasattr(jar, "get"):
                if jar.get("pksid"):
                    return True
            for c in getattr(jar, "jar", []) or []:
                if getattr(c, "name", None) == "pksid" and getattr(c, "value", None):
                    return True
        except Exception:
            pass
        return False

    def piccoma_html_indicates_guest_shell(self, final_url: str, html: str) -> bool:
        """
        High-confidence logged-out / sign-in HTML. Do not use bare 'PCM-headerLogin' on large pages:
        Smartoon bundles embed that string and cause false 'guest' detections.
        """
        u = final_url or ""
        if "/web/acc/signin" in u or "/acc/email/signin" in u:
            return True
        t = html or ""
        if "ログイン｜ピッコマ" in t:
            return True
        if "PCM-loginMenu" in t:
            return True
        if "/acc/signin?next_url=" in t:
            return True
        if len(t) < 70000 and "PCM-headerLogin" in t:
            return True
        return False

    def viewer_redirected_to_product_page(self, viewer_url: str, response_final_url: str) -> bool:
        """True when a viewer URL was requested but the session landed on a series product page (paywall / not unlocked)."""
        if not viewer_url or not re.search(r"/web/viewer/(?:s/)?\d+/\d+", viewer_url):
            return False
        clean = (response_final_url or "").split("?", 1)[0].rstrip("/")
        if "/web/viewer" in clean:
            return False
        return "/web/product/" in clean

    def _is_fake_404(self, status: int, text: str, headers: dict, url: str = "", quiet: bool = False) -> bool:
        """S-Grade: Detects 'trap' 404 pages (status 200 but 404 content, or redirect to 404)."""
        content_type = headers.get('Content-Type', '').lower()
        log = logger.debug if quiet else logger.warning

        if status == 404:
            log(f"🛑 [Piccoma Identity] Hard 404 detected at {url}")
            return True

        if 'text/html' not in content_type:
            return False

        low_text = text.lower()
        indicators = ["404", "見つかりません", "not found", "error", "ご利用いただけません", "アクセス制限", "access denied"]

        if status == 200 and len(text) < 18000:
            for ind in indicators:
                if ind in low_text:
                    log(f"🛑 [Piccoma Identity] Trap Triggered: HTTP {status}, length {len(text)}, found trigger '{ind}' at {url}")
                    return True

        return False

    def _dump_diagnostic_data(self, label: str, content: str, metadata: dict = None, developer_mode: bool = True):
        """S-Grade Diagnostic: Dumps HTML/State to local files for expert analysis."""
        if not developer_mode: return
        
        try:
            import datetime
            timestamp = datetime.datetime.now().strftime("%H%M%S")
            dump_dir = os.path.join(os.getcwd(), "tmp", "piccoma_dev")
            os.makedirs(dump_dir, exist_ok=True)
            
            filename = f"{timestamp}_{label}.html"
            filepath = os.path.join(dump_dir, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            
            if metadata:
                meta_path = os.path.join(dump_dir, f"{timestamp}_{label}_meta.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    import json
                    json.dump(metadata, f, indent=4)
            
            logger.info(f"📁 [DEV-TRACE] Diagnostic dump created: tmp/piccoma_dev/{filename}")
        except Exception as e:
            logger.error(f"Failed to dump diagnostic data: {e}")

    async def _safe_request(
        self,
        session: AsyncSession,
        method: str,
        url: str,
        developer_mode: bool = True,
        trap_dump: bool = True,
        **kwargs,
    ) -> any:
        """S-Grade: Wraps request with manual redirect handling and trap detection."""
        kwargs["allow_redirects"] = False
        
        max_redirects = 5
        current_url = url
        
        for i in range(max_redirects):
            try:
                start_time = time.time()
                res = await session.request(method, current_url, **kwargs)
                elapsed = time.time() - start_time
                
                if res.status_code in [301, 302, 303, 307, 308]:
                    location = res.headers.get("Location")
                    if not location:
                        break
                    
                    if not location.startswith("http"):
                        location = urllib.parse.urljoin(current_url, location)
                    
                    logger.info(f"📡 [Piccoma Identity] Redirect {i+1}: {res.status_code} -> {location}")
                    
                    if any(trap in location.lower() for trap in ["404", "blocked", "captcha", "error", "ご利用いただけません"]):
                        logger.error(f"⚠️ [Piccoma Identity] Redirect Trap: {current_url} -> {location}")
                        raise ScraperError(f"Redirect trap detected: -> {location}")
                        
                    current_url = location
                    method = "GET"
                    kwargs.pop("data", None)
                    kwargs.pop("json", None)
                    continue
                
                if i > 0:
                    logger.debug(f"✅ [Piccoma Identity] Followed to: {current_url} ({res.status_code}, {len(res.text)} bytes, {elapsed:.2f}s)")
                
                if self._is_fake_404(res.status_code, res.text, res.headers, url=current_url, quiet=not trap_dump):
                    if developer_mode and trap_dump:
                        req_headers = dict(res.request.headers) if hasattr(res, 'request') else "N/A"
                        self._dump_diagnostic_data(f"trap_detected_{int(time.time())}", res.text, {
                            "url": current_url,
                            "status": res.status_code,
                            "headers": dict(res.headers),
                            "request_headers": req_headers
                        }, developer_mode=developer_mode)
                    raise ScraperError(f"Block/Trap page detected at {current_url}")
                    
                return res
            except ScraperError:
                raise
            except Exception as e:
                logger.warning(f"Request error on {current_url}: {e}")
                raise ScraperError(f"Network error: {e}")
                
        raise ScraperError(f"Too many redirects at {url}")

    async def run_ritual(self, session: AsyncSession, base_url: str = "https://piccoma.com") -> None:
        """S-Grade Adaptive Ritual: Performs randomized navigation to 'warm up' the session."""
        scenarios = [
            ["/", "/web/genre/comic", "/web/product/list?list_type=T&sort_type=N"], 
            ["/", "/web/search/result?word={word}"],
            ["/web/genre/smartoon", "/web/product/list?list_type=T&sort_type=H"], 
            ["/web/mypage/history", "/web/mypage/bookshelf"] 
        ]
        
        keywords = ["ファンタジー", "アクション", "令嬢", "恋愛", "異世界", "冒険"]
        scenario = random.choice(scenarios)
        
        logger.info(f"[Piccoma Identity] 🔮 Warm-up Ritual: Scenario {scenarios.index(scenario)} initiated.")
        for step in scenario:
            try:
                rel_url = step
                if "{word}" in step:
                    rel_url = step.format(word=random.choice(keywords))
                
                full_url = f"{base_url}{rel_url}"
                # Use basic request for ritual to avoid infinite recursion if safe_request calls ritual
                await session.get(full_url, allow_redirects=True, timeout=15)
                
                delay = max(2.5, random.gauss(6, 2))
                logger.debug(f"[Piccoma Identity] Ritual step complete. Pausing {delay:.2f}s...")
                await asyncio.sleep(delay)
            except Exception as e:
                logger.warning(f"⚠️ [Piccoma Identity] Ritual step '{step}' failed: {e}")
        
        logger.info("[Piccoma Identity] ✅ Session matured. Proceeding to target.")
