# Piccoma Paywall & Coin Chapter Unlocking Technical Overview

This document details how the Piccoma provider handles the unlocking of "Paywall" chapters—episodes that require the consumption of Coins or Points (as opposed to "Wait-Free" or "Free" chapters).

---

## 🔍 Chapter Identification

Chapters are categorized during the `get_series_info` and `scrape_chapter` phases:

- **Free**: No lock icon, accessible to guests.
- **Wait-Free (待てば¥0)**: Accessible after a timer (23h/72h) or by using a ticket.
- **Paywall / Coin**: Specifically refers to chapters where `is_locked` is true and `is_wait_free` is false. These typically represent the most recent chapters in a series.

### UI Indicators in HTML
The bot detects a paywall state in the viewer if:
- HTTP Status is `200 OK` (Piccoma doesn't 403 locked chapters; it serves a purchase UI).
- The string `js_purchaseForm` is present.
- The button text `チャージ中` (Charging) or `ポイントで読む` (Read with points) is found.
- The viewer URL redirects back to the `/web/product/` page.

---

## 🚀 The V2 Unlock Flow (Modern)

The current implementation primarily uses the Piccoma "V2" web APIs discovered via browser traffic analysis (HAR). This flow is handled by `PiccomaPurchase._try_v2_point_coin_unlock`.

### 1. The Access Handshake
Before any purchase call, the bot must perform a handshake. This registers the intended viewer access and prepares the session.

- **Endpoint**: `/web/user/access`
- **Method**: `POST`
- **Payload**:
  ```json
  {
    "product_id": "12345",
    "episode_id": "67890",
    "referrer_type": "",
    "current_episode_id": ""
  }
  ```
- **Importance**: This call is the most common trigger for an "Auth Kick" if the session is stale. The bot monitors this response to trigger session healing (re-login).

### 2. The Use API
After the handshake, the bot attempts to "use" the currency to unlock the chapter.

- **Endpoints**:
  - `/web/v2/point/use/{series_id}/{episode_id}`
  - `/web/v2/coin/use/{series_id}/{episode_id}`
- **Method**: `POST`
- **Headers**:
  - `x-csrftoken`: Extracted from cookies or meta tags.
  - `Referer`: The series episodes page.
- **Payload**: `{"is_discount_campaign": "N"}`

### 3. Currency Priority
The bot follows a tiered fallback strategy for Paywall chapters:
1.  **Points**: Tries the Point API first (useful for bonus points/membership points).
2.  **Coins**: Tries the Coin API if points are insufficient or fail.
3.  **Wait-Free**: Tries the Wait-Free API as a ghost-failure fallback.

---

## 🔒 Security Integrity

### CSRF Management
Piccoma/Django requires a valid CSRF token in the `x-csrftoken` header. The bot extracts this with high precedence for the **cookie value** (`csrftoken`), falling back to meta tags or hidden inputs.

### Security Hash (`X-Security-Hash`)
While primarily used for legacy and wait-free endpoints, the bot includes this for consistency across all POST requests.
- **Secret Salt**: `fh_SpJ#a4LuNa6t8`
- **Algorithm**: `SHA-256(episode_id + salt)`

---

## 🔄 The Discovery Matrix (Legacy Fallback)

If the modern V2 flow fails, the bot enters a **Discovery Matrix** in `fast_purchase`. This is an exhaustive trial-and-error loop across older API versions and different payload formats.

### Target Paths
- `/web/episode/purchase`
- `/web/episode/use`

### Trial Combinations
The matrix iterates through:
- **Encodings**: JSON vs `application/x-www-form-urlencoded`.
- **Key Schemas**: `episodeId`/`productId` vs `episode_id`/`product_id`.

---

## ✅ Unlocking Verification

Success is NEVER assumed based on an API `200 OK` or `result: "ok"`. 

The bot performs **Mandatory Manifest Verification**:
1.  Re-fetches the viewer URL (`/web/viewer/s/{series_id}/{episode_id}`).
2.  Passes the response text through `_extract_pdata_heuristic`.
3.  Only if the JSON manifest containing image paths is found is the chapter marked as **UNLOCKED**.

---

## 🛠️ Common Failure Modes

| Symptom | Detection | Cause |
200 OK but Manifest missing | `_extract_pdata_heuristic` returns None | Purchase API returned success but session was not updated server-side in time. |
| **Auth Kick (Redirect)** | ScraperError with "sign-in" in message | Session `pksid` is expired or rejected by the `/access` handshake. |
| **403 Forbidden** | Status code 403 on V2 Use API | CSRF token mismatch. The bot will automatically refresh the episodes page to rotate CSRF and retry once. |
| **Balance Exhausted** | 200 OK with `result: "ng"` or error message | Account has 0 points and 0 coins. The bot cannot bypass actual payment walls. |
