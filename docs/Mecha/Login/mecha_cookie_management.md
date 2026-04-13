# Mecha Session Cookie & Bot Detection Management

This document provides a comprehensive technical overview of how the Verzue bot manages Mecha Comic session cookies to maintain authenticated identities and bypass sophisticated bot detection mechanisms.

## 1. Authentication Architecture

Mecha Comic utilizes a Ruby on Rails backend with session-based authentication. The bot maintains an "Authenticated Identity" by capturing, storing, and rotating session cookies that represent logged-in accounts.

### Key Authentication Tokens
- **`_mecha_session`**: The primary Rails session cookie containing the encrypted session data.
- **`authenticity_token`**: A CSRF (Cross-Site Request Forgery) token required for all state-changing requests (Login, Purchases, Favorites).
- **`remember_me`**: A persistent cookie (when enabled) that allows the session to remain valid for 30 days.

---

## 2. Cookie Management Workflow

### A. Headless Acquisition
The bot performs a "handshake" to acquire fresh cookies without human intervention:
1. **Initial Visit**: The bot visits `https://mechacomic.jp/session/input` to initialize a guest session and extract the `authenticity_token` from the login form.
2. **Credential Handshake**: It submits a `POST` request with the email, password, and the extracted CSRF token.
3. **Capture**: Upon a successful 302/200 response, it extracts all cookies set by the server.

### B. Session Vaulting (Redis)
All cookies are stored in a centralized "Session Vault" (backed by Redis). This ensures:
- **Persistence**: Sessions survive bot restarts.
- **Concurrency**: Multiple worker threads can share the same authenticated identity.
- **Auto-Rotation**: The bot can rotate between multiple accounts to avoid rate limits.

### C. Domain-Specific Application
When applying cookies to a new request session, the bot ensures "Double-Domain Coverage" to prevent authentication drops:
- It sets the cookie for the apex domain: `mechacomic.jp`
- It sets the cookie for the wildcard domain: `.mechacomic.jp`

---

## 3. Bot Detection Bypass Techniques

Mecha Comic employs several layers of security to detect automated traffic. Verzue circumvents these using the following strategies:

### A. TLS & JA3 Fingerprinting (Impersonation)
Most scrapers fail because their TLS handshake (JA3 fingerprint) does not match a real browser. Verzue uses `curl_cffi` with a specific **`chrome120` impersonation**:
- **TLS Handshake**: Emulates the exact cipher suites, extensions, and protocols used by Google Chrome.
- **HTTP/2**: Correctly implements HTTP/2 window sizes and settings to mimic real browser behavior.

### B. Modern Browser Headers
Every request includes a full set of "Secure Client Hints" and fetch metadata:
- **`User-Agent`**: A modern Chrome string (e.g., `Chrome/120.0.0.0`).
- **`Sec-Ch-Ua`**: Correctly formatted browser version hints.
- **`Sec-Fetch-Dest/Mode/Site`**: Metadata that tells the server whether the request is a navigation, an image fetch, or a form submission.
- **`Referer` Logic**: The bot dynamically sets the `Referer` header (e.g., setting it to the book info page when accessing a chapter) to fulfill security checks.

### C. Behavioral Rituals (Human Emulation)
To avoid being flagged as a "headless zombie" (an account that only hits API/Download endpoints), the bot executes a **Ritual** after login:
1. **Home Visit**: Loads the landing page (`/`).
2. **Discovery Visit**: Visits the `/free` section to simulate a user looking for content.
3. **Account Heartbeat**: Visits `/account` to check status.
4. **Gaussian Jitter**: Uses `asyncio.sleep` with a Gaussian distribution (randomized timing) between requests to simulate human interaction speeds.

---

## 4. Session Validation & Healing

### Validation Loop
Before performing sensitive actions (like unlocking a chapter), the bot validates the session:
- **URL Check**: The bot hits `https://mechacomic.jp/account`.
- **Redirect Detection**: If the server redirects the bot to `/login`, the session is marked as `EXPIRED`.

### Automated Healing
Once a session is marked `EXPIRED`:
1. **Reporting**: The `SessionService` registers a failure for that specific account.
2. **Locking**: A platform-wide "Refresh Lock" is acquired to prevent multiple workers from trying to login at once.
3. **Re-Authentication**: The `MechaLoginHandler` is triggered to perform a fresh login and update the Vault with new cookies.

---

## 5. Summary Table: Detection vs. Bypass

| Security Layer | Detection Method | Verzue Bypass Strategy |
| :--- | :--- | :--- |
| **Transport Layer** | JA3/TLS Fingerprinting | `curl_cffi` Chrome 120 Impersonation |
| **Request Protocol** | HTTP/1.1 vs HTTP/2 checks | Unified HTTP/2 stack emulation |
| **Environment** | Missing `Sec-CH` headers | Full Browser Header Spoofing |
| **CSRF** | `authenticity_token` validation | Automated HTML parsing & extraction |
| **Behavioral** | Direct API Access (No HTML) | Multi-step "Ritual" browsing flow |
| **IP-Based** | Rate limiting / Geo-blocking | Proxy rotation & Session Health monitoring |

---

## 6. Implementation References
- **Login Handler**: [mecha_login.py](file:///e:/Code%20Files/Verzue/app/services/login/mecha_login.py)
- **Cookie Usage**: [mecha.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/mecha.py)
- **Vault Service**: [session_service.py](file:///e:/Code%20Files/Verzue/app/services/session_service.py)
