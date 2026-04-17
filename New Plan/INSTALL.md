# Jumptoon Optimization — Install Guide

## Method summary

**Foreground (get_series_info):** Tail-first — page 1 + last page only.
User sees newest + oldest chapters in ~300ms.

**Background (_perform_full_scan → fetch_more_chapters):** Parallel middle-pages.
Bounded semaphore (4) fills pages 2..N-1 concurrently, UI refreshes when done.

**No cache, no poller changes.** Exact match of Mecha's philosophy with Jumptoon's
pagination-size realities (30/page vs Mecha's 10/page).

---

## Files changed

### 1. `app/providers/platforms/jumptoon.py`

**Replace these methods** (search for the existing `def` line):

- `async def get_series_info(self, url, fast=False)` → use patch1_jumptoon.py version
- `async def fetch_more_chapters(self, url, total_pages, seen_ids, skip_pages=None)` → use patch1 version
- `async def sync_latest_chapters(self, url)` → use patch1 version (minor cleanup, reuses _extract_sort_key)

**Add this staticmethod to the class:**

- `_extract_sort_key(ch)` → staticmethod, single source of truth for ordering

---

### 2. `app/bot/common/view.py`

**Replace this method** in class `UniversalDashboard`:

- `async def _perform_full_scan(self)` → use patch2_view.py version

**Add this staticmethod to the class:**

- `_jumptoon_sort_key(ch)` → mirrors provider's sort key

---

### 3. Poller (`app/tasks/poller.py`)

**No changes.** Poller calls `get_series_info(url)` without kwargs — it now
returns tail-first data by default, which is exactly what the poller needs
(it reads `chapter_list[-1]` for latest chapter ID).

---

## What the user sees

### Before (old sequential crawl):
```
[0ms]    User pastes Jumptoon URL
[400ms]  Landing page fetched + parsed
[600ms]  Sequential fetch page 2
[800ms]  Sequential fetch page 3
[1000ms] Sequential fetch page 4
[1200ms] Sequential fetch page 5
[1200ms] UI appears with full chapter list
```

### After (tail-first + parallel background):
```
[0ms]    User pastes Jumptoon URL
[400ms]  Landing page fetched + parsed
[600ms]  Last page fetched (parallel-ready)
[620ms]  UI appears with newest + oldest chapters → user can hit Download Latest
[620ms]  Background scan fires 3 middle pages in parallel
[820ms]  Middle pages land, UI refreshes with full list
```

**Net result:** UI is interactive ~600ms earlier, total bandwidth unchanged.

---

## Testing checklist

- [ ] Small series (1 page): no regression, background scan no-ops correctly
- [ ] Medium series (4-5 pages): foreground shows p1+last, background fills 2/3/4
- [ ] Large series (14+ pages): semaphore prevents proxy PX_403
- [ ] Hiatus series (has 45.1, 45.2): sort order still correct after background merge
- [ ] Coming-soon chapters: still filtered out across foreground + background
- [ ] UP tags: still applied to chapters from all pages
- [ ] Poller: still detects new chapters (reads chapter_list[-1] which is always the last page's newest)
- [ ] Fast mode (`fast=True`): still returns page 1 only, unchanged
- [ ] sync_latest_chapters: still works for background subscription adds

---

## Rollback

Each patch is additive/replacement-only. To roll back:
1. Restore the original `get_series_info`, `fetch_more_chapters`, `sync_latest_chapters`
2. Restore the original `_perform_full_scan`
3. Remove the added staticmethods (`_extract_sort_key`, `_jumptoon_sort_key`)

No DB migrations, no Redis keys written, no config changes.
