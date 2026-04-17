# ═══════════════════════════════════════════════════════════════════════════
# JUMPTOON CHAPTER LIST SCRAPING — OPTIMIZATION PATCH
# File: app/providers/platforms/jumptoon.py
#
# Strategy:
#   Tier 1: Parallel pagination (gather pages 2..N concurrently)
#   Tier 2: Last-page-first for polling (Mecha-parity)
#   Tier 3: Redis-backed ETag cache (skip crawl if nothing changed)
#
# Preserves: UP tags, coming-soon filter, sort keys, release_day, status,
#            base64 ID decoding, auth-redirect handling, telemetry hooks.
# ═══════════════════════════════════════════════════════════════════════════


# ─── Helper 1: Extract metadata (title, total, poster, status, release_day) ───
# Pulled out of get_series_info to keep the main function flat and reusable
# across fast/full/poll modes.

def _extract_series_metadata(self, html_content: str, series_id: str):
    """
    Pure function: parse the landing page HTML once and return all series-level
    metadata. No network calls. Idempotent. Used by every code path.
    """
    clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_content)
    clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')

    # Series ID (HTML-reported wins over URL-derived)
    id_match = re.search(r'"seriesId"\s*:\s*"([^"]+)"', clean_html)
    if id_match:
        series_id = id_match.group(1)

    # Total chapter count — try structured fields first, fall back to H2 scrape
    total_chapters = 0
    for pattern in (
        r'"totalEpisodeCount"\s*:\s*"(\d+)"',
        r'"totalEpisodeCount"\s*:\s*(\d+)',
        r'"totalCount"\s*:\s*"(\d+)"',
        r'"totalCount"\s*:\s*(\d+)',
    ):
        m = re.search(pattern, clean_html)
        if m:
            total_chapters = int(m.group(1))
            break
    if total_chapters == 0:
        h2 = re.search(r'<h2[^>]*>全\s*(?:<!--.*?-->\s*)*(\d+)\s*(?:<!--.*?-->\s*)*話</h2>',
                       html_content, re.DOTALL)
        if h2:
            total_chapters = int(h2.group(1))

    # Title — JSON-first, HTML fallback
    title = series_id
    series_match = re.search(r'"series"\s*:\s*\{', clean_html)
    if series_match:
        window = clean_html[series_match.end(): series_match.end() + 2000]
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', window)
        if name_match:
            title = name_match.group(1)
    try:
        if "\\u" in title:
            title = title.encode('utf-8').decode('unicode_escape')
    except Exception:
        pass
    title = title.replace('&amp;', '&').strip()
    if not title or title == series_id:
        h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html_content)
        if h1:
            title = BeautifulSoup(h1.group(1), "html.parser").get_text().strip()
        if not title or title == series_id:
            t = re.search(r'<title>(.*?)</title>', html_content, re.I)
            if t:
                title = t.group(1).strip().split('|')[0].strip().split(' | ')[0].strip()

    # Poster
    image_url = None
    img_match = re.search(
        r'"(?:thumbnailUrl|posterUrl|coverUrl|image)"\s*:\s*"(https://assets\.jumptoon\.com/[^"]+)"',
        clean_html
    )
    if img_match:
        image_url = img_match.group(1).split('?')[0]
    if not image_url:
        og = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)', html_content)
        if og:
            image_url = og.group(1).split('?')[0]

    # Status + release day
    status_label = None
    if "読切" in html_content:
        status_label = "Oneshot"
    elif "完結" in html_content:
        status_label = "Completed"

    release_day = None
    day_match = re.search(r'"publishDayNames"\s*:\s*\["([^"]+)"\]', clean_html)
    if day_match:
        release_day = day_match.group(1).capitalize()

    return {
        'series_id': series_id,
        'title': title,
        'total_chapters': total_chapters,
        'image_url': image_url,
        'status_label': status_label,
        'release_day': release_day,
        'clean_html': clean_html,
    }


