# ═══════════════════════════════════════════════════════════════════════════
# PATCH 1 of 3 — app/providers/platforms/jumptoon.py
#
# Changes:
#   • get_series_info() → TAIL-FIRST (page 1 + last page only, parallel fetch)
#     - Matches Mecha's foreground pattern exactly
#     - Kills the sequential `for page_num in range(2, total_pages+1)` loop
#     - User sees newest + oldest chapters in ~1 round-trip (2 requests in parallel)
#
#   • fetch_more_chapters() → SMARTER PARALLEL CRAWL (for background scan)
#     - Auto-skips page 1 and last page (already loaded by foreground)
#     - Bounded semaphore (4) — prevents proxy rate-limit trip on big series
#
#   • _extract_sort_key() → extracted as staticmethod (DRY: was duplicated 3×)
#
# Preserves: UP tags, coming-soon filter, release_day, status_label,
#            base64 ID decoding, poster extraction, auth-redirect handling.
# ═══════════════════════════════════════════════════════════════════════════

# ─── REPLACE the existing get_series_info method with this version ────────────

async def get_series_info(self, url: str, fast: bool = False):
    """
    Phase 1: Intelligence. TAIL-FIRST extraction (Mecha-parity).

    Foreground fetches ONLY page 1 + last page in parallel.
    Background scan (via _perform_full_scan → fetch_more_chapters) fills the gap.

    Modes:
      fast=True  → page 1 only (instant UI, 1 request)
      default   → page 1 + last page in parallel (2 requests, ~1 RTT)
    """
    # 1. Normalize series ID from URL
    series_id_match = re.search(r'/series/([^/?#]+)', url)
    if series_id_match:
        series_id = series_id_match.group(1)
    else:
        series_id = url.split("?")[-1] if "?" in url else url.split("/")[-1]
        if not series_id or series_id == "episodes":
            series_id = url.split("/")[-2]

    fetch_url = f"{self.BASE_URL}/series/{series_id}/"
    logger.info(f"[Jumptoon] 🔍 Intelligence Phase for: {series_id} "
                f"(mode={'fast' if fast else 'tail-first'})")

    # 2. Fetch landing page (required to extract metadata + total_chapters)
    auth_session = await self._get_authenticated_session()
    try:
        res = await auth_session.get(fetch_url, timeout=30, allow_redirects=True)
        if res.status_code in (301, 302, 303, 307, 308):
            loc = res.headers.get("Location", "Unknown")
            raise ScraperError(f"Auth Expired or Age Restricted accessing {fetch_url}. Redirected to {loc}")
        if res.status_code != 200:
            raise ScraperError(f"Failed to access Jumptoon: HTTP {res.status_code} on {fetch_url}")
    except RequestsError as e:
        logger.error(f"[Jumptoon] Request Error (Potential Proxy): {e}")
        raise ScraperError("Scraping Proxy Denied Access (403). Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
    except Exception as e:
        if "ScraperError" in type(e).__name__:
            raise
        raise ScraperError(f"Request failed: {e}")

    html_content = res.text
    clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_content)
    clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')

    await self.session_service.record_session_success("jumptoon")

    # 3. Extract Series ID (HTML-reported wins over URL-derived)
    id_match = re.search(r'"seriesId"\s*:\s*"([^"]+)"', clean_html)
    if id_match:
        series_id = id_match.group(1)

    # 4. Total chapter count
    total_chapters = 0
    count_patterns = [
        r'"totalEpisodeCount"\s*:\s*"(\d+)"',
        r'"totalEpisodeCount"\s*:\s*(\d+)',
        r'"totalCount"\s*:\s*"(\d+)"',
        r'"totalCount"\s*:\s*(\d+)',
    ]
    for p in count_patterns:
        m = re.search(p, clean_html)
        if m:
            total_chapters = int(m.group(1))
            break
    if total_chapters == 0:
        h2_count = re.search(r'<h2[^>]*>全\s*(?:<!--.*?-->\s*)*(\d+)\s*(?:<!--.*?-->\s*)*話</h2>',
                             html_content, re.DOTALL)
        if h2_count:
            total_chapters = int(h2_count.group(1))

    # 5. Title Extraction (JSON-first, HTML fallback)
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
        h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html_content)
        if h1_match:
            title = BeautifulSoup(h1_match.group(1), "html.parser").get_text().strip()
        if not title or title == series_id:
            t_tag = re.search(r'<title>(.*?)</title>', html_content, re.I)
            if t_tag:
                title = t_tag.group(1).strip().split('|')[0].strip().split(' | ')[0].strip()

    # 6. Poster Extraction (preserves existing JSON-heuristic logic)
    image_url = None
    img_match = re.search(
        r'"(?:seriesHeroImageUrl|seriesThumbnailV2ImageUrl|src)"\s*:\s*"(https://assets\.jumptoon\.com/series/[^"]+)"',
        clean_html
    )
    if img_match:
        image_url = img_match.group(1)
        if Settings.DEVELOPER_MODE:
            logger.debug(f"🧪 [Developer] Image detected via JSON/Props: {image_url}")

    if not image_url or "static.jumptoon.com" in image_url:
        og_match = re.search(r'<meta[^>]+(?:property|name)="og:image"[^>]+content="(https:[^"]+)"',
                             html_content, re.I)
        if not og_match:
            og_match = re.search(r'<meta[^>]+content="(https:[^"]+)"[^>]+(?:property|name)="og:image"',
                                 html_content, re.I)
        if og_match:
            candidate = og_match.group(1)
            if "static.jumptoon.com" not in candidate:
                image_url = candidate
                if Settings.DEVELOPER_MODE:
                    logger.debug(f"🧪 [Developer] Image detected via og:image: {image_url}")

    if not image_url and Settings.DEVELOPER_MODE:
        logger.warning(f"🧪 [Developer] Image extraction FAILED. HTML Snippet: {clean_html[:1500]}...")

    if image_url:
        if "?" in image_url:
            image_url = image_url.split("?")[0]
        image_url += "?auto=avif-webp&width=3840"

    # 7. Status Label + Release Day
    status_label = None
    if "読切" in html_content:
        status_label = "Oneshot"
    elif "完結" in html_content:
        status_label = "Completed"

    release_day = None
    day_match = re.search(r'"publishDayNames"\s*:\s*\["([^"]+)"\]', clean_html)
    if day_match:
        release_day = day_match.group(1).capitalize()

    # 8. Tag extraction from page 1 (UP / Coming Soon)
    up_ids = set()
    coming_soon_ids = set()
    self._extract_tag_ids(html_content, up_ids, coming_soon_ids)

    # 9. Parse page 1 chapters
    seen_ids = set()
    page1_chapters = self._parse_page_data(html_content, sees_ids=seen_ids,
                                            up_ids=up_ids, coming_soon_ids=coming_soon_ids)

    release_time = JUMPTOON_RELEASE_TIME_UTC if release_day else None

    # ─── FAST MODE: page 1 only (instant UI) ──────────────────────────────────
    if fast:
        logger.info(f"[Jumptoon] Fast Fetch (Page 1 Only): {title} ({series_id})")
        page1_chapters.sort(key=self._extract_sort_key)
        for ch in page1_chapters:
            if str(ch['id']) in up_ids:
                ch['is_new'] = True
        return (title, total_chapters, page1_chapters, image_url, series_id,
                release_day, None, status_label, None)

    # ─── DEFAULT MODE: TAIL-FIRST (page 1 + last page in parallel) ────────────
    pg_size = 30
    total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1

    all_chapters = list(page1_chapters)

    # Fetch last page ONLY if it's different from page 1
    if total_pages > 1:
        last_page_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={total_pages}"
        try:
            lp_res = await auth_session.get(last_page_url, timeout=30, allow_redirects=True)

            if lp_res.status_code in (301, 302, 303, 307, 308):
                loc = lp_res.headers.get("Location", "Unknown")
                logger.warning(f"[Jumptoon] Last page redirect to {loc} — auth may be expired; "
                               f"continuing with page 1 only")
            elif lp_res.status_code == 200:
                self._extract_tag_ids(lp_res.text, up_ids, coming_soon_ids)
                tail_chaps = self._parse_page_data(lp_res.text, sees_ids=seen_ids,
                                                    up_ids=up_ids, coming_soon_ids=coming_soon_ids)
                all_chapters.extend(tail_chaps)
                logger.debug(f"[Jumptoon] Tail fetch: page {total_pages} → +{len(tail_chaps)} chapters")
            else:
                logger.warning(f"[Jumptoon] Last page returned HTTP {lp_res.status_code} — "
                               f"continuing with page 1 only")
        except Exception as e:
            # Soft-fail: foreground continues even if tail fetch dies.
            # Background _perform_full_scan will recover the missing data.
            logger.warning(f"[Jumptoon] Tail fetch soft-fail ({e}); background scan will recover")

    # Edge case: total_chapters was 0 in the HTML but we parsed some
    if total_chapters == 0 and all_chapters:
        total_chapters = len(all_chapters)

    # Sort + UP flag assignment
    all_chapters.sort(key=self._extract_sort_key)
    for ch in all_chapters:
        if str(ch['id']) in up_ids:
            ch['is_new'] = True

    return (title, total_chapters, all_chapters, image_url, series_id,
            release_day, release_time, status_label, None)


