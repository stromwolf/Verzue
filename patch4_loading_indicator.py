# ═══════════════════════════════════════════════════════════════════════════
# PATCH 4 — app/bot/common/view.py
#
# Problem: tail-first returns page 1 + last page. For a series with
#   chapters 1-6 (p1) and 31-34 (last page), the user sees a visible
#   gap (ch7-30 missing) with no indication that anything is loading.
#
# Fix: Two tiny surgical changes.
#   1. __init__: add self._bg_scanning = False
#   2. _perform_full_scan: flip the flag on start/end + trigger_refresh
#   3. build_v2_payload: inject a loading line into the chapter list
#      when the gap exists and the background scan is still running
#
# No new dependencies. No refactor. Three additions to existing code.
# ═══════════════════════════════════════════════════════════════════════════


# ─── CHANGE 1: In __init__, after self._full_scan_task = None line ────────────
#
# Add this line:
self._bg_scanning = False
#
# Then change the background scan launch block from:
#
#   if self.service_type in ["mecha", "jumptoon"] and self.total_chapters > len(self.all_chapters):
#       self._full_scan_task = asyncio.create_task(self._perform_full_scan())
#
# To:
if self.service_type in ["mecha", "jumptoon"] and self.total_chapters > len(self.all_chapters):
    self._bg_scanning = True  # ← ADD THIS LINE
    self._full_scan_task = asyncio.create_task(self._perform_full_scan())


# ─── CHANGE 2: In _perform_full_scan, wrap the body ──────────────────────────
#
# The method currently starts with:
#   try:
#       logger.info(...)
#
# Change the finally block (or add one if not present) to always clear the flag:

async def _perform_full_scan(self):
    """
    Fetches all missing chapter metadata in the background.
    Sets _bg_scanning=True during load so the UI can show a loading indicator.
    """
    try:
        logger.info(f"[{self.req_id}] 📡 Starting background scan for {self.service_type}...")
        scraper = self.bot.task_queue.provider_manager.get_provider_for_url(self.url)

        pg_size = 30 if self.service_type == "jumptoon" else 10
        total_pages = math.ceil(self.total_chapters / pg_size) if self.total_chapters else 1
        seen_ids = {ch['id'] for ch in self.all_chapters}

        # ── JUMPTOON: single parallel background call ────────────────────────
        if self.service_type == "jumptoon":
            if total_pages <= 2:
                logger.debug(f"[{self.req_id}] Jumptoon ≤2 pages; no middle pages to fetch")
                return

            logger.debug(f"[{self.req_id}] 📡 Jumptoon parallel BG scan: "
                         f"{total_pages - 2} middle pages")

            new_chaps = await scraper.fetch_more_chapters(
                self.url, total_pages, seen_ids, skip_pages=None
            )

            if new_chaps:
                self.all_chapters.extend(new_chaps)
                self.all_chapters.sort(key=self._jumptoon_sort_key)
                logger.info(f"[{self.req_id}] ✅ Jumptoon BG scan complete: "
                            f"{len(self.all_chapters)} chapters mapped")
            return

        # ── MECHA (and others): sequential incremental scan ──────────────────
        for p in range(1, total_pages + 1):
            if p == 1 and len(self.all_chapters) >= pg_size:
                continue

            logger.debug(f"[{self.req_id}] 📡 BG fetching {self.service_type} page {p}...")
            new_chaps = await scraper.fetch_more_chapters(
                self.url, p, seen_ids,
                skip_pages=[i for i in range(1, p)]
            )

            if new_chaps:
                self.all_chapters.extend(new_chaps)

                def extract_num(ch):
                    m = re.search(r'\d+', ch.get('notation', ''))
                    if m: return int(m.group())
                    raw_id = ch.get('id')
                    return int(raw_id) if raw_id and str(raw_id).isdigit() else 0

                self.all_chapters.sort(key=extract_num)

                now = time.time()
                if now - self._latest_ui_update > 5:
                    logger.info(f"[{self.req_id}] 🔄 BG update: {len(self.all_chapters)} chapters")
                    self.trigger_refresh()
                    self._latest_ui_update = now

        logger.info(f"[{self.req_id}] ✅ BG scan complete: {len(self.all_chapters)} chapters")

    except Exception as e:
        logger.error(f"[{self.req_id}] ❌ BG scan failed: {e}")

    finally:
        # ✅ ALWAYS clear the flag, even on exception
        self._bg_scanning = False
        self.trigger_refresh()          # ← triggers one final UI update to remove the indicator
        self._latest_ui_update = time.time()


# ─── CHANGE 3: In build_v2_payload, inside the chapter list block ─────────────
#
# Find this existing block (it's in the STANDARD DESIGN / selection mode section):
#
#   desc += "### Chapter List\n"
#   start_idx = (self.page - 1) * self.per_page
#   display_chapters = self.all_chapters[start_idx : start_idx + self.per_page]
#
# After building the chapter list lines but BEFORE appending desc to the payload,
# add the loading indicator. Here is the full replacement for that block:

# ── Inside build_v2_payload, replace the chapter list section with: ──────────

desc += "### Chapter List\n"
start_idx = (self.page - 1) * self.per_page
display_chapters = self.all_chapters[start_idx: start_idx + self.per_page]

# ... (existing chapter line-building loop — unchanged) ...

# 🟢 BACKGROUND SCAN INDICATOR
# Show a loading line when we know middle pages are still arriving.
# Condition: bg scan is running AND there's a visible gap in the current page.
# A gap exists when the last displayed chapter's number is not contiguous
# with the first chapter we know is on the last page.
if getattr(self, '_bg_scanning', False):
    # How many chapters are we still expecting?
    missing = self.total_chapters - len(self.all_chapters)
    if missing > 0:
        # Only show the indicator on the page where the gap would appear.
        # The gap is always between the last chapter of page 1 data and the
        # first chapter of the last-page data. We detect this by checking if
        # the current display window contains chapters from BOTH ends of the
        # known list (i.e., page 1 is on display) but total_chapters is higher.
        #
        # Simpler heuristic: show it whenever the bg scan is running and the
        # user is on the FIRST page of the chapter list (most likely to see gap).
        if self.page == 1:
            desc += f"\n{ICONS['load']} *Loading {missing} more chapters...*\n"
