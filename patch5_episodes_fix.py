# ═══════════════════════════════════════════════════════════════════════════
# PATCH 5 — app/providers/platforms/jumptoon.py
#
# Fix: _fetch_series_info_uncached
#
# The landing page /series/JT00020 only embeds 5-6 chapters as a preview.
# The real full chapter list lives at /series/JT00020/episodes/?page=X
#
# New flow:
#   1. GET /series/JT00020/         → metadata ONLY (title, total, poster, etc.)
#   2. GET /episodes/?page=1        ┐ in parallel
#      GET /episodes/?page=N        ┘ (tail-first)
#   3. Background fills pages 2..N-1 as before
#
# fast=True:  GET /series/ + GET /episodes/?page=1  (2 requests, sequential-ish)
# default:    GET /series/ + parallel(page=1, page=N) (3 requests, ~1 RTT)
# ═══════════════════════════════════════════════════════════════════════════

async def _fetch_series_info_uncached(self, url: str, series_id: str, fast: bool):
    """
    Metadata from landing page. Chapters from /episodes/?page=X.
    All HTTP requests go through _jumptoon_gated_get.
    """
    landing_url = f"{self.BASE_URL}/series/{series_id}/"
    episodes_base = f"{self.BASE_URL}/series/{series_id}/episodes/"

    logger.info(f"[Jumptoon] 🔍 Fetching: {series_id} "
                f"(mode={'fast' if fast else 'tail-first'})")

    auth_session = await self._get_authenticated_session()

    # ─── Step 1: Landing page — metadata ONLY, no chapter parsing ────────────
    try:
        res = await self._jumptoon_gated_get(auth_session, landing_url, timeout=30)
        if res.status_code in (301, 302, 303, 307, 308):
            loc = res.headers.get("Location", "Unknown")
            raise ScraperError(f"Auth Expired or Age Restricted. Redirected to {loc}")
        if res.status_code != 200:
            raise ScraperError(f"Failed to access Jumptoon: HTTP {res.status_code}")
    except RequestsError as e:
        logger.error(f"[Jumptoon] Request Error (Potential Proxy): {e}")
        raise ScraperError("Scraping Proxy Denied Access (403). "
                           "Check bandwidth or IP Whitelist in Vess Dashboard.", code="PX_403")
    except ScraperError:
        raise
    except Exception as e:
        raise ScraperError(f"Request failed: {e}")

    await self.session_service.record_session_success("jumptoon")

    html_content = res.text
    clean_html = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), html_content)
    clean_html = re.sub(r'\\+"', '"', clean_html).replace('\\/', '/')

    # Extract Series ID (HTML-reported is more reliable than URL)
    id_match = re.search(r'"seriesId"\s*:\s*"([^"]+)"', clean_html)
    if id_match:
        series_id = id_match.group(1)

    # Total chapter count
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

    # Title
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
    if image_url:
        if "?" in image_url:
            image_url = image_url.split("?")[0]
        image_url += "?auto=avif-webp&width=3840"

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

    release_time = JUMPTOON_RELEASE_TIME_UTC if release_day else None

    # ─── Step 2: Fetch chapters from /episodes/?page=X ───────────────────────
    pg_size = 30
    total_pages = math.ceil(total_chapters / pg_size) if total_chapters > 0 else 1

    up_ids = set()
    coming_soon_ids = set()
    seen_ids = set()

    # ── FAST MODE: just page 1 from /episodes/ ───────────────────────────────
    if fast:
        logger.info(f"[Jumptoon] Fast Fetch (episodes page 1): {title} ({series_id})")
        try:
            p1_res = await self._jumptoon_gated_get(
                auth_session, f"{episodes_base}?page=1", timeout=30
            )
            if p1_res.status_code == 200:
                self._extract_tag_ids(p1_res.text, up_ids, coming_soon_ids)
                page1_chapters = self._parse_page_data(p1_res.text, sees_ids=seen_ids,
                                                        up_ids=up_ids, coming_soon_ids=coming_soon_ids)
            else:
                page1_chapters = []
        except Exception as e:
            logger.warning(f"[Jumptoon] Fast page-1 fetch failed: {e}")
            page1_chapters = []

        if total_chapters == 0 and page1_chapters:
            total_chapters = len(page1_chapters)

        page1_chapters.sort(key=self._extract_sort_key)
        for ch in page1_chapters:
            if str(ch['id']) in up_ids:
                ch['is_new'] = True

        return (title, total_chapters, page1_chapters, image_url, series_id,
                release_day, None, status_label, None)

    # ── DEFAULT MODE: page 1 + last page in PARALLEL from /episodes/ ─────────
    async def fetch_episodes_page(page_num: int):
        try:
            r = await self._jumptoon_gated_get(
                auth_session, f"{episodes_base}?page={page_num}", timeout=30
            )
            if r.status_code in (301, 302, 303, 307, 308):
                logger.warning(f"[Jumptoon] episodes p{page_num} redirected — auth may be stale")
                return page_num, None
            if r.status_code == 200:
                return page_num, r.text
            logger.warning(f"[Jumptoon] episodes p{page_num} → HTTP {r.status_code}")
            return page_num, None
        except Exception as e:
            logger.warning(f"[Jumptoon] episodes p{page_num} fetch failed: {e}")
            return page_num, None

    pages_to_fetch = [1]
    if total_pages > 1:
        pages_to_fetch.append(total_pages)

    # Fire page 1 and last page in parallel
    fetch_results = await asyncio.gather(*(fetch_episodes_page(p) for p in pages_to_fetch))

    # Phase A: extract tags from all fetched pages first (consistent filtering)
    for _, html in fetch_results:
        if html:
            self._extract_tag_ids(html, up_ids, coming_soon_ids)

    # Phase B: parse chapters in page order
    all_chapters = []
    for _, html in sorted(fetch_results, key=lambda r: r[0]):
        if not html:
            continue
        chaps = self._parse_page_data(html, sees_ids=seen_ids,
                                       up_ids=up_ids, coming_soon_ids=coming_soon_ids)
        all_chapters.extend(chaps)

    if total_chapters == 0 and all_chapters:
        total_chapters = len(all_chapters)

    all_chapters.sort(key=self._extract_sort_key)
    for ch in all_chapters:
        if str(ch['id']) in up_ids:
            ch['is_new'] = True

    logger.info(f"[Jumptoon] ✅ Foreground done: {len(all_chapters)} chapters "
                f"(p1+last of {total_pages}), background will fill the rest")

    return (title, total_chapters, all_chapters, image_url, series_id,
            release_day, release_time, status_label, None)