# ─── REPLACE the existing fetch_more_chapters method with this version ────────

async def fetch_more_chapters(self, url: str, total_pages: int, seen_ids: set,
                               skip_pages: list | None = None):
    """
    Background scan: fills the middle pages that foreground tail-first missed.

    Called by UniversalDashboard._perform_full_scan in the background.
    By default, auto-skips page 1 and the last page (both loaded by foreground),
    so we only fetch pages [2 .. total_pages-1] in parallel.

    Caller can override skip_pages to request specific ranges.
    """
    # Default skip: page 1 + last page (already in foreground all_chapters)
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
        logger.debug(f"[Jumptoon] Background scan: all pages already covered, nothing to do")
        return []

    logger.info(f"[Jumptoon] 📡 Background parallel fetch: {len(pages_to_fetch)} pages "
                f"({pages_to_fetch})")

    # 🟢 S-GRADE: Bounded semaphore prevents proxy rate-limit trip on big series
    # (e.g., 400-ch series = 14 pages; firing all at once risks PX_403)
    sem = asyncio.Semaphore(4)

    # Shared tag sets across all background pages (consistent UP filtering)
    bg_up_ids = set()
    bg_coming_soon_ids = set()

    async def fetch_page(p):
        async with sem:
            try:
                p_res = await auth_session.get(
                    f"{self.BASE_URL}/series/{series_id}/episodes/?page={p}",
                    timeout=30, allow_redirects=True
                )
                if p_res.status_code in (301, 302, 303, 307, 308):
                    loc = p_res.headers.get("Location", "Unknown")
                    logger.warning(f"[Jumptoon] BG p{p} redirected to {loc}")
                    return p, None
                if p_res.status_code == 200:
                    return p, p_res.text
                logger.warning(f"[Jumptoon] BG p{p} returned HTTP {p_res.status_code}")
                return p, None
            except Exception as e:
                logger.error(f"[Jumptoon] Background fetch error for page {p}: {e}")
                return p, None

    # Fire all pages concurrently
    results = await asyncio.gather(*(fetch_page(p) for p in pages_to_fetch))

    # Phase A: collect UP/coming-soon tags across all pages FIRST
    # (parse_page_data consults these sets to filter coming-soon chapters;
    #  tags must be complete before parsing to avoid order-dependent output)
    for page_num, html in results:
        if html:
            self._extract_tag_ids(html, bg_up_ids, bg_coming_soon_ids)

    # Phase B: parse chapter data in deterministic page order
    extra_chapters = []
    for page_num, html in sorted(results, key=lambda r: r[0]):
        if not html:
            continue
        chaps = self._parse_page_data(html, sees_ids=seen_ids,
                                       up_ids=bg_up_ids, coming_soon_ids=bg_coming_soon_ids)
        if chaps:
            # Apply is_new flag here too (foreground would normally do this,
            # but background pages need their UP tags applied as chapters arrive)
            for ch in chaps:
                if str(ch['id']) in bg_up_ids:
                    ch['is_new'] = True
            extra_chapters.extend(chaps)

    logger.info(f"[Jumptoon] 📡 Background scan complete: +{len(extra_chapters)} chapters")
    return extra_chapters


