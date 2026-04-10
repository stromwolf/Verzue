import re
import json
import logging
import hashlib
import asyncio
import urllib.parse
from bs4 import BeautifulSoup
from app.core.exceptions import ScraperError

logger = logging.getLogger("PiccomaProvider.Purchase")

class PiccomaPurchase:
    """
    Extracted Purchasing and Discovery Matrix logic for Piccoma.
    """
    def __init__(self, provider):
        self.provider = provider

    async def fast_purchase(self, task) -> bool:
        """
        S-Grade Unified Purchase: Detects and handles Coin, Point, and Wait-Free chapters.
        Implements human-like 'Ritual' warm-up and automated redirect trap detection.
        Follows the 'Discovery Loop' architecture for maximum resilience.
        """
        match = re.search(r'/web/viewer/(?:s/)?(\d+)/(\d+)', task.url)
        if not match: return False
            
        series_id, episode_id = match.groups()
        base_url, region, domain = self.provider._get_context_from_url(task.url)
        auth_session = await self.provider._get_authenticated_session(domain)
        
        logger.info(f"[Piccoma Identity] 🚀 Chapter Handoff: {series_id}/{episode_id} | Session: {domain}")
        await self.provider.run_ritual(auth_session)

        try:
            # 1. Fetch Landing Page for CSRF/Audit
            episode_page_url = f"{base_url}/web/product/{series_id}/episodes?etype=E"
            res = await self.provider._safe_request(auth_session, "GET", episode_page_url)
            soup = BeautifulSoup(res.text, 'html.parser')
            
            # --- 🟢 IDENTITY AUDIT ---
            is_guest = bool(soup.select_one('.PCM-headerLogin, a[href*="/acc/signin"]'))
            if is_guest:
                logger.error("🛑 [Piccoma Identity] Browser shows LOGIN button. Session is guest or expired!")
                try:
                    if hasattr(self.provider.session_service, "record_session_failure"):
                        await self.provider.session_service.record_session_failure("piccoma")
                    elif hasattr(self.provider.session_service, "report_session_failure"):
                        active = await self.provider.session_service.get_active_session("piccoma")
                        if active and active.get("account_id"):
                            await self.provider.session_service.report_session_failure(
                                "piccoma",
                                active.get("account_id"),
                                reason="Purchase flow saw login page"
                            )
                except Exception as e:
                    logger.warning(f"[Piccoma][DEV-TRACE] Failed to report purchase-session failure: {e}")
                return False

            # --- CSRF EXTRACTION (Multi-Source Search) ---
            csrf_token = None
            csrf_middleware_token = None
            
            # Meta tags
            meta_csrf = soup.select_one('meta[name="csrf-token"]')
            if meta_csrf: csrf_token = meta_csrf.get('content')
            
            # Hidden inputs (Common for Django/Django REST)
            csrf_input = soup.select_one('input[name="csrfmiddlewaretoken"]')
            if csrf_input: csrf_middleware_token = csrf_input.get('value')
            
            # Next.JS props
            next_data_script = soup.select_one('script#__NEXT_DATA__')
            if next_data_script:
                try:
                    n_data = json.loads(next_data_script.string)
                    csrf_token = csrf_token or n_data.get('props', {}).get('pageProps', {}).get('csrfToken')
                except: pass

            # 2. Security Hash Calculation
            sec_hash = self._calculate_security_hash(episode_id)

            # 3. Endpoints by chapter type (wait-free vs coin/point — do not hit wait-free URLs for pure paywall chapters)
            wf = getattr(task, "piccoma_wait_free", None)
            waitfree_paths = ["/web/episode/waitfree/use", "/web/api/v2/episode/waitfree/use"]
            coin_paths = ["/web/episode/purchase", "/web/episode/use"]
            if wf is True:
                endpoints = waitfree_paths + coin_paths
                logger.info(f"[Piccoma] Unlock strategy: wait-free chapter — trying wait-free APIs first, then coin fallbacks.")
            elif wf is False:
                endpoints = coin_paths
                logger.info(f"[Piccoma] Unlock strategy: coin/point chapter — skipping wait-free-only API paths.")
            else:
                endpoints = coin_paths + waitfree_paths
                logger.info(f"[Piccoma] Unlock strategy: unknown episode flags — coin paths first, then wait-free.")

            # 4. Discovery Matrix: Multi-endpoint, Multi-encoding, Multi-keys
            # -----------------------------------------------------------
            encodings = ["JSON", "FORM"]
            key_sets = [
                {"ep": "episodeId", "prod": "productId", "csrft": "csrfToken"},
                {"ep": "episode_id", "prod": "product_id", "csrft": "csrfmiddlewaretoken"}
            ]

            for endpoint in endpoints:
                purchase_url = f"{base_url}{endpoint}"
                for encoding in encodings:
                    for keys in key_sets:
                        try:
                            # Build the specific payload for this trial
                            payload = {
                                keys["ep"]: int(episode_id),
                                keys["prod"]: int(series_id),
                                "hash": sec_hash,
                                keys["csrft"]: csrf_token if "middleware" not in keys["csrft"] else csrf_middleware_token
                            }
                            # Cleanup: don't send None
                            payload = {k: v for k, v in payload.items() if v is not None}
                            
                            headers = self.provider._build_browser_headers(referer=episode_page_url)
                            if encoding == "JSON":
                                headers["Content-Type"] = "application/json"
                                headers["Accept"] = "application/json"
                                kwargs = {"json": payload}
                            else:
                                headers["Content-Type"] = "application/x-www-form-urlencoded"
                                kwargs = {"data": payload}
                                
                            headers.update({
                                "X-CSRF-Token": csrf_token or csrf_middleware_token,
                                "X-Requested-With": "XMLHttpRequest",
                                "X-Security-Hash": sec_hash,
                                "X-Hash-Code": sec_hash
                            })
                            
                            logger.debug(f"🔍 [Piccoma Discovery] Trial: {endpoint} ({encoding}, {keys['ep']})")
                            purchase_res = await self.provider._safe_request(
                                auth_session, "POST", purchase_url, trap_dump=False, headers=headers, **kwargs
                            )
                            
                            # Success verification Part 1: API Response
                            if purchase_res.status_code == 200:
                                try:
                                    res_json = purchase_res.json()
                                    if res_json.get("result") == "ok":
                                        logger.info(f"✨ [Piccoma API] Discovery loop found valid endpoint: {endpoint} ({encoding})")
                                        # Verification Part 2: Final Manifest Check (Mandatory)
                                        viewer_res = await self.provider._safe_request(auth_session, "GET", task.url)
                                        if self.provider._extract_pdata(viewer_res.text):
                                            logger.info(f"✅ [Piccoma] Discovery Matrix Success! Chapter {episode_id} is functionally UNLOCKED.")
                                            return True
                                except: pass
                        except Exception: continue

            if wf is True:
                logger.error(
                    f"❌ [Piccoma] No working wait-free (or fallback) unlock path for episode {episode_id}; "
                    "Piccoma may have changed API routes."
                )
            elif wf is False:
                logger.error(
                    f"❌ [Piccoma] No coin/purchase API path for episode {episode_id}; "
                    "legacy /web/episode/purchase and /web/episode/use return 404 — unlock may require an updated endpoint."
                )
            else:
                logger.error(f"❌ [Piccoma] Discovery matrix exhausted for episode {episode_id}.")
            return False

        except Exception as e:
            logger.error(f"[Piccoma] Error in fast_purchase: {e}")
            return False

    def _calculate_security_hash(self, episode_id: str) -> str:
        """
        X-Security-Hash / X-Hash-Code for purchase and wait-free API calls.
        See docs/piccoma_wait_free_unlock.md (sha256(episode_id + salt), hex digest).
        """
        seed = f"{episode_id}fh_SpJ#a4LuNa6t8"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
