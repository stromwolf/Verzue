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

    def _read_csrf_cookie(self, auth_session) -> str | None:
        """csrftoken cookie value — Django expects POST header to match this."""
        try:
            jar = getattr(auth_session, "cookies", None)
            if jar is not None and hasattr(jar, "get"):
                t = jar.get("csrftoken") or jar.get("csrf_token")
                if t:
                    return str(t)
            for c in getattr(jar, "jar", []) or []:
                name = getattr(c, "name", None)
                if name and name.lower() in ("csrftoken", "csrf_token"):
                    val = getattr(c, "value", None)
                    if val:
                        return str(val)
        except Exception:
            pass
        return None

    def _csrf_token_for_requests(self, auth_session, page_csrf: str | None, middleware_csrf: str | None) -> str | None:
        """Prefer cookie over meta/page CSRF so X-Csrftoken matches what Piccoma/Django validates."""
        cookie_csrf = self._read_csrf_cookie(auth_session)
        if cookie_csrf:
            return cookie_csrf
        return page_csrf or middleware_csrf

    def _v2_xhr_headers(self, base_url: str, referer: str, csrf_token: str) -> dict:
        """Match browser HAR: fetch to same origin (Sec-Fetch-*, Sec-Ch-Ua), not navigation headers."""
        ua = self.provider.default_user_agent
        chrome_major = None
        try:
            m = re.search(r"Chrome/(\d+)\.", ua)
            if m:
                chrome_major = m.group(1)
        except Exception:
            chrome_major = None

        # In HAR, `sec-ch-ua` major matches the real UA. Some WAFs dislike mismatches.
        if chrome_major:
            sec_ch_ua = f'"Chromium";v="{chrome_major}", "Not-A.Brand";v="24", "Google Chrome";v="{chrome_major}"'
        else:
            sec_ch_ua = '"Chromium", "Not-A.Brand";v="24", "Google Chrome"'

        return {
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            # HAR sample: en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7,ar;q=0.6,es;q=0.5
            "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7,ar;q=0.6,es;q=0.5",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": base_url,
            "Referer": referer,
            "Sec-Ch-Ua": sec_ch_ua,
            "Sec-Ch-Ua-Mobile": "?0",
            'Sec-Ch-Ua-Platform': '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "x-csrftoken": csrf_token,
            "priority": "u=1, i",
        }

    async def _try_v2_point_coin_unlock(
        self,
        auth_session,
        base_url: str,
        episode_page_url: str,
        series_id: str,
        episode_id: str,
        viewer_url: str,
        csrf_token: str | None,
        wf: bool | None = None,
    ) -> bool:
        """
        Current Piccoma web unlock (from browser HAR): POST /web/user/access, then
        POST /web/v2/{coin|point|waitfree}/use/{product_id}/{episode_id} with form is_discount_campaign=N
        and header x-csrftoken. Legacy /web/episode/* routes return 404.
        """
        if not csrf_token:
            logger.debug("[Piccoma] No CSRF for v2 unlock; skipping /web/v2/.../use.")
            return False

        def hdr(csrf: str) -> dict:
            return self._v2_xhr_headers(base_url, episode_page_url, csrf)

        access_form = {
            "product_id": str(series_id),
            "episode_id": str(episode_id),
            "referrer_type": "product",
            "current_episode_id": str(episode_id),
        }
        # V2 Handshake: This call often triggers an 'Auth Kick' if the session is stale.
        # We MUST NOT swallow ScraperErrors (auth redirects) here.
        await self.provider._safe_request(
            auth_session,
            "POST",
            f"{base_url}/web/user/access",
            trap_dump=False,
            headers=dict(hdr(csrf_token)),
            data=access_form,
        )

        v2_body = {"is_discount_campaign": "N"}
        vh = self.provider._build_browser_headers(referer=episode_page_url)

        async def _post_v2(kind: str, csrf: str):
            return await self.provider._safe_request(
                auth_session,
                "POST",
                f"{base_url}/web/v2/{kind}/use/{series_id}/{episode_id}",
                trap_dump=False,
                headers=dict(hdr(csrf)),
                data=v2_body,
            )

        # Build priority list for V2 endpoints (Including "Wait-Free-More" feature)
        if wf is True:
            kinds = ["waitfree", "waitfreemore", "point", "coin"]
        elif wf is False:
            kinds = ["point", "coin", "waitfree", "waitfreemore"] 
        else:
            kinds = ["waitfree", "waitfreemore", "point", "coin"]

        for kind in kinds:
            try:
                r = await _post_v2(kind, csrf_token)
                if r.status_code in (401, 403):
                    self.provider._dump_diagnostic_data(
                        f"v2_forbidden_{kind}_{series_id}_{episode_id}",
                        r.text or "",
                        metadata={
                            "url": str(getattr(r, "url", "")),
                            "status": r.status_code,
                            "has_pksid": self.provider.helpers.session_has_pksid(auth_session),
                            "has_csrftoken": bool(self._read_csrf_cookie(auth_session)),
                            "response_headers": dict(getattr(r, "headers", {}) or {}),
                        },
                        developer_mode=True,
                    )
                if r.status_code == 403 and kind in ("point", "waitfree"):
                    logger.info(
                        f"[Piccoma] v2/{kind}/use returned 403 — refreshing episodes page to rotate csrftoken, retry once"
                    )
                    await self.provider._safe_request(auth_session, "GET", episode_page_url)
                    csrf2 = self._read_csrf_cookie(auth_session) or csrf_token
                    if csrf2 != csrf_token:
                        logger.debug("[Piccoma] CSRF cookie updated after refresh.")
                    r = await _post_v2(kind, csrf2)
                if r.status_code != 200:
                    logger.info(
                        f"[Piccoma] v2/{kind}/use HTTP {r.status_code} for {series_id}/{episode_id} "
                        f"(body prefix: {r.text[:200]!r})"
                    )
                    continue
                viewer_res = await self.provider._safe_request(
                    auth_session, "GET", viewer_url, headers=vh
                )
                if self.provider._extract_pdata_heuristic(viewer_res.text):
                    logger.info(f"✅ [Piccoma] v2/{kind}/use unlocked episode {episode_id} (viewer manifest present).")
                    return True
                logger.info(
                    f"[Piccoma] v2/{kind}/use returned 200 but viewer still has no manifest for {episode_id} "
                    f"(viewer len={len(viewer_res.text)})"
                )
            except Exception as ex:
                if "sign-in" in str(ex).lower() or "session rejected" in str(ex).lower():
                    logger.warning(f"[Piccoma] V2 {kind} unlock triggered an auth-kick. Aborting loop to heal session.")
                    raise ex
                logger.info(f"[Piccoma] v2/{kind}/use attempt failed: {ex}")
                continue
        return False

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

            # --- 🟢 IDENTITY AUDIT (avoid false positives: large pages embed "PCM-headerLogin" in JS) ---
            if self.provider.helpers.piccoma_html_indicates_guest_shell(str(res.url), res.text):
                logger.error(
                    "🛑 [Piccoma Identity] Episodes page looks like a logged-out / sign-in shell. "
                    "Session is guest or expired!"
                )
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

            csrf_any = self._csrf_token_for_requests(auth_session, csrf_token, csrf_middleware_token)
            
            # --- 🟢 Wait-Free / Type Audit ---
            wf = getattr(task, "piccoma_wait_free", None)

            if await self._try_v2_point_coin_unlock(
                auth_session, base_url, episode_page_url, series_id, episode_id, task.url, csrf_any, wf=wf
            ):
                return True

            # 2. Security Hash Calculation (legacy / wait-free discovery)
            sec_hash = self._calculate_security_hash(episode_id)

            # 3. Endpoints by chapter type (wait-free vs coin/point — do not hit wait-free URLs for pure paywall chapters)
            waitfree_paths = ["/web/episode/waitfree/use", "/web/api/v2/episode/waitfree/use"]
            coin_paths = ["/web/episode/purchase", "/web/episode/use"]
            if wf is True:
                endpoints = waitfree_paths + coin_paths
                logger.info(f"[Piccoma] Unlock strategy: wait-free chapter — trying wait-free APIs first, then coin fallbacks.")
            elif wf is False:
                endpoints = coin_paths + waitfree_paths # S-Grade Fallback: Try wait-free if coin paths fail
                logger.info(f"[Piccoma] Unlock strategy: coin/point chapter — trying coin APIs first, then wait-free fallbacks.")
            else:
                endpoints = waitfree_paths + coin_paths
                logger.info(f"[Piccoma] Unlock strategy: unknown episode flags — trying all discovery paths.")

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
                                "use_type": 1 if endpoint in waitfree_paths else None,
                                keys["csrft"]: csrf_any if "middleware" not in keys["csrft"] else csrf_middleware_token
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
                                "X-CSRF-Token": csrf_any or csrf_token or csrf_middleware_token,
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
                                        if self.provider._extract_pdata_heuristic(viewer_res.text):
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
                    f"❌ [Piccoma] Unlock failed for episode {episode_id} "
                    "(tried /web/v2/coin|point/use and legacy routes)."
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
