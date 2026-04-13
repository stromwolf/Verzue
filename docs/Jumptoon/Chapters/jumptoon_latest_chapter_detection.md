# Jumptoon Latest Chapter Detection

This document provides a technical overview of how the **Verzue** Jumptoon provider detects and validates the latest chapters released on the Jumptoon platform.

## 1. Overview & Schedule

Jumptoon typically releases new chapters at **00:00 JST** (15:00 UTC). The system is designed to synchronize with this schedule using a high-frequency polling window to ensure near-instant detection for subscribed series.

- **Standard Release Time**: 15:00 UTC.
- **Polling Window**: The `AutoDownloadPoller` activates a high-frequency loop (every 10 seconds) between **15:00:01 and 15:05:00 UTC**.
- **Targeting**: Only series whose `release_day` matches the current day are prioritized during this high-frequency window to optimize proxy bandwidth.

## 2. Core Detection Logic

The detection process is primarily handled by the `JumptoonProvider.get_series_info` and `_parse_page_data` methods.

### A. Intelligence Phase (Metadata Extraction)
When a series is checked, the provider first visits the base series URL:
`https://jumptoon.com/series/<series_id>/`

It extracts the following metadata from the raw HTML:
- **Total Chapter Count**: Extracted via regex from JSON-like hydration strings (e.g., `totalEpisodeCount` or `totalCount`).
- **Release Day**: Extracted from `publishDayNames` in the hydrated page state.
- **Series Status**: Checks for labels like "読切" (Oneshot) or "完結" (Completed).

### B. Chapter Discovery (Hydrated Data Parsing)
Jumptoon uses a Next.js architecture where data is "hydrated" into the HTML as JSON strings. The provider uses a dual-parsing approach:

1.  **JSON Heuristic Extraction**: The provider scans the HTML for blocks starting with `{"id": "..."`. It recursively extracts the full JSON object for each chapter to access metadata like `publishStartDatetime`, `offerType`, and `isPurchased`.
2.  **Breadth-First HTML Search**: If JSON parsing is insufficient, it uses `BeautifulSoup` to scan `<li>` blocks for `<h3>` tags. This ensures that the exact Chapter Notation (e.g., "第1話") and Chapter Title (e.g., "始まりの場所") are captured as they appear in the UI.

## 3. Validation & Filtering

To prevent the detection of "Coming Soon" placeholders or future-scheduled releases, several strict filters are applied.

### A. Strict Timestamp Filter
Every chapter node contains a `publishStartDatetime` (UTC milliseconds). The provider compares this against the current system time:
- **Logic**: `publishStartDatetime > (current_time + 60000)`
- **Purpose**: Chapters scheduled more than 1 minute in the future are ignored. This prevents the bot from attempting to "download" chapters that are visible in the list but not yet accessible.

### B. Tag-Based Detection (`UP` Tag)
The provider performs a raw scan of the `<li>` blocks for specific CSS markers or inner text matching:
- **UP Tag**: Detected via regex `>UP<|UP\s*</|[>{\s]UP[\s<}]`. Chapters with this tag are marked as `is_new = True`.
- **Exclusion Markers**: "COMING SOON", "次回更新", or "に更新予定" are used to flag chapters that should be skipped during the current poll.

### C. Chapter Type Filtering
Only chapters with a valid `offerType` (e.g., `FREE`, `WAIT_FREE`, `PAID`) are processed. If `offerType` is `null`, it is treated as an unreleased placeholder.

## 4. Sorting & Indexing

Chapters are sorted before being compared to the local database to ensure the "Latest" chapter is correctly identified as the tail of the list.

The `extract_sort_key` uses the following priority:
1.  **Numeric `number` field**: Provided directly in the JSON data.
2.  **Notation Parsing**: Regex `(\d+)` applied to the `notation` field (e.g., extracting `10` from `第10話`).
3.  **Episode ID**: A fallback numeric ID used to maintain chronological order in cases of hiatus or non-standard naming.

## 5. Synchronization Optimizations

### High-Frequency Poller
The `AutoDownloadPoller` implements a "High Frequency" mode specifically for Jumptoon. Between 15:00 and 15:05 UTC, it ignores the standard 30-minute interval and checks targets every 10 seconds.

### Background Sync (`sync_latest_chapters`)
For series with a very large number of chapters (e.g., 500+), the provider uses a `sync_latest_chapters` optimization:
1.  Calculates the `last_page` index based on `total_chapters / 30`.
2.  Directly visits `https://jumptoon.com/series/<id>/episodes/?page=<last_page>`.
3.  This avoids a full crawl of all 500+ chapters, focusing only on the most recent entries.

## 6. Persistence & Notification

Once chapters are extracted and sorted:
1.  The `current_id` of the last chapter in the sorted list is compared against `last_known_chapter_id` in the group's subscription JSON.
2.  If `current_id != last_known_chapter_id`, the system triggers an update.
3.  The database is updated with the new ID, and a Discord notification is dispatched via the `PollerNotifier`.
