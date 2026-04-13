# Mecha Automated Login Reference

This document provides a technical reference for the Mecha Comic automated login system. It covers how the Verzue bot maintains, validates, and heals authenticated sessions without human intervention.

## 1. Overview
The Mecha automated login system ensures that the provider always has access to "HEALTHY" sessions (cookies) required for scraping book info, chapters, and performing purchases. It follows a multi-layered approach:
1. **Session Vaulting**: All active cookies are stored in a Redis-backed session service.
2. **Automated Healing**: If no healthy sessions are found, the system triggers a headless login flow to generate new cookies.
3. **Continuous Validation**: Every request validates the current session's health, reporting failures to trigger immediate rotation or healing.

---

## 2. Core Components

### A. MechaProvider (`app/providers/platforms/mecha.py`)
The high-level logic that coordinates session usage.
- **`_get_authenticated_session()`**: The entry point for all authenticated requests. It attempts to pull a healthy session from the vault.
- **`is_session_valid(session)`**: Performs a check by visiting `https://mechacomic.jp/account`. If the response redirects to `/login`, the session is marked as `EXPIRED`.

### B. LoginService & MechaLoginHandler (`app/services/login/`)
The "Healer" that performs the automated login.
- **`MechaLoginHandler.login(creds)`**:
    1. **Impersonation**: Uses `curl_cffi` with `chrome120` impersonation to mimic a real browser and bypass basic bot detection.
    2. **CSRF Extraction**: Visits the login page to extract the `authenticity_token` from the HTML form.
    3. **Handshake**: Performs a `POST` request to the session endpoint with credentials and the token.
    4. **Cookie Processing**: Captures the resulting cookies and updates the `SessionService`.

### C. SessionService (`app/services/session_service.py`)
Manages the lifecycle of sessions across all accounts.
- **Storage**: Uses Redis for persistence, allowing sessions to survive bot restarts.
- **Health Tracking**: Maintains a status of `HEALTHY` or `EXPIRED` for each session.
- **Concurrency Control**: Implements a global `refresh_lock` to prevent multiple threads from attempting automated login simultaneously for the same platform.

### D. SessionHealer (`app/services/session_healer.py`)
A background service that listens for session failure events.
- **Event Listener**: Subscribes to the `verzue:events:session` Redis channel.
- **Automatic Dispatch**: When a `session_expired` event is received, it automatically triggers `LoginService.auto_login`.
- **Behavioral Rituals**: After a successful login, it executes a "Ritual" to mimic human behavior (browsing the home page, free section, etc.) to harden the session against detection.

---

## 3. The Authentication Lifecycle

### Step 1: Session Retrieval
When a task (e.g., scraping a chapter) begins, `MechaProvider` calls `_get_authenticated_session`.
- It asks `SessionService` for a session.
- `SessionService` performs **Random Rotation** among all sessions marked `HEALTHY` in the Redis vault.

### Step 2: Session Validation & Reporting
During scraping, the bot monitors for authentication kicks:
- If a request is redirected to `/login`, or if `is_session_valid` fails, the session is reported as failed via `report_session_failure`.
- The session is marked `EXPIRED` in Redis.
- A **Telemetry Event** (`session_expired`) is published to Redis.

### Step 3: Automated Healing
The `SessionHealer` (running in the background) picks up the `session_expired` event:
1. It acquires a platform-specific **Refresh Lock**.
2. It calls `LoginService.auto_login("mecha")`.
3. The system loads credentials from `data/secrets/mecha/account.json`.
4. `MechaLoginHandler` performs the HTTP handshake and saves new cookies to Redis.
5. **Post-Login Ritual**: The healer visits several pages (`/`, `/free`, `/account`) with random delays to establish a "human" browsing history for the new session.

---

## 4. Technical Details

### Browser Impersonation
Mecha Comic has strict environment checks. To bypass these, the bot uses `curl_cffi` which provides:
- TLS fingerprinted headers.
- HTTP/2 support.
- Browser-accurate `User-Agent` and `Sec-CH` headers (typically `chrome120`).

### Cookie Persistence
When cookies are captured, the bot ensures they are applied to both the main domain and wildcards:
- Domain: `mechacomic.jp`
- Wildcard: `.mechacomic.jp`

### Behavioral Rituals (`run_ritual`)
To prevent "Ghost Sessions" (sessions that only visit API endpoints), the bot executes a ritual:
- **Home Visit**: Loads the landing page.
- **Discovery Visit**: Visits the `/free` section.
- **Account Heartbeat**: Visits `/account` to confirm final status.
- **Gaussian Jitter**: Random delays are added between steps to simulate human reading speed.

### Credential Storage
Credentials used for automated login are stored locally in the following path:
- `Path: data/secrets/mecha/account.json`
- **Format**:
```json
{
    "email": "example@email.com",
    "password": "password123",
    "account_id": "primary"
}
```

---

## 5. Failure & Recovery Summary

| Failure Scenario | Logic Response |
| :--- | :--- |
| **No healthy sessions in Redis** | Trigger `auto_login` immediately. |
| **Redirected to `/login`** | Mark session `EXPIRED`, `SessionHealer` triggers auto-rotation/healing. |
| **403 Forbidden / WAF Block** | Mark session as `WAF_BLOCK`, trigger backoff / proxy rotation. |
| **CSRF Token Missing** | Log error, retry login with updated selectors in `MechaLoginHandler`. |

---

## 6. Key Files for Developers
- **Login Logic**: [mecha_login.py](file:///e:/Code%20Files/Verzue/app/services/login/mecha_login.py)
- **Healing & Rituals**: [session_healer.py](file:///e:/Code%20Files/Verzue/app/services/session_healer.py)
- **Session Management**: [session_service.py](file:///e:/Code%20Files/Verzue/app/services/session_service.py)
- **Provider Integration**: [mecha.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/mecha.py)
