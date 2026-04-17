# ═══════════════════════════════════════════════════════════════════════════
# PATCH 2 of 3 — app/bot/common/view.py
#
# Change: Collapse the sequential page-by-page background loop into a SINGLE
# fetch_more_chapters call for Jumptoon. The provider now parallelizes
# internally (see Patch 1), so we don't need to drive pagination from here.
#
# Mecha keeps its existing sequential behavior (it only has 2 pages typically,
# and its fetch_more_chapters is still sequential — not touched here).
# ═══════════════════════════════════════════════════════════════════════════

# ─── REPLACE the existing _perform_full_scan method with this version ────────

async def _perform_full_scan(self):
    """
    Fetches all missing chapter metadata in the background.

    For Jumptoon: single gather-all call (provider handles parallelism internally).
    For Mecha: sequential incremental scan (preserved from original behavior).
    """
    try:
        logger.info(f"[{self.req_id}] 📡 Starting background scan for {self.service_type}...")
        scraper = self.bot.task_queue.provider_manager.get_provider_for_url(self.url)

        pg_size = 30 if self.service_type == "jumptoon" else 10
        total_pages = math.ceil(self.total_chapters / pg_size) if self.total_chapters else 1

        seen_ids = {ch['id'] for ch in self.all_chapters}

        # ─── JUMPTOON: single parallel background call ───────────────────────
        # Provider auto-skips page 1 + last page (both already in all_chapters
        # from the tail-first foreground fetch). Internal semaphore keeps proxy
        # happy. One UI refresh when all middle pages land.
        if self.service_type == "jumptoon":
            if total_pages <= 2:
                # No middle pages to fetch (series fits in 2 pages or less)
                logger.debug(f"[{self.req_id}] Jumptoon series has ≤2 pages; no background scan needed")
                return

            logger.debug(f"[{self.req_id}] 📡 Jumptoon parallel background scan: "
                         f"{total_pages - 2} middle pages")

            # scraper.fetch_more_chapters auto-skips [1, total_pages] when
            # skip_pages=None, so we pass None explicitly to get the default.
            new_chaps = await scraper.fetch_more_chapters(
                self.url, total_pages, seen_ids, skip_pages=None
            )

            if new_chaps:
                self.all_chapters.extend(new_chaps)
                self.all_chapters.sort(key=self._jumptoon_sort_key)

                logger.info(f"[{self.req_id}] ✅ Jumptoon background scan complete: "
                            f"{len(self.all_chapters)} chapters mapped")
                self.trigger_refresh()
                self._latest_ui_update = time.time()
            return

        # ─── MECHA (and any future provider): sequential incremental scan ────
        # Preserved from original behavior — Mecha's fetch_more_chapters is
        # still page-by-page sequential, and the incremental UI updates
        # provide good UX for the small number of pages it typically has.
        for p in range(1, total_pages + 1):
            # Page 1 is already fast-fetched; skip if we have enough
            if p == 1 and len(self.all_chapters) >= pg_size:
                continue

            logger.debug(f"[{self.req_id}] 📡 Background fetching {self.service_type} page {p}...")
            new_chaps = await scraper.fetch_more_chapters(
                self.url, p, seen_ids,
                skip_pages=[i for i in range(1, p)]
            )

            if new_chaps:
                self.all_chapters.extend(new_chaps)

                # Generic numeric sort (works for Mecha; Jumptoon uses its own above)
                def extract_num(ch):
                    m = re.search(r'\d+', ch.get('notation', ''))
                    if m:
                        return int(m.group())
                    raw_id = ch.get('id')
                    return int(raw_id) if raw_id and str(raw_id).isdigit() else 0

                self.all_chapters.sort(key=extract_num)

                # Throttled UI refresh (max once per 5s)
                now = time.time()
                if now - self._latest_ui_update > 5:
                    logger.info(f"[{self.req_id}] 🔄 Throttled UI update: "
                                f"{len(self.all_chapters)} chapters mapped.")
                    self.trigger_refresh()
                    self._latest_ui_update = now

        # Final update once fully complete
        logger.info(f"[{self.req_id}] ✅ Background scan complete. "
                    f"Total mapped: {len(self.all_chapters)}")
        self.trigger_refresh()
        self._latest_ui_update = time.time()

    except Exception as e:
        logger.error(f"[{self.req_id}] ❌ Background full scan failed: {e}")


# ─── ADD this helper method to UniversalDashboard (reuses provider's sort) ───

@staticmethod
def _jumptoon_sort_key(ch):
    """
    Jumptoon-specific sort matching JumptoonProvider._extract_sort_key.
    Kept in sync deliberately — Jumptoon has hiatus chapters (45.1, 45.2, etc.)
    where generic regex extraction would collide.
    """
    # 1. Primary: numeric 'number' field
    num = ch.get('number')
    if num and str(num).isdigit():
        return int(num)
    # 2. Secondary: regex from notation
    import re as _re
    not_match = _re.search(r'(\d+)', ch.get('notation', ''))
    if not_match:
        return int(not_match.group(1))
    # 3. Tertiary: numeric ID fallback
    raw_id = ch.get('id')
    if raw_id and str(raw_id).isdigit():
        return int(raw_id)
    return 0
