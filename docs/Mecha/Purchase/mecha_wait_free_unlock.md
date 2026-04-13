# Mecha Comic: Wait-Free Chapter Unlocking Overview

This document provides a comprehensive technical overview of the **Mecha Comic** provider's mechanism for unlocking "Wait-Free" (待てば¥0) or "Daily Free" (毎日無料) chapters. It details how the Verzue scraper identifies, authenticates, and executes the unlock process without manual intervention.

---

## 1. Technical Context: What is "Wait-Free"?

In the Mecha Comic ecosystem, "Wait-Free" refers to chapters that are typically paywalled but can be accessed for free at specific intervals (e.g., daily) or as part of a "Free Serialization" (無料連載) promotion. 

Technically, these chapters are treated as **0-Point Purchases**. The bot must perform a "purchase" handshake, but the point balance is not decremented. Once unlocked, access is usually granted for a 72-hour window.

---

## 2. Authentication Foundation

Unlocking wait-free chapters requires a valid authenticated session. Mecha Comic employs strict session-based access control.

### Session Management
- **Cookies**: The bot uses a persistent session cookie (`pksid` or similar) stored in the session vault.
- **Domain Scope**: Cookies must be valid for `.mechacomic.jp`.
- **Validation**: Before an unlock attempt, the bot verifies session health by checking the `/account` endpoint. If the server redirects to `/login`, the session is considered "kicked" and requires healing via the `LoginService`.

### CSRF Protection (`authenticity_token`)
Mecha Comic is built on Ruby on Rails and uses a strict `authenticity_token` for all POST requests, including unlocking free chapters. The bot extracts this token using a multi-source discovery pattern:
1.  **Hidden Input**: `input[name="authenticity_token"]` within the purchase form.
2.  **Meta Tag**: `meta[name="csrf-token"]` in the page header.
3.  **Regex Fallback**: Scanning the body for `authenticity_token: "..."` in JavaScript blocks.

---

## 3. The Unlocking Workflow

The process of unlocking a wait-free chapter is encapsulated in the `fast_purchase` method within the `MechaProvider` class.

### Step 1: Scenario Identification
The bot first scans the chapter's container (usually `.p-buyConfirm-currentChapter`) for specific UI elements that indicate an unlockable state.

| UI Element | CSS Classes | Purpose |
| :--- | :--- | :--- |
| **Read Link** | `a.c-btn-read-end`, `a.c-btn-free`, `a.js-bt_read` | Direct link to viewer/unlock for already free chapters. |
| **Unlock Button** | `input.c-btn-free`, `button.c-btn-read-end` | Triggers the 0-point purchase form. |
| **Purchase Button** | `input.js-bt_buy_and_download`, `input.c-btn-buy` | Triggers a point-based purchase (used if free options fail). |

### Step 2: Form Discovery & Extraction
If a "Free" or "Read" button is identified, the bot locates its parent `<form>` element to extract the necessary metadata for the unlock request:
- **Action URL**: Typically `/chapters/{chapter_id}/download`.
- **Method**: Almost always `POST`.
- **Payload**: Includes hidden inputs like `book_id`, `chapter_id`, and the `authenticity_token`.

### Step 3: Submission Handshake
The bot construct a POST request (mimicking a browser form submission):
```python
# Conceptual payload construction
payload = {
    "authenticity_token": extracted_token,
    "book_id": series_id,
    "chapter_id": real_id,
    "purchase_type": "download_only" # Example
}
headers = {
    "Referer": f"https://mechacomic.jp/chapters/{real_id}",
    "Origin": "https://mechacomic.jp"
}
response = await auth_session.post(action_url, data=payload, headers=headers)
```

### Step 4: Redirect to Viewer
A successful unlock request results in a `302 Redirect` or a `200 OK` response where the target URL (or the response body) contains the **Viewer URL**. The bot specifically looks for the `contents_vertical` parameter, which signifies that the chapter is now accessible.

---

## 4. Manifest Retrieval and Image Decryption (Post-Unlock)

Once the chapter is unlocked, the bot transitions to the content acquisition phase.

### The Viewer Manifest
The bot parses the viewer URL to extract the `contents_vertical` JSON manifest. This manifest contains:
- List of image filenames (e.g., `001.png`).
- Image dimensions and metadata.
- CDN base directory (`directory` parameter).

### DRM Decryption (AES-CBC)
Even "Free" chapters are protected by Mecha's proprietary DRM.
1.  **Key Acquisition**: The bot fetches a unique decryption key from `https://mechacomic.jp/viewer_cryptokey/chapter/{id}`.
2.  **Binary Processing**:
    - The first **16 bytes** are the Initialization Vector (IV).
    - The rest is the encrypted image data.
3.  **Transformation**: The data is decrypted using **AES-128-CBC** and unpadded using **PKCS7**.

---

## 5. Summary of Unlocking Logic (The "Discovery Matrix")

The bot follows a cascaded priority list to ensure the highest success rate:

1.  **Direct Check**: Try to access the chapter URL directly. If redirected to the viewer, it's already unlocked.
2.  **GET Link**: If a `c-btn-read-end` link exists, follow it.
3.  **POST Form**: If a `c-btn-free` form exists, submit it with the CSRF token.
4.  **Fallback Download**: If no viewer URL is found in the response, try hitting `https://mechacomic.jp/chapters/{id}/download` directly as a final effort.

---

> [!IMPORTANT]
> **Anti-Bot Resilience**: To prevent session rejection, the bot uses `curl_cffi` to impersonate a Chrome browser, maintaining consistent TLS fingerprints and headers throughout the unlocking handshake.
