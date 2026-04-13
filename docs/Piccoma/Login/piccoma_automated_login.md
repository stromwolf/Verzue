# Piccoma Automated Login & Cookie Scraping Technical Deep-Dive

This document provides a standalone technical reference for the Piccoma automated login system. It covers everything a developer needs to know about how the bot maintains an authenticated identity without human intervention.

---

## 🔑 Credential Storage

The bot retrieves its credentials from a local JSON file:
- **Path**: `data/secrets/piccoma/account.json`
- **Structure**:
  ```json
  {
      "email": "your-email@example.com",
      "password": "your-password",
      "account_id": "primary"
  }
  ```

---

## 🚀 The Automated Login Flow (`login_service.py`)

When the `PiccomaProvider` detects a missing or expired session, it triggers the `auto_login` method in `app/services/login_service.py`.

### Step 1: CSRF Handshake
Before sending the login POST, the bot must visit the sign-in page (`/web/acc/email/signin`) to obtain a **`csrfmiddlewaretoken`**. Without this token, Piccoma will reject the login attempt with a 403 Forbidden error.

### Step 2: The Authentication POST
The bot constructs a form-encoded payload with the following fields:
- `csrfmiddlewaretoken`: The token scraped in Step 1.
- `email` & `password`: From `account.json`.
- `next_url`: Set to `/web` to ensure a smooth redirect after success.

### Step 3: The "Identity Handshake" (Warming)
Immediately after login, the bot **must** visit several pages to "settle" the cookies across different Piccoma domains:
1.  Visit `/web`
2.  Visit `/web/product/favorite`
3.  Include `asyncio.sleep(0.5)` between requests to mimic human browsing behavior.

**Why?** Piccoma uses behavioral tracking. Jumping directly to a purchase or a scraping task without this "warming" phase is a high-confidence signal of bot activity.

---

## 🍪 Automated Cookie Scraping

Once the login flow is complete, the bot scrapes the resulting cookies from the browser's session.

### 1. Jar Extraction (Primary Method)
The bot iterates through the `RequestsCookieJar` of the `curl_cffi` session. It captures the name, value, domain, and expiry for every cookie found. It specifically hunts for the **`pksid`**, which is the primary identity token.

### 2. Header Extraction Fallback (Secondary Method)
If the `pksid` is missing from the cookie jar (common when the server sets cookies on a domain/path different from the one currently active), the bot parses the **`Set-Cookie` headers** from the response object manually using regex:
```python
p_match = re.search(r'pksid=([^; ]+)', cookie_str)
```

### 3. Persistent Storage
Scraped cookies are sent to the `SessionService`, which stores them as a JSON array in **Redis**. This allows the bot to share a single authenticated session across multiple worker threads.

---

## 🛡️ Error Handling & Stability

### The 3-Tier Retry Bridge
The `auto_login` method uses a retry loop to handle network or proxy failures:
- **Maximum Retries**: 3
- **Wait Policy**: Exponential backoff (`wait_time = attempt * 2`) to let the proxy tunnel cool down after a failure.

### Success Verification
A login is only marked as **Successful** if:
1.  The response status is `200 OK` or a valid redirect (`302`).
2.  The "Login" page is **not** re-rendered (indicating a bad password or captcha).
3.  The **`pksid`** cookie is found in either the jar or the headers.

---

## 🛠️ Testing & Debugging

- **Force a Login**: Delete the Piccoma entry in Redis or modify the `account.json` to trigger an auth failure.
- **WAF Blocks**: If login fails repeatedly with `403`, the scraping proxy's IP may be flagged. Switch proxies in `.env` or check the `SCRAPING_PROXY` setting.
- **Diagnostic Dump**: If `DEVELOPER_MODE` is enabled in [piccoma.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/piccoma.py), extraction failures will dump the full HTML into `tmp/piccoma_dev/` for visual debugging.