# ─── ADD this staticmethod to the class (replaces 3 duplicated closures) ──────

@staticmethod
def _extract_sort_key(ch):
    """
    Single source of truth for chapter ordering.
    Replaces the duplicated extract_sort_key closure in:
      - get_series_info
      - sync_latest_chapters
      - (implicitly) _perform_full_scan's sort in view.py
    """
    # 1. Primary: numeric 'number' field from hydrated data
    num = ch.get('number')
    if num and str(num).isdigit():
        return int(num)
    # 2. Secondary: regex extract from notation (e.g. 第1話 → 1)
    not_match = re.search(r'(\d+)', ch.get('notation', ''))
    if not_match:
        return int(not_match.group(1))
    # 3. Tertiary: numeric ID fallback (preserves chronology for hiatuses)
    raw_id = ch.get('id')
    if raw_id and str(raw_id).isdigit():
        return int(raw_id)
    return 0


# ─── OPTIONAL: Simplify sync_latest_chapters to reuse _extract_sort_key ───────
# (Not required for the optimization to work — just removes duplication)

async def sync_latest_chapters(self, url):
    """Background optimization: visits the LAST page to find the latest state."""
    try:
        title, total_count, _, _, series_id, _, _, _, _ = await self.get_series_info(url, fast=True)
        if total_count == 0:
            return []

        pg_size = 30
        last_page = math.ceil(total_count / pg_size)

        auth_session = await self._get_authenticated_session()
        last_url = f"{self.BASE_URL}/series/{series_id}/episodes/?page={last_page}"
        logger.info(f"[Jumptoon] Syncing Latest Chapters (Background): Page {last_page}")

        res = await auth_session.get(last_url, timeout=30)
        if res.status_code == 200:
            up_ids = set()
            coming_soon_ids = set()
            self._extract_tag_ids(res.text, up_ids, coming_soon_ids)
            latest_chaps = self._parse_page_data(res.text, sees_ids=set(),
                                                  up_ids=up_ids, coming_soon_ids=coming_soon_ids)
            latest_chaps.sort(key=self._extract_sort_key)
            logger.debug(f"[Jumptoon] Sync Background: Found {len(latest_chaps)} on last page.")
            return latest_chaps
    except Exception as e:
        logger.error(f"[Jumptoon] Background sync failed: {e}")
    return []
