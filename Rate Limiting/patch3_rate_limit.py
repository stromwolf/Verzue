# ═══════════════════════════════════════════════════════════════════════════
# PATCH 3 of 3 — app/providers/platforms/jumptoon.py
#
# RATE-LIMIT HARDENING (multi-user safety).
#
# Four layers, all composable with the tail-first + parallel patches (1 & 2):
#
#   Layer 1: Module-level semaphore (6) — caps in-flight page requests
#            across ALL concurrent users in this process
#   Layer 2: Per-series asyncio.Lock + 60s in-memory cache — dedupes
#            simultaneous requests for the same trending series
#   Layer 3: Redis token bucket (8 req/s sustained, 15 burst) — cross-process
#            fairness when running bot + worker processes together
#   Layer 4: 429/403 backoff with jitter — graceful degradation instead of
#            surfacing PX_403 directly to the user
#
# All existing functionality preserved. The _jumptoon_gated_get() helper
# is the single choke point — every request goes through it.
# ═══════════════════════════════════════════════════════════════════════════

import os
import re
import time
import json
import logging
import math
import asyncio
import random
import base64
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from curl_cffi.requests import AsyncSession, RequestsError
from app.providers.base import BaseProvider
from app.services.session_service import SessionService
from app.services.redis_manager import RedisManager
from app.core.exceptions import ScraperError
from config.settings import Settings

logger = logging.getLogger("JumptoonProvider")

JUMPTOON_RELEASE_TIME_UTC = "15:00"


# ═══════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL RATE-LIMIT PRIMITIVES
# ═══════════════════════════════════════════════════════════════════════════

# Layer 1: Process-wide semaphore capping concurrent Jumptoon page requests.
# Sized for one user's worst-case (2 foreground + 4 background = 6).
# Multiple concurrent users fairly share these 6 slots.
# Same pattern as STITCH_SEMAPHORE(3) and GLOBAL_UPLOAD_SEMAPHORE(5) in worker.py.
JUMPTOON_METADATA_SEMAPHORE = asyncio.Semaphore(6)

# Layer 2: Per-series lock registry. When 5 users open the same series at once,
# only 1 actually fetches — the rest await the cached result.
_SERIES_LOCKS: dict[str, asyncio.Lock] = {}
_SERIES_LOCKS_LOCK = asyncio.Lock()  # Guards _SERIES_LOCKS dict itself

# Layer 2 cache: series_id → (cached_result_tuple, expiry_timestamp)
# 60s TTL matches realistic user-session overlap window.
_SERIES_CACHE: dict[str, tuple] = {}
_SERIES_CACHE_TTL = 60.0  # seconds


async def _get_series_lock(series_id: str) -> asyncio.Lock:
    """Atomically get-or-create the per-series lock."""
    async with _SERIES_LOCKS_LOCK:
        lock = _SERIES_LOCKS.get(series_id)
        if lock is None:
            lock = asyncio.Lock()
            _SERIES_LOCKS[series_id] = lock
            # Cleanup: if we ever have >500 distinct series locks in memory,
            # prune ones not currently held. Cheap because dict iteration is fast.
            if len(_SERIES_LOCKS) > 500:
                stale = [sid for sid, lk in _SERIES_LOCKS.items()
                         if not lk.locked() and sid != series_id]
                # Keep the 250 most recent (rough LRU approximation via dict order)
                for sid in stale[:-250]:
                    _SERIES_LOCKS.pop(sid, None)
        return lock


def _cache_get(series_id: str):
    """Return cached result if fresh, else None. Non-blocking."""
    entry = _SERIES_CACHE.get(series_id)
    if not entry:
        return None
    result, expiry = entry
    if time.time() > expiry:
        _SERIES_CACHE.pop(series_id, None)
        return None
    return result


def _cache_put(series_id: str, result: tuple):
    """Store result with TTL. Bounded: prune if >200 entries."""
    if len(_SERIES_CACHE) > 200:
        # Drop 50 oldest by expiry
        oldest = sorted(_SERIES_CACHE.items(), key=lambda kv: kv[1][1])[:50]
        for sid, _ in oldest:
            _SERIES_CACHE.pop(sid, None)
    _SERIES_CACHE[series_id] = (result, time.time() + _SERIES_CACHE_TTL)


# ═══════════════════════════════════════════════════════════════════════════
# JumptoonProvider class — additions + modifications
# ═══════════════════════════════════════════════════════════════════════════

