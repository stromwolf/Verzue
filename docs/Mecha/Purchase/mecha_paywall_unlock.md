# Mecha Comic: Technical Chapter Unlocking Overview

This document provides a comprehensive technical overview of the **Mecha Comic** provider's chapter unlocking mechanism, including the "Paywall" and "Coins" (Point-based) purchasing systems. It details the "Discovery Matrix" used by the Verzue scraper to navigate complex access scenarios.

---

## 1. Authentication & Session Management

Before any unlocking attempt, the bot must ensure a valid authenticated session. This is handled by the `MechaLoginHandler` and `SessionService`.

- **Session Persistence**: Sessions are stored in the vault with cookies (domain: `.mechacomic.jp`).
- **Validation**: The backend periodically checks session health by hitting `https://mechacomic.jp/account`. If redirected to `/login`, the session is marked for healing.
- **Automated Login**: If no healthy session exists, the `LoginService` triggers an automated login:
    1. Fetches `https://mechacomic.jp/session/input`.
    2. Extracts the `authenticity_token` (CSRF) from the login form.
    3. Submits a POST request with credentials and the token.
    4. Captures and saves the resulting cookies.

---

## 2. The Discovery Matrix

Unlocking chapters in Mecha Comic is not a single-step process. The bot employs a multi-scenario "Discovery Matrix" to determine the most efficient path to access the content.

### Scenario A: Immediate Verification
The bot first checks if the chapter is already accessible (e.g., it was previously purchased or is currently in a "Free" state). 
- It attempts a `GET` request to `https://mechacomic.jp/chapters/{episode_id}`.
- If the final URL contains `contents_vertical`, or if the response body contains the viewer manifest link, the chapter is considered unlocked.

### Scenario B: Free/Read Button Discovery
If Scenario A fails, the bot looks for explicit "Read" or "Free" buttons.
- **Selectors**: `a.c-btn-read-end`, `a.c-btn-free`, `a.js-bt_read`.
- **Action**: If found, the bot follows the `href` link. This often triggers a server-side session update that grants temporary access.

### Scenario C: Wait-Free (Timed) Handling
Mecha's "Wait-Free" (待てば¥0) mechanism is usually treated as a specialized case of Scenario B or D.
- If a "Wait-Free" read button is present, it is often a simple link that redirects to the viewer once clicked.
- In some cases, it may require a form submission (similar to Scenario D).

### Scenario D: Point-Based / Form-Based Purchase (Paywall)
This is the core logic for bypassing the paywall or spending coins.
- **Trigger**: The bot identifies a purchase button within the `.p-buyConfirm-currentChapter` container.
- **Selectors**: `input.js-bt_buy_and_download`, `input.c-btn-buy`.
- **Mechanism**:
    1. **Form Extraction**: The bot identifies the parent `<form>` of the buy button.
    2. **CSRF Extraction**: It retrieves the `authenticity_token` (see section 3).
    3. **Payload Construction**: It maps all hidden inputs (e.g., `book_id`, `chapter_id`) and adds the button's own value (e.g., `buy_and_download`).
    4. **Submission**: The bot submits a `POST` (usually) or `GET` to the form's `action` URL (typically `/chapters/{id}/download`).

---

## 3. CSRF Protection (Authenticity Token)

Mecha Comic uses a strict Rails-style `authenticity_token` for all state-changing requests (Login, Purchase, Favoriting). The bot uses an **S-Grade Multi-Source Pattern** to ensure the token is captured:

1.  **Hidden Input**: `input[name="authenticity_token"]`
2.  **Meta Tags**: `meta[name="csrf-token"]`
3.  **Regex Search**: If both fail, the bot searches the response body for `authenticity_token : "..."` strings (common in JS blocks).

---

## 4. Transition to Viewer (The "Unlocked" State)

Once the purchase/unlock request is accepted, the server redirects the bot or provides a **Viewer URL**.

### Key Viewer Parameters:
A successful unlock results in a URL with the following structure:
`https://mechacomic.jp/viewer?contents_vertical={MANIFEST_URL}&directory={CDN_BASE}&cryptokey={KEY_ENDPOINT}&ver={VERSION}`

- **contents_vertical**: A URL to a JSON manifest containing page indices and image filenames.
- **directory**: The base CDN path for image downloads.
- **cryptokey**: The endpoint (or the hex key itself) used to retrieve the decryption key.

---

## 5. Security & Decryption (Post-Purchase)

Even after the paywall is bypassed, images are protected by AES-CBC DRM.

1.  **Key Retrieval**: The bot fetches the decryption key from `/viewer_cryptokey/chapter/{id}`.
2.  **Image Download**: Images are retrieved from the `directory`.
3.  **Decryption Workflow**:
    - **Header**: The first 16 bytes of the downloaded binary are the **Initialization Vector (IV)**.
    - **Ciphertext**: The remaining bytes are the encrypted PNG data.
    - **Cipher**: AES-CBC with the retrieved key and extracted IV.
    - **Padding**: PKCS7 unpadding is applied to the decrypted plaintext.

---

## 6. Summary of Key Endpoints

| Purpose | URL Pattern |
| :--- | :--- |
| **Login Page** | `https://mechacomic.jp/session/input` |
| **Chapter Detail** | `https://mechacomic.jp/chapters/{id}` |
| **Purchase/Unlock** | `https://mechacomic.jp/chapters/{id}/download` |
| **Decryption Key** | `https://mechacomic.jp/viewer_cryptokey/chapter/{id}` |
| **Alerts/Updates** | `https://mechacomic.jp/alerts?content=chapter&type=book` |

> [!IMPORTANT]
> To maximize resilience, the bot always uses the `curl_cffi` library to impersonate a modern Chrome browser to avoid environment-based rejection (e.g., TLS fingerprinting).
