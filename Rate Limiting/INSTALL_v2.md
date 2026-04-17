# Jumptoon Optimization + Rate-Limit Hardening — Final Guide

## Method summary (all three patches combined)

**Foreground (get_series_info):** Tail-first — page 1 + last page only.

**Background (fetch_more_chapters):** Parallel middle-pages.

**Rate-limit hardening (NEW):** 4-layer defense against IP ban / rate-limit:
1. **Process semaphore (6)** — caps in-flight Jumptoon page requests globally
2. **Per-series lock + 60s cache** — collapses duplicate concurrent requests
3. **Redis token bucket (8/s, 15 burst)** — cross-process fairness
4. **429/503 backoff with jitter** — graceful degradation, honors Retry-After

---

## The threat model (why this matters)

Before the fix, this scenario would trip rate limits:

```
T=0.0s  User A opens series X (tail-first) → 2 requests
T=0.1s  User A's UI appears → background scan fires → 4 requests parallel
T=0.5s  User B opens series Y → 2 requests
T=0.6s  User B's background → 4 requests parallel
T=0.8s  User C opens series Z → 2 requests
T=1.0s  Poller cycle starts, checks 5 subs → 10 requests
─────────────────────────────────────────────────────────
Total:  24 requests to jumptoon.com in ~1s from same IP
Result: 429 / PX_403 / possibly IP cooldown
```

After the fix:

```
T=0.0s  User A opens series X — all gets routed through gated GET
        → Redis bucket: 8 tokens consumed, 7 remaining
        → Process semaphore: 6 slots, queues the rest
T=0.1s  User A's background scan — waits for semaphore slots as A's
        foreground releases them. Never more than 6 in flight globally.
T=0.5s  User B opens series Y — same gating applies
T=0.8s  User C opens series Z — if Z == X or Y (popular series),
        Layer 2 cache hits instantly (0 requests)
T=1.0s  Poller — waits for token bucket, naturally paces itself
─────────────────────────────────────────────────────────
Sustained: ≤8 req/s, ≤6 concurrent. Burst-safe, IP-safe.
```

---

## Files changed (final list)

### 1. `app/providers/platforms/jumptoon.py`

**At module top (after imports, before class):**
- Add `JUMPTOON_METADATA_SEMAPHORE = asyncio.Semaphore(6)`
- Add `_SERIES_LOCKS`, `_SERIES_LOCKS_LOCK`, `_SERIES_CACHE`, `_SERIES_CACHE_TTL`
- Add helper functions `_get_series_lock`, `_cache_get`, `_cache_put`

**In `JumptoonProvider.__init__`:**
- Add `self.redis = RedisManager()` line

**Add new method to class:**
- `_jumptoon_gated_get(self, auth_session, url, ...)` — THE CHOKE POINT
- `_compute_backoff(attempt)` — staticmethod for exponential backoff

**Modify existing methods** (replace all raw `auth_session.get(...)` calls with `self._jumptoon_gated_get(auth_session, ...)`):
- `get_series_info` → split into `get_series_info` (cache layer) + `_fetch_series_info_uncached` (actual fetch)
- `fetch_more_chapters` → remove local semaphore, use gated GET
- `sync_latest_chapters` → use gated GET

**Unchanged:**
- `_get_authenticated_session`, `_extract_tag_ids`, `_parse_page_data`
- `_extract_sort_key` (from Patch 1)
- `scrape_chapter` and its image-download loop (different concern — uses `_download_semaphore`)
- `fast_purchase`, `get_new_series_list`

---

### 2. `app/bot/common/view.py`

Same as Patch 2 — no changes from rate-limit hardening. The provider is the single choke point.

---

### 3. Poller (`app/tasks/poller.py`)

**No changes required.** Poller calls `get_series_info` which now has:
- Cache layer (avoids redundant fetches for same series within 60s)
- Gated GETs (naturally paced by token bucket)

If you have a high-frequency poll loop during premiere windows, consider
bumping Redis token rate temporarily to `rate=15, capacity=25` during that window.
This is a one-line change in `_jumptoon_gated_get` if needed.

---

## Tuning dials (where to adjust if needed)

| Knob | Location | Default | When to change |
|---|---|---|---|
| Process semaphore | `JUMPTOON_METADATA_SEMAPHORE` | `6` | Raise if users complain of slow UI during peak, lower if you still see 429s |
| Redis token rate | `_jumptoon_gated_get` → `rate=8` | `8/s` | Raise carefully — observe Retry-After headers in logs first |
| Redis burst capacity | `_jumptoon_gated_get` → `capacity=15` | `15` | Raise if legitimate spikes (user batches 20 chapters) get throttled |
| Cache TTL | `_SERIES_CACHE_TTL` | `60s` | Lower if users complain of stale chapter lists, raise for more dedup |
| Retry count | `_jumptoon_gated_get` → `max_retries=2` | `2` | Rarely touched — 2 retries is standard for transient 429s |

---

## Observability (how to verify it's working)

All rate-limit events log with recognizable prefixes. Grep patterns:

```bash
# Token bucket pacing (normal, expected during load)
grep "Token bucket wait" logs/bot.log

# Per-series cache hits (should trend up with user activity)
grep "Series cache HIT" logs/bot.log

# 429/backoff events (should be RARE; if frequent, lower rate)
grep "HTTP 429\|HTTP 503\|Proxy block" logs/bot.log

# Hard failures (should be nearly zero)
grep "Jumptoon rate limit sustained" logs/bot.log
```

Add a health metric if desired: count of tokens consumed per minute vs.
retries triggered. Ratio > 20:1 = healthy. Ratio < 5:1 = too aggressive.

---

## Rollback plan

Each layer is independently rollback-safe:

- **Disable Layer 3 (Redis)** → set `rate=9999, capacity=9999` — bucket always hands out tokens
- **Disable Layer 2 (cache)** → set `_SERIES_CACHE_TTL = 0.001` — cache never hits
- **Disable Layer 1 (semaphore)** → set `JUMPTOON_METADATA_SEMAPHORE = asyncio.Semaphore(9999)`
- **Disable Layer 4 (backoff)** → set `max_retries=0` — no retries

Full rollback: revert the three patches in reverse order (3 → 2 → 1). No DB
migrations, no new Redis keys beyond what `get_token` already creates, no
config env vars changed.

---

## Why this composition beats alternatives

**Why not just a bigger semaphore?**
A semaphore is in-process only. When you scale to Bot + Worker processes, both processes can fire at full capacity simultaneously against the same IP. Redis token bucket is the cross-process piece.

**Why not just the token bucket?**
Token buckets smooth the average rate but allow instantaneous bursts up to capacity. Without the semaphore, 15 users could still fire 15 parallel requests in the same millisecond. Semaphore caps the concurrency ceiling.

**Why not just per-series lock?**
That only helps when users happen to open the same series. Different series → no dedup benefit. Need the global layers for heterogeneous load.

**Why both cache AND lock?**
Lock alone: 5 users open same series at same instant → 1 fetches, 4 wait, 4 fetch after unlock = 5 total fetches. Lock + cache: 1 fetches, writes cache, 4 wake up and hit cache = 1 total fetch.

**Why 8 req/s specifically?**
Jumptoon is a Next.js app on Vercel/Cloudflare. Default Cloudflare enterprise rate limit is ~50/s per IP, but shared scraping proxies impose their own caps (~20/s typical). 8/s leaves 2.5× headroom for image downloads to coexist on the same IP without contention.