# ─── Helper 2: Parallel page crawl with shared state ──────────────────────────

async def _crawl_pages_parallel(self, auth_session, series_id: str, pages: list[int],
                                 seen_ids: set, up_ids: set, coming_soon_ids: set):
    """
    Fetch N pages concurrently and merge results. Returns a flat list of
    chapter dicts. Mutates the shared sets (up_ids, coming_soon_ids, seen_ids).

    Uses a bounded semaphore (4) to avoid hammering Jumptoon's edge or
    triggering rate-limit heuristics on the scraping proxy.
    """
    if not pages:
        return []

    sem = asyncio.Semaphore(4)  # tuned for proxy fairness; Mecha uses similar bounds

    async def fetch_one(page_num: int):
        async with sem:
            url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={page_num}"
            try:
                res = await auth_session.get(url, timeout=30, allow_redirects=True)
            except RequestsError as e:
                logger.error(f"[Jumptoon] Parallel fetch network error on p{page_num}: {e}")
                return page_num, None
            except Exception as e:
                logger.error(f"[Jumptoon] Parallel fetch error on p{page_num}: {e}")
                return page_num, None

            if res.status_code in (301, 302, 303, 307, 308):
                loc = res.headers.get("Location", "Unknown")
                logger.warning(f"[Jumptoon] p{page_num} redirected to {loc} — auth may be expired")
                return page_num, None
            if res.status_code != 200:
                return page_num, None
            return page_num, res.text

    # Fire all requests concurrently, preserving page order in results
    results = await asyncio.gather(*(fetch_one(p) for p in pages))

    # Phase A: accumulate UP/coming-soon tags across all pages FIRST
    # This matters because _parse_page_data consults up_ids/coming_soon_ids
    # to decide filtering — so tags must be complete before parsing.
    for page_num, html in results:
        if html:
            self._extract_tag_ids(html, up_ids, coming_soon_ids)

    # Phase B: parse chapter data in page order (preserves deterministic output)
    collected = []
    for page_num, html in sorted(results, key=lambda r: r[0]):
        if not html:
            continue
        chaps = self._parse_page_data(html, sees_ids=seen_ids,
                                       up_ids=up_ids, coming_soon_ids=coming_soon_ids)
        collected.extend(chaps)
    return collected


# ─── Helper 3: Redis ETag cache for skip-crawl optimization ───────────────────

async def _cache_fingerprint(self, series_id: str):
    """Returns (total_chapters, latest_id) from Redis, or (None, None) if cold."""
    try:
        redis = self.bot.redis_brain.client if hasattr(self, 'bot') else None
        if not redis:
            # Fallback: access via session_service's redis if available
            redis = getattr(self.session_service, 'redis_client', None)
        if not redis:
            return None, None
        key = f"verzue:jumptoon:fp:{series_id}"
        data = await redis.hgetall(key)
        if not data:
            return None, None
        total = data.get(b'total') or data.get('total')
        latest = data.get(b'latest') or data.get('latest')
        return int(total) if total else None, (latest.decode() if isinstance(latest, bytes) else latest)
    except Exception as e:
        logger.debug(f"[Jumptoon] Cache read miss: {e}")
        return None, None


async def _cache_write(self, series_id: str, total: int, latest_id: str):
    """Write fingerprint with 7-day TTL (series metadata is stable)."""
    try:
        redis = self.bot.redis_brain.client if hasattr(self, 'bot') else None
        if not redis:
            redis = getattr(self.session_service, 'redis_client', None)
        if not redis:
            return
        key = f"verzue:jumptoon:fp:{series_id}"
        await redis.hset(key, mapping={'total': str(total), 'latest': str(latest_id)})
        await redis.expire(key, 604800)  # 7 days
    except Exception as e:
        logger.debug(f"[Jumptoon] Cache write miss: {e}")


# ─── Replacement: get_series_info (the main entry point) ──────────────────────

