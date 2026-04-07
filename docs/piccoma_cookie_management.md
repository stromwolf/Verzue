# Piccoma Cookie & Session Management Technical Deep-Dive

This document details how the bot handles Piccoma's session cookies to maintain an authenticated identity and bypass bot detection.

---

## 🍪 Key Cookies Breakdown

| Cookie Name | Purpose | Criticality |
| :--- | :--- | :--- |
| **`pksid`** | The main session identifier for Piccoma. Links the request to your account. Without this, you are a "Guest". | **EXTREME** (Required for everything) |
| **`csrftoken`** | Required for server-driven POST requests (e.g., in `fast_purchase`). | **HIGH** |
| **`csrfmiddlewaretoken`** | The internal CSRF token name used during the login handshake. | **HIGH** |
| **`q`** | Tracking/context cookie used primarily in Piccoma France. | **LOW** (Currently unused) |

---

## 🔄 The Cookie Lifecycle

### 1. Generation & Warming Up (`login_service.py`)
When a session expires or is missing, the `LoginService` performs an automated login using the [account.json](file:///e:/Code%20Files/Verzue/data/secrets/piccoma/account.json) credentials.

#### **Handshake & Auth POST**
The bot must visit the sign-in page to capture an initial `csrfmiddlewaretoken` before it can POST the credentials.
```python
# From _login_piccoma in login_service.py
payload = {
    "csrfmiddlewaretoken": csrf_token,
    "email": email,
    "password": password,
    "next_url": "/web"
}
post_res = await session.post(login_page_url, data=payload, headers=headers)
```

#### **Identity Handshake (The "Warming" Phase)**
Immediately after login, the bot **must** visit several pages (`/web`, `/web/product/favorite`) to "settle" the cookies across different Piccoma domains. This is critical for capturing the **`pksid`**.

---

### 2. Multi-tier Extraction
The bot uses a robust fallback method to extract cookies from the `curl_cffi` jar:

#### **Jar Extraction**
The bot iterates through the `RequestsCookieJar` to find all active cookies.
```python
# From login_service.py
jar_obj = session.cookies.jar
for domain in jar_obj._cookies:
    for path in jar_obj._cookies[domain]:
        for name, cookie in jar_obj._cookies[domain][path].items():
            # Extract pksid, domain, and path
```

#### **Header Fallback**
If `pksid` is still missing from the jar after login, the bot parses the raw **`Set-Cookie` headers** from the response object manually using regex:
```python
# Header Fallback Regex
p_match = re.search(r'pksid=([^; ]+)', cookie_str)
```

---

### 3. Persistent Storage
Cookies are stored as a JSON array in **Redis**. This allows multiple worker instances to share the same authenticated session.
```python
# Storage Signature
await self.session_service.update_session_cookies("piccoma", account_id, cookies)
```

---

### 4. Aggressive Injection (`piccoma.py`)
When making a request, the bot **force-injects** cookies into the active session.
```python
# Injection Logic from piccoma.py
if name.lower() in ['pksid', 'csrftoken', 'csrf_token']:
    c_domain = ".piccoma.com"  # Forced universal scope
    c_path = "/"
else:
    c_domain = c.get('domain') or region_domain
    c_path = c.get('path') or "/"
```
**Why?** Browsers hide cookies based on their original subdomain origins. By forcing the domain to `.piccoma.com` and the path to `/`, we ensure the server sees the **`pksid`** on every request.

---

## 🛠️ Identity Handshaking & "Warming"

The bot implements a "warming" phase after login ([login_service.py:L149-152](file:///e:/Code%20Files/Verzue/app/services/login_service.py#L149-152)). 
- Visits `/web`
- Visits `/web/product/favorite`
- Introduces tiny `asyncio.sleep()` delays between requests.

**Rationale**: Piccoma uses behavioral tracking and cross-domain redirects that may take several "hops" to fully set the final session cookies. Jumping straight to a chapter purchase after login is a "bot signal" that often results in a 403 Forbidden.

---

## 📡 Troubleshooting

### Identifying "Guest" Sessions
If research or unlocking fails with a `200 OK` but returns incomplete data, check the logs for:
> `🛑 [Piccoma Identity] Browser shows LOGIN button. Session is guest or expired!`

### Missing `pksid`
If a login is "successful" but the `pksid` is missing:
1.  Verify the `proxies` are working (IP blocking can cause 0-byte cookie returns).
2.  Check if the login page changed its CSRF field name.
3.  Check [login_service.py:L201](file:///e:/Code%20Files/Verzue/app/services/login_service.py#L201) to see if the **Header Extraction Fallback** is triggering.
