# Piccoma Technical Overview: Latest Chapter Detection

This document provides a comprehensive technical breakdown of how the Piccoma provider in Verzue detects the latest chapters for a given series. It covers the data sources, extraction heuristics, and the update comparison logic used by the polling system.

## 1. Core Workflow Overview

The detection of new chapters follows a multi-stage process:
1.  **Target Identification**: The series ID is extracted from the provided Piccoma URL (e.g., `https://piccoma.com/web/product/12345`).
2.  **Data Acquisition**: The provider fetches both the main product page and a dedicated episode list endpoint.
3.  **Information Extraction**: Two heuristics are applied to parse the chapter list, favoring structured JSON data before falling back to HTML scraping.
4.  **Sorting & Selection**: Chapters are sorted by their internal Piccoma ID. The chapter with the highest ID is considered the "Latest" chapter.
5.  **State Comparison**: The polling system compares this latest ID against the `last_known_chapter_id` stored in the subscription database.

---

## 2. Data Sources & Endpoints

The provider interacts with two primary URLs to gather chapter information:

| URL Type | Pattern | Purpose |
| :--- | :--- | :--- |
| **Product Page** | `https://piccoma.com/web/product/{series_id}` | Primary landing page containing series metadata. |
| **Episode List API** | `https://piccoma.com/web/product/{series_id}/episodes?etype=E` | A secondary endpoint that often provides a cleaner list of episodes. |

The provider uses an **Authenticated Session** to access these pages, ensuring that chapters visible only to logged-in users (or those reachable via "Wait-Free" mechanics) are correctly identified.

---

## 3. Extraction Heuristics

Piccoma uses a modern web stack (Next.js), which allows Verzue to extract data using two distinct strategies.

### Heuristic A: Structured JSON (`__NEXT_DATA__`)
This is the most reliable method. Most Piccoma pages contain a script tag with the ID `__NEXT_DATA__`, which holds the initial state of the page in JSON format.

*   **Path**: `props.pageProps.initialState.product.episodeList` (or `.viewer.episodeList`).
*   **Data Fields**:
    *   `id`: The unique numeric identifier for the chapter.
    *   `title`: The display name (e.g., "Episode 105").
    *   `is_new` / `isNew`: Boolean flag indicating if the chapter was recently released.
    *   `is_free` / `isFree`: Boolean flag for immediate access.
    *   `is_wait_free` / `isWaitFree`: Boolean flag for "Wait-Free" (待てば¥0) availability.

### Heuristic B: DOM Parsing (HTML Fallback)
If the JSON blob is missing or malformed, the provider falls back to scanning the HTML structure of the episode list.

*   **Selectors**: `ul.PCM-epList li`, `div.PCM-epList_item`.
*   **Logic**:
    *   **ID Extraction**: Parsed from the anchor tag `href` or `data-episode_id` attribute.
    *   **Status Detection**: Checks for CSS classes like `.PCM-icon_waitfree` or text snippets such as "待てば￥0" to determine if the chapter is unlocked or unlockable.
    *   **New Flag**: Looks for the string `"NEW"` within the list item's text content.

---

## 4. Determining the "Latest" Chapter

Once all chapters are extracted into a list, the following logic is applied:

1.  **ID Normalization**: All chapter IDs are treated as integers.
2.  **Sorting**: The list is sorted in **ascending order** based on the ID.
3.  **Selection**: The **last element** of the sorted list (`chapter_list[-1]`) is designated as the current "Latest Chapter".

> [!NOTE]
> Piccoma IDs are generally sequential. Sorting by ID is a robust way to determine the release order even if the HTML listing is temporarily out of order or paginated.

---

## 5. Polling and Update Logic

The `AutoDownloadPoller` service manages the periodic checking of subscriptions.

### The Comparison Algorithm
The poller performs a simple equality check:
```python
latest_chapter = chapter_list[-1]
last_known = sub.get("last_known_chapter_id")
current_id = str(latest_chapter["id"])

if current_id != last_known:
    # Trigger Update Notification
```

### High-Frequency Polling
For platforms like Piccoma (JP), updates typically drop at **15:00 UTC** (00:00 JST). The poller enters a "High-Frequency" mode between 15:00 and 15:05 UTC, checking every 10 seconds to ensure the latest chapter is detected and processed as soon as it goes live.

---

## 6. Constraints and Edge Cases

### Region Locking
Piccoma (JP) is strictly geo-blocked. If the provider detects a "Japan only" warning in the response body (checked via `helpers.py`), it raises a `ScraperError`. Latest chapter detection will fail if not using a Japanese proxy/VPN.

### Authentication Sensitivity
The visibility of the latest chapter can sometimes depend on the account's region or subscription status. The provider performs a "CSRF Handshake" ritual before fetching series info to ensure the session is primed and the latest data is visible to the scraper.

### Wait-Free Detection
If the latest chapter is marked as `is_wait_free`, the system knows it can be "purchased" for 0 coins. This detection is crucial for the `fast_purchase` loop that follows the update detection.