async def get_series_info(self, url: str, fast: bool = False, poll: bool = False):
    """
    Phase 1: Intelligence extraction.

    Modes:
      fast=True  → page 1 only (instant UI, ~1 request)
      poll=True  → page 1 + last page only (Mecha-parity, ~2 requests)
                   Uses Redis fingerprint to short-circuit if unchanged.
      default   → full crawl, pages 2..N in PARALLEL (1 + ceil(N/4) round-trips)
    """
    # 1. Normalize series ID from URL
    m = re.search(r'/series/([^/?#]+)', url)
    if m:
        series_id = m.group(1)
    else:
        series_id = url.split("?")[-1] if "?" in url else url.split("/")[-1]
        if not series_id or series_id == "episodes":
            series_id = url.split("/")[-2]

    fetch_url = f"{self.BASE_URL}/series/{series_id}/"
    logger.info(f"[Jumptoon] 🔍 Intelligence Phase: {series_id} "
                f"(mode={'fast' if fast else 'poll' if poll else 'full'})")

    # 2. Fetch landing page (required for every mode)
    auth_session = await self._get_authenticated_session()
    try:
        res = await auth_session.get(fetch_url, timeout=30, allow_redirects=True)
        if res.status_code in (301, 302, 303, 307, 308):
            loc = res.headers.get("Location", "Unknown")
            raise ScraperError(f"Auth Expired or Age Restricted. Redirected to {loc}")
        if res.status_code != 200:
            raise ScraperError(f"Failed to access Jumptoon: HTTP {res.status_code}")
    except RequestsError as e:
        logger.error(f"[Jumptoon] Request Error (Potential Proxy): {e}")
        raise ScraperError("Scraping Proxy Denied Access (403). "
                           "Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
    except Exception as e:
        if "ScraperError" in type(e).__name__:
            raise
        raise ScraperError(f"Request failed: {e}")

    await self.session_service.record_session_success("jumptoon")

    # 3. Extract all metadata in one pass
    meta = self._extract_series_metadata(res.text, series_id)
    series_id = meta['series_id']
    title = meta['title']
    total_chapters = meta['total_chapters']
    image_url = meta['image_url']
    status_label = meta['status_label']
    release_day = meta['release_day']
    clean_html = meta['clean_html']
    html_content = res.text

    # 4. Extract tags from page 1 (shared across all modes)
    up_ids = set()
    coming_soon_ids = set()
    self._extract_tag_ids(html_content, up_ids, coming_soon_ids)

    # 5. Parse page 1 chapters (used by fast + poll + full)
    seen_ids = set()
    page1_chapters = self._parse_page_data(html_content, sees_ids=seen_ids,
                                            up_ids=up_ids, coming_soon_ids=coming_soon_ids)

    release_time = JUMPTOON_RELEASE_TIME_UTC if release_day else None

    # ─── FAST MODE: return page 1 only ────────────────────────────────────────
    if fast:
        logger.info(f"[Jumptoon] Fast Fetch (Page 1): {title} ({series_id})")
        return (title, total_chapters, page1_chapters, image_url, series_id,
                release_day, None, status_label, None)

    # 5.5. Compute pagination
    pg_size = 30
    total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1

    # ─── POLL MODE: page 1 + last page only, with Redis skip-check ────────────
    if poll:
        # Cheap path: if fingerprint unchanged, don't even fetch the last page.
        # Jumptoon shows the newest episode ID in the page-1 hydrated JSON,
        # so page 1 alone tells us if something changed.
        cached_total, cached_latest = await self._cache_fingerprint(series_id)

        # Find the newest chapter ID we've seen on page 1
        p1_latest = None
        if page1_chapters:
            # Same sort key used for final output — keeps comparison consistent
            p1_latest = max(page1_chapters, key=self._extract_sort_key).get('id')

        if (cached_total == total_chapters and cached_latest and cached_latest == str(p1_latest)):
            logger.info(f"[Jumptoon] 🟢 Cache HIT: {title} unchanged "
                        f"(total={total_chapters}, latest={p1_latest}) — skipping last page")
            # Still need to return sorted chapters; page 1 is enough for poller
            # since it only reads chapter_list[-1]
            page1_chapters.sort(key=self._extract_sort_key)
            if page1_chapters:
                for ch in page1_chapters:
                    if str(ch['id']) in up_ids:
                        ch['is_new'] = True
            return (title, total_chapters, page1_chapters, image_url, series_id,
                    release_day, release_time, status_label, None)

        # Cache miss or changed — fetch last page only (Mecha-style tail fetch)
        all_chapters = page1_chapters
        if total_pages > 1:
            last_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={total_pages}"
            try:
                lp_res = await auth_session.get(last_url, timeout=30, allow_redirects=True)
                if lp_res.status_code == 200:
                    self._extract_tag_ids(lp_res.text, up_ids, coming_soon_ids)
                    tail = self._parse_page_data(lp_res.text, sees_ids=seen_ids,
                                                  up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                    all_chapters.extend(tail)
            except Exception as e:
                logger.warning(f"[Jumptoon] Poll tail-fetch soft-fail: {e}")

        all_chapters.sort(key=self._extract_sort_key)
        for ch in all_chapters:
            if str(ch['id']) in up_ids:
                ch['is_new'] = True

        # Update cache with the newest ID we just saw
        if all_chapters:
            latest_id = all_chapters[-1].get('id')
            if latest_id:
                await self._cache_write(series_id, total_chapters, str(latest_id))

        return (title, total_chapters, all_chapters, image_url, series_id,
                release_day, release_time, status_label, None)

    # ─── FULL MODE: page 1 + parallel pages 2..N ──────────────────────────────
    all_chapters = list(page1_chapters)

    # Safety net: if page 1 returned < pg_size AND we expect more, force a
    # retry through the /episodes/ endpoint. This handles the edge case where
    # the landing page has a condensed/hero-only view.
    if total_chapters > pg_size and len(all_chapters) < pg_size:
        fallback_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page=1"
        try:
            fb_res = await auth_session.get(fallback_url, timeout=30, allow_redirects=True)
            if fb_res.status_code == 200:
                self._extract_tag_ids(fb_res.text, up_ids, coming_soon_ids)
                fb_chaps = self._parse_page_data(fb_res.text, sees_ids=seen_ids,
                                                  up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                all_chapters.extend(fb_chaps)
        except Exception as e:
            logger.warning(f"[Jumptoon] Page-1 fallback soft-fail: {e}")

    if total_chapters == 0 and all_chapters:
        total_chapters = len(all_chapters)

    # 🟢 PARALLEL CRAWL: fire pages 2..N concurrently
    remaining_pages = list(range(2, int(total_pages) + 1))
    if remaining_pages:
        extra = await self._crawl_pages_parallel(
            auth_session, series_id, remaining_pages,
            seen_ids, up_ids, coming_soon_ids
        )
        all_chapters.extend(extra)

    # Sort + UP flag assignment
    all_chapters.sort(key=self._extract_sort_key)
    for ch in all_chapters:
        if str(ch['id']) in up_ids:
            ch['is_new'] = True

    # Update Redis fingerprint for future poll-mode hits
    if all_chapters:
        latest_id = all_chapters[-1].get('id')
        if latest_id:
            await self._cache_write(series_id, total_chapters, str(latest_id))

    return (title, total_chapters, all_chapters, image_url, series_id,
            release_day, release_time, status_label, None)


# ─── Shared sort key (extracted from inline closures in 3 places) ─────────────

@staticmethod
def _extract_sort_key(ch):
    """Single source of truth for chapter ordering."""
    num = ch.get('number')
    if num and str(num).isdigit():
        return int(num)
    not_match = re.search(r'(\d+)', ch.get('notation', ''))
    if not_match:
        return int(not_match.group(1))
    raw_id = ch.get('id')
    if raw_id and str(raw_id).isdigit():
        return int(raw_id)
    return 0
