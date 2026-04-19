"""
Global Platform Rate Limiter
─────────────────────────────
Enforces a GLOBAL ceiling across ALL consumers of a platform (downloads,
metadata, $re-schedule, poller — everything). No single operation can
starve or blow past the ceiling.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import ClassVar

logger = logging.getLogger("PlatformRateLimiter")


@dataclass
class PlatformLimits:
    rate: int          # tokens / second (sustained)
    capacity: int      # burst ceiling (token bucket max)
    concurrency: int   # max simultaneous in-flight requests


# ── Defaults per platform ────────────────────────────────────────────────────
PLATFORM_DEFAULTS: dict[str, PlatformLimits] = {
    "jumptoon": PlatformLimits(rate=10, capacity=12, concurrency=6),
    "piccoma":  PlatformLimits(rate=10, capacity=10, concurrency=4),
    "mecha":    PlatformLimits(rate=10, capacity=8,  concurrency=4),
}

# Fallback for unknown platforms
_DEFAULT_LIMITS = PlatformLimits(rate=10, capacity=8, concurrency=4)


class PlatformRateLimiter:
    """
    Process-wide singleton per platform.
    
    Usage:
        limiter = PlatformRateLimiter.get("jumptoon")
        async with limiter.acquire():
            response = await session.get(url)
    """

    _instances: ClassVar[dict[str, "PlatformRateLimiter"]] = {}

    @classmethod
    def get(cls, platform: str) -> "PlatformRateLimiter":
        """Return the singleton limiter for this platform."""
        key = platform.lower()
        if key not in cls._instances:
            limits = PLATFORM_DEFAULTS.get(key, _DEFAULT_LIMITS)
            cls._instances[key] = cls(platform=key, limits=limits)
            logger.info(
                f"[RateLimiter] 🔧 Created limiter for '{key}': "
                f"{limits.rate} req/s, burst={limits.capacity}, "
                f"concurrency={limits.concurrency}"
            )
        return cls._instances[key]

    def __init__(self, platform: str, limits: PlatformLimits):
        self.platform    = platform
        self.limits      = limits
        self._semaphore  = asyncio.Semaphore(limits.concurrency)

        # Local token bucket (fallback when Redis is unavailable)
        self._tokens     = float(limits.capacity)
        self._last_refill = time.monotonic()
        self._bucket_lock = asyncio.Lock()

    # ── Public context manager ───────────────────────────────────────────────

    class _AcquireContext:
        def __init__(self, limiter: "PlatformRateLimiter"):
            self._limiter = limiter

        async def __aenter__(self):
            await self._limiter._wait_for_token()
            await self._limiter._semaphore.acquire()
            return self

        async def __aexit__(self, *_):
            self._limiter._semaphore.release()

    def acquire(self) -> "_AcquireContext":
        """
        Usage:
            async with PlatformRateLimiter.get("jumptoon").acquire():
                ...
        """
        return self._AcquireContext(self)

    # ── Token bucket (local fallback) ────────────────────────────────────────

    async def _wait_for_token(self):
        """
        Primary: tries Redis token bucket (via RedisManager).
        Fallback: local asyncio token bucket.
        Both respect the GLOBAL per-platform cap.
        """
        # Try Redis first (shared across processes / workers if ever multi-process)
        try:
            from app.services.redis_manager import RedisManager
            redis = RedisManager()
            bucket_key = f"platform:global:{self.platform}"

            while True:
                allowed, wait_time = await redis.get_token(
                    bucket_key,
                    rate=self.limits.rate,
                    capacity=self.limits.capacity
                )
                if allowed:
                    return
                sleep_for = min(float(wait_time or 0.1), 2.0)
                logger.debug(
                    f"[RateLimiter] ⏳ {self.platform} global bucket full "
                    f"— waiting {sleep_for:.3f}s"
                )
                await asyncio.sleep(sleep_for)

        except Exception as e:
            logger.debug(f"[RateLimiter] Redis unavailable ({e}), using local bucket")
            await self._local_bucket_wait()

    async def _local_bucket_wait(self):
        """Pure-asyncio token bucket, used when Redis is down."""
        while True:
            async with self._bucket_lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    float(self.limits.capacity),
                    self._tokens + elapsed * self.limits.rate
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait_for = (1.0 - self._tokens) / self.limits.rate

            await asyncio.sleep(wait_for)

    # ── Diagnostics ──────────────────────────────────────────────────────────

    @property
    def concurrency_available(self) -> int:
        """How many concurrent slots are free right now."""
        return self._semaphore._value   # type: ignore[attr-defined]

    def __repr__(self):
        return (
            f"<PlatformRateLimiter platform={self.platform!r} "
            f"rate={self.limits.rate}/s capacity={self.limits.capacity} "
            f"concurrency={self.limits.concurrency} "
            f"free_slots={self.concurrency_available}>"
        )