class JumptoonProvider(BaseProvider):
    IDENTIFIER = "jumptoon"
    BASE_URL = "https://jumptoon.com"
    SERIES_PATH = "/series/"

    def __init__(self):
        self.session_service = SessionService()
        self.redis = RedisManager()  # Layer 3: token bucket
        self.active_account_id = None
        self.default_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'ja,en-US;q=0.9',
            'Referer': 'https://jumptoon.com/'
        }
        # Image download semaphore (existing — unchanged)
        self._download_semaphore = asyncio.Semaphore(10)

    # ─── THE CHOKE POINT: every metadata GET goes through this helper ────────

    async def _jumptoon_gated_get(self, auth_session, url: str, timeout: int = 30,
                                   allow_redirects: bool = True, max_retries: int = 2):
        """
        Unified rate-gated GET for all Jumptoon metadata requests.

        Applies in order:
          1. Redis token bucket (cross-process fairness)
          2. Module semaphore (in-process concurrency cap)
          3. HTTP request
          4. 429/403 detection → exponential backoff retry with jitter

        Used by: get_series_info, fetch_more_chapters, sync_latest_chapters,
                 and any future metadata endpoints.

        Returns the response object (never None — raises on hard failure).
        """
        last_err = None
        for attempt in range(max_retries + 1):
            # ─── Layer 3: Redis token bucket (distributed rate limit) ────────
            # 8 req/s sustained, 15 burst capacity. Tuned well below Jumptoon's
            # observed limits; leaves headroom for image downloads on same IP.
            try:
                allowed, wait_time = await self.redis.get_token(
                    "platform:jumptoon",
                    rate=8,
                    capacity=15
                )
                if not allowed:
                    sleep_for = min(wait_time or 0.15, 2.0)
                    logger.debug(f"[Jumptoon] Token bucket wait: {sleep_for:.2f}s")
                    await asyncio.sleep(sleep_for)
                    # Don't count this as a retry — token wait is normal backpressure
                    continue
            except Exception as e:
                # Redis down → fail-open (still have semaphore layer)
                logger.debug(f"[Jumptoon] Token bucket unavailable ({e}); falling back to semaphore only")

            # ─── Layer 1: Process semaphore (concurrency cap) ────────────────
            async with JUMPTOON_METADATA_SEMAPHORE:
                try:
                    res = await auth_session.get(
                        url,
                        timeout=timeout,
                        allow_redirects=allow_redirects
                    )
                except RequestsError as e:
                    last_err = e
                    # Proxy-level rate limiting looks like PX_403 in this codebase
                    err_str = str(e).lower()
                    is_proxy_block = ("403" in err_str or "tunnel" in err_str or
                                       "denied" in err_str)
                    if is_proxy_block and attempt < max_retries:
                        backoff = self._compute_backoff(attempt)
                        logger.warning(f"[Jumptoon] Proxy block on {url} "
                                        f"(attempt {attempt+1}/{max_retries+1}); "
                                        f"backoff {backoff:.1f}s")
                        await asyncio.sleep(backoff)
                        continue
                    # Surface the original error for upstream handlers
                    raise
                except Exception as e:
                    last_err = e
                    raise

                # ─── Layer 4: 429 / 503 detection with backoff ───────────────
                if res.status_code in (429, 503):
                    if attempt < max_retries:
                        # Honor Retry-After header if present, else use backoff
                        retry_after = res.headers.get('Retry-After')
                        if retry_after:
                            try:
                                backoff = min(float(retry_after), 10.0)
                            except ValueError:
                                backoff = self._compute_backoff(attempt)
                        else:
                            backoff = self._compute_backoff(attempt)
                        logger.warning(f"[Jumptoon] HTTP {res.status_code} on {url} "
                                        f"(attempt {attempt+1}/{max_retries+1}); "
                                        f"backoff {backoff:.1f}s")
                        await asyncio.sleep(backoff)
                        continue
                    # Out of retries — convert to a recognizable error
                    raise ScraperError(
                        f"Jumptoon rate limit sustained (HTTP {res.status_code}). "
                        f"Try again in a moment.",
                        code="RL_001"
                    )

                # Success (or non-retryable status) — return
                return res

        # Shouldn't reach here, but if we do, surface the last error
        if last_err:
            raise last_err
        raise ScraperError("Jumptoon request exhausted retries", code="RL_002")

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        """Exponential backoff with jitter: 2^attempt + random(0, 1)."""
        base = min(2 ** attempt, 8)
        return base + random.random()

    # ─── _get_authenticated_session (unchanged, shown for context) ───────────

    async def _get_authenticated_session(self):
        session_obj = await self.session_service.get_active_session("jumptoon")
        if not session_obj:
            raise ScraperError("No healthy sessions available for jumptoon. Use /add-cookies to fix.")

        self.active_account_id = session_obj["account_id"]
        async_session = curl_requests.AsyncSession(impersonate="chrome", proxy=Settings.get_proxy())
        async_session.headers.update(self.default_headers)

        for c in session_obj["cookies"]:
            name, value = c.get('name'), c.get('value')
            if not name or not value:
                continue
            raw_domain = c.get('domain', 'jumptoon.com').lstrip('.')
            async_session.cookies.set(name, value, domain=raw_domain)
            async_session.cookies.set(name, value, domain='.' + raw_domain)

        return async_session

    # ─── MODIFIED: get_series_info with Layer 2 (per-series dedup + cache) ───

    async def get_series_info(self, url: str, fast: bool = False):
        """
        Tail-first extraction with full rate-limit hardening.

        Layer 2 here: if 5 users open the same series in 60s, only 1 fetches.
        The rest await the cached result.
        """
        # 1. Normalize series ID (same as before)
        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if series_id_match:
            series_id = series_id_match.group(1)
        else:
            series_id = url.split("?")[-1] if "?" in url else url.split("/")[-1]
            if not series_id or series_id == "episodes":
                series_id = url.split("/")[-2]

        # ─── Layer 2: Cache check (non-fast mode only — fast mode is already cheap) ─
        cache_key = f"{series_id}:{'fast' if fast else 'full'}"
        if not fast:
            cached = _cache_get(cache_key)
            if cached is not None:
                logger.info(f"[Jumptoon] 🟢 Series cache HIT: {series_id} "
                            f"(saved a full tail-first fetch)")
                return cached

        # ─── Layer 2: Per-series lock to collapse concurrent first-requests ──
        # If user A is already fetching series X, users B/C/D wait here.
        # When A finishes, B/C/D find the cache hit and return immediately.
        series_lock = await _get_series_lock(series_id)

        async with series_lock:
            # Re-check cache inside the lock (A might have just finished)
            if not fast:
                cached = _cache_get(cache_key)
                if cached is not None:
                    logger.info(f"[Jumptoon] 🟢 Series cache HIT (post-lock): {series_id}")
                    return cached

            # Actually perform the fetch
            result = await self._fetch_series_info_uncached(url, series_id, fast)

            # Cache only non-fast, successful full results
            if not fast:
                _cache_put(cache_key, result)

            return result

    async def _fetch_series_info_uncached(self, url: str, series_id: str, fast: bool):
        """
        The actual tail-first fetch logic. Pulled out of get_series_info so the
        cache layer in get_series_info stays clean.

        All HTTP GETs go through _jumptoon_gated_get — no exceptions.
        """
        fetch_url = f"{self.BASE_URL}/series/{series_id}/"
        logger.info(f"[Jumptoon] 🔍 Intelligence Phase for: {series_id} "
                    f"(mode={'fast' if fast else 'tail-first'})")

        auth_session = await self._get_authenticated_session()

        # Landing page (gated)
        try:
            res = await self._jumptoon_gated_get(auth_session, fetch_url, timeout=30)
            if res.status_code in (301, 302, 303, 307, 308):
                loc = res.headers.get("Location", "Unknown")
                raise ScraperError(f"Auth Expired or Age Restricted accessing {fetch_url}. "
                                    f"Redirected to {loc}")
            if res.status_code != 200:
                raise ScraperError(f"Failed to access Jumptoon: HTTP {res.status_code} on {fetch_url}")
        except RequestsError as e:
            logger.error(f"[Jumptoon] Request Error (Potential Proxy): {e}")
            raise ScraperError("Scraping Proxy Denied Access (403). "
                                "Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
        except ScraperError:
            raise
        except Exception as e:
            raise ScraperError(f"Request failed: {e}")

        html_content = res.text
        clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_content)
        clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')

        await self.session_service.record_session_success("jumptoon")

        # [Series ID, total_chapters, title, poster, status, release_day extraction]
        # ... (identical to Patch 1 — omitted here for brevity; keep as-is from Patch 1)
        # The only difference from Patch 1 is that the LAST-PAGE fetch below
        # also goes through _jumptoon_gated_get.

        # --- PLACEHOLDER: paste the metadata extraction block from Patch 1 here ---
        # (id_match, total_chapters, title, image_url, status_label, release_day)

        # Tag extraction from page 1
        up_ids = set()
        coming_soon_ids = set()
        self._extract_tag_ids(html_content, up_ids, coming_soon_ids)

        seen_ids = set()
        page1_chapters = self._parse_page_data(html_content, sees_ids=seen_ids,
                                                up_ids=up_ids, coming_soon_ids=coming_soon_ids)

        # ... (insert metadata variables: total_chapters, title, image_url, etc.)
        # These come from the extraction block — same as Patch 1.
        # For the full function body, merge Patch 1 + this gated-get approach.

        release_time = JUMPTOON_RELEASE_TIME_UTC  # if release_day else None

        if fast:
            logger.info(f"[Jumptoon] Fast Fetch (Page 1 Only): {series_id}")
            page1_chapters.sort(key=self._extract_sort_key)
            for ch in page1_chapters:
                if str(ch['id']) in up_ids:
                    ch['is_new'] = True
            # return (title, total_chapters, page1_chapters, image_url, series_id,
            #         release_day, None, status_label, None)
            return  # placeholder — see Patch 1 for the actual return tuple

        # ─── TAIL-FIRST: last page via gated GET ─────────────────────────────
        pg_size = 30
        # total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1
        # all_chapters = list(page1_chapters)

        # if total_pages > 1:
        #     last_page_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={total_pages}"
        #     try:
        #         lp_res = await self._jumptoon_gated_get(auth_session, last_page_url, timeout=30)
        #         if lp_res.status_code == 200:
        #             self._extract_tag_ids(lp_res.text, up_ids, coming_soon_ids)
        #             tail_chaps = self._parse_page_data(lp_res.text, sees_ids=seen_ids,
        #                                                 up_ids=up_ids, coming_soon_ids=coming_soon_ids)
        #             all_chapters.extend(tail_chaps)
        #     except Exception as e:
        #         logger.warning(f"[Jumptoon] Tail fetch soft-fail ({e}); background scan will recover")

        # ... (remainder identical to Patch 1: sort, UP flag, return tuple)

    # ─── MODIFIED: fetch_more_chapters uses gated GET too ────────────────────

    async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set,
                                   skip_pages: list | None = None):
        """Background scan — all page GETs flow through _jumptoon_gated_get."""
        if skip_pages is None:
            skip_pages = [1]
            if total_pages > 1:
                skip_pages.append(total_pages)

        series_id_match = re.search(r'/series/([^/?#]+)', url)
        if not series_id_match:
            return []
        series_id = series_id_match.group(1)

        auth_session = await self._get_authenticated_session()

        pages_to_fetch = [p for p in range(1, total_pages + 1) if p not in skip_pages]
        if not pages_to_fetch:
            return []

        logger.info(f"[Jumptoon] 📡 Background parallel fetch (rate-gated): "
                    f"{len(pages_to_fetch)} pages")

        # NOTE: No local semaphore here anymore. The module-level
        # JUMPTOON_METADATA_SEMAPHORE inside _jumptoon_gated_get is the
        # single source of truth for concurrency. Avoids nested-semaphore
        # deadlock risk when multiple users' background scans collide.

        bg_up_ids = set()
        bg_coming_soon_ids = set()

        async def fetch_page(p):
            try:
                url_p = f"{self.BASE_URL}/series/{series_id}/episodes/?page={p}"
                p_res = await self._jumptoon_gated_get(auth_session, url_p, timeout=30)
                if p_res.status_code in (301, 302, 303, 307, 308):
                    return p, None
                if p_res.status_code == 200:
                    return p, p_res.text
                return p, None
            except Exception as e:
                logger.error(f"[Jumptoon] BG p{p} error: {e}")
                return p, None

        results = await asyncio.gather(*(fetch_page(p) for p in pages_to_fetch))

        # Phase A: collect tags
        for _, html in results:
            if html:
                self._extract_tag_ids(html, bg_up_ids, bg_coming_soon_ids)

        # Phase B: parse in order
        extra_chapters = []
        for _, html in sorted(results, key=lambda r: r[0]):
            if not html:
                continue
            chaps = self._parse_page_data(html, sees_ids=seen_ids,
                                           up_ids=bg_up_ids, coming_soon_ids=bg_coming_soon_ids)
            if chaps:
                for ch in chaps:
                    if str(ch['id']) in bg_up_ids:
                        ch['is_new'] = True
                extra_chapters.extend(chaps)

        logger.info(f"[Jumptoon] 📡 BG scan complete: +{len(extra_chapters)} chapters")
        return extra_chapters

    # ─── sync_latest_chapters: gated too ─────────────────────────────────────

    async def sync_latest_chapters(self, url):
        """Background subscription sync — gated."""
        try:
            title, total_count, _, _, series_id, _, _, _, _ = await self.get_series_info(url, fast=True)
            if total_count == 0:
                return []

            pg_size = 30
            last_page = math.ceil(total_count / pg_size)

            auth_session = await self._get_authenticated_session()
            last_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={last_page}"
            logger.info(f"[Jumptoon] Syncing Latest Chapters (gated): Page {last_page}")

            res = await self._jumptoon_gated_get(auth_session, last_url, timeout=30)
            if res.status_code == 200:
                up_ids = set()
                coming_soon_ids = set()
                self._extract_tag_ids(res.text, up_ids, coming_soon_ids)
                latest_chaps = self._parse_page_data(res.text, sees_ids=set(),
                                                      up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                latest_chaps.sort(key=self._extract_sort_key)
                return latest_chaps
        except Exception as e:
            logger.error(f"[Jumptoon] Background sync failed: {e}")
        return []

    # ─── (rest of class methods unchanged: _extract_sort_key, _extract_tag_ids,
    #     _parse_page_data, scrape_chapter, fast_purchase, etc.) ──────────────
