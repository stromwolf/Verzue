# Piccoma "Wait-Free" (待てば¥0) Unlocking Technical Deep-Dive

This document provides a technical overview of how the Piccoma provider handles the unlocking of "Wait-Free" chapters. Use this as a reference when debugging or refactoring the `app/providers/platforms/piccoma.py` file.

---

## 🏗️ Architecture & Session Flow

The bot simulates a high-entropy browser session to avoid detection and ensure server-side state (like "Wait-Free" availability) is correctly accessible.

### 1. Session Retrieval (The Background)
Before any action, the bot retrieves an active, healthy session from **Redis**.
```python
# From PiccomaProvider.__init__
self.session_service = SessionService()

# From _get_authenticated_session
session_obj = await self.session_service.get_active_session("piccoma")
```
If no healthy session is found, the bot triggers an **Automated Login** using the credentials stored in `data/secrets/piccoma/account.json`.

### 2. Cookie Injection (The "Key" to Access)
Once a session is retrieved, the bot forces critical cookies into the `curl_cffi` request session. This is known as **Aggressive Injection**.
```python
# Injection Logic from piccoma.py
if name.lower() in ['pksid', 'csrftoken', 'csrf_token']:
    c_domain = ".piccoma.com"
    c_path = "/"
else:
    c_domain = c.get('domain') or region_domain
    c_path = c.get('path') or "/"

async_session.cookies.set(name, value, domain=c_domain, path=c_path)
```
**Why?** Browsers naturally hide cookies based on their original subdomain (e.g., `auth.piccoma.com`). By forcing the path to `/` and the domain to `.piccoma.com`, we ensure the server sees the **`pksid`** (our identity) on every request.

---

## 🛡️ Identity Audit

Before attempting a "Wait-Free" unlock, the bot performs a **Guest Check**.
```python
# Guest Detection Logic from fast_purchase
is_guest = bool(soup.select_one('.PCM-headerLogin, a[href*="/acc/signin"]'))
if is_guest:
    logger.error("🛑 [Piccoma Identity] Browser shows LOGIN button. Session is guest or expired!")
    # Trigger automated login
    await self.session_service.report_session_failure("piccoma", "primary", "Session expired/Guest detected")
```
If the login button (`.PCM-headerLogin`) is visible, the server has rejected our cookies, and the bot must re-authenticate.

---

## 🔒 Security Tokens & Hashing

Piccoma uses multiple layers of security to validate purchase requests.

### 1. CSRF Multi-Tier Extraction
The bot uses a heuristic approach to find the CSRF token:
1.  **Form Data**: Primary check in `js_purchaseForm`.
2.  **Meta Tags**: `meta[name="csrf-token"]`.
3.  **Config Scripts**: Regex extraction from `__p_config__` JavaScript objects.
4.  **Hidden Inputs**: Check for `csrfmiddlewaretoken`.

### 2. Security Hash Calculation
Requests require an `X-Security-Hash` (and `X-Hash-Code`) header.
- **Algorithm**: SHA-256
- **Salt/Seed**: `fh_SpJ#a4LuNa6t8`
- **Formula**: `hash = sha256(episode_id + "fh_SpJ#a4LuNa6t8")`
```python
import hashlib
seed_string = f"{episode_id}fh_SpJ#a4LuNa6t8"
sec_hash = hashlib.sha256(seed_string.encode('utf-8')).hexdigest()
headers['X-Security-Hash'] = sec_hash
```

---

## 🚀 The Discovery Loop (`fast_purchase`)

Because Piccoma's frontend and API endpoints can change based on series type (Smartoon vs Manga) or platform updates, the bot employs a **Trial-and-Error Discovery Loop**.

### Targeted Endpoints
1.  `/web/episode/waitfree/use` (Wait-Free specific)
2.  `/web/episode/purchase` (Coin-based / General)
3.  `/web/episode/use` (Alternative generic endpoint)

### Payload Variants
The bot tries the following combinations (both as **JSON** and **Form-Encoded**):
- `episodeId` + `productId` + `hash` + `csrfToken`
- `episode_id` + `product_id` + `hash` + `csrfmiddlewaretoken`

### Success Verification
After a POST trial, the bot verifies success by:
1.  Checking HTTP Status (`200 OK` or redirects).
2.  Parsing the JSON response for `result: "ok"`.
3.  **Recursive Re-fetch**: Attempting to load the viewer page and checking for the existence of `pData` (the image manifest).

---

## 🛠️ Troubleshooting & Failure Modes

| Error / Behavior | Likely Cause | Recommended Fix |
| :--- | :--- | :--- |
| **403 Forbidden** | Missing/Invalid CSRF or Security Hash. | Check salt in `fast_purchase` and CSRF extraction logs. |
| **Guest Detected** | `pksid` cookie missing or expired. | Refresh session via Dashboard or check `SessionService` logs. |
| **404 Not Found** | Incorrect endpoint URL. | Check if Piccoma changed the `/web/episode/...` API path. |
| **Manifest Failure** | Chapter "unlocked" but server didn't provide image data. | Increase wait time between purchase and re-fetch. |

---

## ✅ Manual Verification Steps

To manually verify the unlocking logic:
1.  **Find a series** with a "Wait-Free" (待てば¥0) chapter that you haven't unlocked.
2.  **Run the bot** on that specific URL.
3.  **Monitor logs** for `[Piccoma] Chapter XXX locked, attempting fast purchase/unlock`.
4.  **Check `tmp/piccoma_dev/`**: If `DEVELOPER_MODE` is True, the bot will dump the HTML of the failure page for inspection.
