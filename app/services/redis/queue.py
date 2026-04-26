"""
app/services/redis/queue.py

S-Grade Reliable Queue Implementation.

CHANGES FROM PREVIOUS VERSION:
- BLPOP -> BLMOVE: tasks move atomically into a per-worker processing list
- ack_task(): explicit acknowledgement removes the task from the processing list
- nack_task(): explicit failure can either retry-with-backoff or send to dead-letter
- recover_orphans(): startup sweep that re-queues tasks left in dead workers'
  processing lists (handles SIGKILL, OOM kill, VPS reboot)
- Heartbeats on active_tasks hash so orphaned dedup keys self-expire
- Retry counter embedded in the task envelope, dead-letter after MAX_RETRIES

DESIGN NOTES:
- The "processing list" pattern is the canonical Redis reliable queue (documented
  in the BLMOVE/LMOVE man pages). It gives at-least-once delivery semantics.
- Each worker owns ONE processing list keyed by a stable worker_id (hostname:pid).
  This is critical: if you share a processing list across workers you can't
  distinguish "in-flight" from "orphaned".
- Recovery is a one-shot scan at boot: list all known worker keys, requeue any
  found in dead workers' lists. Live workers' lists are left alone.
"""

import json
import logging
import os
import socket
import time
import asyncio
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger("RedisManager.Queue")

# ---------------------------------------------------------------------------
# Constants — keep in sync with config/settings.py if you externalise these
# ---------------------------------------------------------------------------
GLOBAL_QUEUE = "verzue:queue:global"
DEAD_LETTER_QUEUE = "verzue:queue:dead"
PROCESSING_PREFIX = "verzue:processing:"   # + worker_id
WORKER_REGISTRY = "verzue:workers:alive"   # SET of currently-alive worker_ids
WORKER_HEARTBEAT_PREFIX = "verzue:worker:hb:"  # + worker_id, value = unix ts

ACTIVE_TASKS_HASH = "verzue:active_tasks"
ACTIVE_TASK_TTL_HASH = "verzue:active_tasks:ttl"  # parallel hash of expiry timestamps

MAX_RETRIES = 3
WORKER_HEARTBEAT_TTL = 60          # worker is "dead" if no heartbeat for 60s
ACTIVE_TASK_DEFAULT_TTL = 3600     # 1 hour orphan protection on dedup keys


def make_worker_id() -> str:
    """Stable per-process worker identifier. hostname:pid is enough on a single VPS;
    if you ever go multi-host, this still uniquely identifies the worker."""
    return f"{socket.gethostname()}:{os.getpid()}"


class RedisQueue:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client
        self.worker_id = make_worker_id()
        self.processing_list = f"{PROCESSING_PREFIX}{self.worker_id}"
        self._heartbeat_task: asyncio.Task | None = None

    # =========================================================================
    # WORKER LIFECYCLE
    # =========================================================================

    async def register_worker(self):
        """Call once when the worker process starts. Idempotent.
        Adds this worker to the alive registry and starts the heartbeat loop.
        """
        if not self.client:
            return
        try:
            await self.client.sadd(WORKER_REGISTRY, self.worker_id)
            await self._beat()
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"💓 Worker registered: {self.worker_id}")
        except (ConnectionError, TimeoutError) as e:
            logger.error(f"Worker registration failed: {e}")

    async def deregister_worker(self):
        """Call on graceful shutdown. Drains any in-flight tasks back to the
        global queue so a cold restart picks them up immediately rather than
        waiting on the orphan sweep."""
        if not self.client:
            return
        try:
            # Drain the processing list back to the head of the global queue
            # so these tasks get retried first (they were already in flight).
            drained = 0
            while True:
                raw = await self.client.lpop(self.processing_list)
                if not raw:
                    break
                await self.client.lpush(GLOBAL_QUEUE, raw)  # head, not tail
                drained += 1

            await self.client.srem(WORKER_REGISTRY, self.worker_id)
            await self.client.delete(f"{WORKER_HEARTBEAT_PREFIX}{self.worker_id}")
            logger.info(f"👋 Worker deregistered: {self.worker_id} (drained {drained} in-flight tasks)")

            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
        except (ConnectionError, TimeoutError) as e:
            logger.error(f"Worker deregistration failed: {e}")

    async def _beat(self):
        """Single heartbeat write."""
        await self.client.set(
            f"{WORKER_HEARTBEAT_PREFIX}{self.worker_id}",
            str(int(time.time())),
            ex=WORKER_HEARTBEAT_TTL,
        )

    async def _heartbeat_loop(self):
        """Refreshes the worker heartbeat every WORKER_HEARTBEAT_TTL/3 seconds.
        If this loop stops (process died), the heartbeat key TTL expires and the
        next orphan sweep claims this worker's processing list."""
        interval = max(WORKER_HEARTBEAT_TTL // 3, 5)
        while True:
            try:
                await asyncio.sleep(interval)
                if self.client and self.manager._is_connected:
                    await self._beat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Heartbeat write failed (will retry): {e}")

    # =========================================================================
    # ENQUEUE / DEQUEUE
    # =========================================================================

    async def enqueue_task(self, queue_name: str, task_dict: dict):
        """Pushes a serialised task to the back of the queue. Wraps the user
        payload in an envelope so we can carry retry/timestamp metadata without
        touching the ChapterTask model."""
        if not self.client:
            return False
        envelope = {
            "payload": task_dict,
            "enqueued_at": int(time.time()),
            "attempts": 0,
        }
        try:
            await self.client.rpush(queue_name, json.dumps(envelope))
            await self.manager.connection._handle_connection_status(True)
            return True
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return False

    async def dequeue_task(self, queue_name: str, timeout: int = 5):
        """RELIABLE pop: atomically moves the head of `queue_name` to this
        worker's processing list. Returns the unwrapped payload + an opaque
        envelope_json the caller MUST pass back to ack/nack.

        Returns (payload_dict, envelope_json) or (None, None) on timeout/error.
        """
        if not self.client:
            return None, None
        try:
            # BLMOVE source destination LEFT RIGHT timeout
            # LEFT = head of source (FIFO consumer)
            # RIGHT = tail of processing list (we don't care about ordering there)
            raw = await self.client.blmove(
                queue_name,
                self.processing_list,
                timeout=timeout,
                src="LEFT",
                dest="RIGHT",
            )
            await self.manager.connection._handle_connection_status(True)
            if not raw:
                return None, None
            envelope = json.loads(raw)
            return envelope["payload"], raw
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return None, None
        except (json.JSONDecodeError, KeyError) as e:
            # Malformed envelope: discard rather than poison-pill the queue.
            logger.error(f"Discarding malformed envelope from {queue_name}: {e}")
            # We've already moved it to the processing list, so remove it.
            if 'raw' in locals() and raw:
                await self.client.lrem(self.processing_list, 1, raw)
            return None, None

    async def push_task(self, task_dict: dict):
        return await self.enqueue_task(GLOBAL_QUEUE, task_dict)

    async def pop_task(self, timeout: int = 5):
        """Returns (payload_dict, envelope_json). Caller MUST call ack_task or
        nack_task with envelope_json when finished."""
        return await self.dequeue_task(GLOBAL_QUEUE, timeout=timeout)

    # =========================================================================
    # ACK / NACK
    # =========================================================================

    async def ack_task(self, envelope_json: str):
        """Removes the task from the processing list. Call ONLY after the work
        is durably committed (e.g. uploaded to GDrive, status updated)."""
        if not self.client or not envelope_json:
            return
        try:
            removed = await self.client.lrem(self.processing_list, 1, envelope_json)
            if removed == 0:
                logger.warning(
                    f"ack_task: envelope not found in {self.processing_list} "
                    f"(possibly already swept by orphan recovery)"
                )
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError) as e:
            await self.manager.connection._handle_connection_status(False)
            logger.error(f"ack_task failed (will be re-delivered): {e}")

    async def nack_task(self, envelope_json: str, requeue: bool = True, reason: str = ""):
        """Negative acknowledgement. requeue=True puts it back on the global
        queue with incremented attempt count. After MAX_RETRIES it goes to the
        dead-letter queue for manual inspection."""
        if not self.client or not envelope_json:
            return
        try:
            envelope = json.loads(envelope_json)
            envelope["attempts"] = envelope.get("attempts", 0) + 1
            envelope["last_error"] = reason[:500]  # bounded
            envelope["last_failed_at"] = int(time.time())

            # Remove from processing list FIRST so we don't double-deliver
            await self.client.lrem(self.processing_list, 1, envelope_json)

            if requeue and envelope["attempts"] < MAX_RETRIES:
                # Exponential backoff in queue position is overkill here; we just
                # push to the tail so it doesn't immediately re-pop.
                await self.client.rpush(GLOBAL_QUEUE, json.dumps(envelope))
                logger.warning(
                    f"🔁 nack: requeued (attempt {envelope['attempts']}/{MAX_RETRIES}) — {reason}"
                )
            else:
                await self.client.rpush(DEAD_LETTER_QUEUE, json.dumps(envelope))
                logger.error(
                    f"💀 nack: sent to dead-letter after {envelope['attempts']} attempts — {reason}"
                )
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError) as e:
            await self.manager.connection._handle_connection_status(False)
            logger.error(f"nack_task failed: {e}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"nack_task: malformed envelope, discarding: {e}")
            await self.client.lrem(self.processing_list, 1, envelope_json)

    # =========================================================================
    # ORPHAN RECOVERY
    # =========================================================================

    async def recover_orphans(self) -> int:
        """Startup sweep. Inspects all worker processing lists; if a worker has
        no live heartbeat, its in-flight tasks are moved back to the global
        queue and the worker is removed from the registry.

        Returns the number of tasks recovered. Call this ONCE at boot, before
        starting your worker loop. Safe to call concurrently with workers
        (we only touch dead workers).
        """
        if not self.client:
            return 0

        try:
            known_workers = await self.client.smembers(WORKER_REGISTRY)
        except (ConnectionError, TimeoutError) as e:
            logger.error(f"recover_orphans: cannot read registry: {e}")
            return 0

        recovered = 0
        for worker_id in known_workers:
            heartbeat_key = f"{WORKER_HEARTBEAT_PREFIX}{worker_id}"
            try:
                hb = await self.client.get(heartbeat_key)
                if hb is not None:
                    # Worker is alive (heartbeat key still has TTL); skip.
                    continue
            except (ConnectionError, TimeoutError):
                continue

            # Worker is dead. Drain its processing list back to the global queue.
            dead_list = f"{PROCESSING_PREFIX}{worker_id}"
            drained_here = 0
            while True:
                try:
                    raw = await self.client.lpop(dead_list)
                except (ConnectionError, TimeoutError):
                    break
                if not raw:
                    break

                # Increment attempts on recovery — a crashed worker counts as a
                # failed attempt to bound infinite recovery loops on poison pills.
                try:
                    envelope = json.loads(raw)
                    envelope["attempts"] = envelope.get("attempts", 0) + 1
                    envelope["recovered_from"] = worker_id
                    envelope["recovered_at"] = int(time.time())

                    if envelope["attempts"] >= MAX_RETRIES:
                        await self.client.rpush(DEAD_LETTER_QUEUE, json.dumps(envelope))
                        logger.error(
                            f"💀 Recovery: task from {worker_id} exceeded retries, "
                            f"sent to dead-letter"
                        )
                    else:
                        # LPUSH back to the head — these were already in flight.
                        await self.client.lpush(GLOBAL_QUEUE, json.dumps(envelope))
                except (json.JSONDecodeError, KeyError):
                    logger.warning(f"Recovery: discarding malformed envelope from {worker_id}")
                    continue

                drained_here += 1

            await self.client.srem(WORKER_REGISTRY, worker_id)
            if drained_here:
                logger.warning(
                    f"♻️  Recovered {drained_here} orphaned tasks from dead worker {worker_id}"
                )
                recovered += drained_here

        if recovered:
            logger.warning(f"♻️  Orphan recovery complete: {recovered} tasks re-queued")
        return recovered

    # =========================================================================
    # ACTIVE TASK DEDUP — now with TTL
    # =========================================================================

    async def set_active_task(self, key: str, task_id: str, ttl: int = ACTIVE_TASK_DEFAULT_TTL):
        """Marks a series:episode as in-flight across all workers, with a TTL
        guard so a crash can't permanently orphan the dedup key."""
        if not self.client:
            return
        try:
            await self.client.hset(ACTIVE_TASKS_HASH, key, task_id)
            # Track expiry separately because HSET doesn't support per-field TTL
            # in standard Redis (Redis 7.4+ has HEXPIRE; we don't assume it).
            expires_at = int(time.time()) + ttl
            await self.client.hset(ACTIVE_TASK_TTL_HASH, key, expires_at)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def get_active_task(self, key: str):
        """Returns the task_id if active and not expired, else None.
        Cleans up the entry lazily if expired."""
        if not self.client:
            return None
        try:
            task_id = await self.client.hget(ACTIVE_TASKS_HASH, key)
            if not task_id:
                await self.manager.connection._handle_connection_status(True)
                return None

            expires_at = await self.client.hget(ACTIVE_TASK_TTL_HASH, key)
            if expires_at and int(expires_at) < int(time.time()):
                # Stale; clean it up so the caller can re-claim.
                await self.client.hdel(ACTIVE_TASKS_HASH, key)
                await self.client.hdel(ACTIVE_TASK_TTL_HASH, key)
                logger.info(f"🧹 Cleared stale active_task: {key}")
                await self.manager.connection._handle_connection_status(True)
                return None
            
            await self.manager.connection._handle_connection_status(True)
            return task_id
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return None

    async def remove_active_task(self, key: str):
        if not self.client:
            return
        try:
            await self.client.hdel(ACTIVE_TASKS_HASH, key)
            await self.client.hdel(ACTIVE_TASK_TTL_HASH, key)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def refresh_active_task(self, key: str, ttl: int = ACTIVE_TASK_DEFAULT_TTL):
        """Heartbeat for long-running tasks. Call periodically from the worker
        to extend the dedup key TTL while the work is still progressing."""
        if not self.client:
            return
        try:
            if not await self.client.hexists(ACTIVE_TASKS_HASH, key):
                await self.manager.connection._handle_connection_status(True)
                return
            expires_at = int(time.time()) + ttl
            await self.client.hset(ACTIVE_TASK_TTL_HASH, key, expires_at)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    # =========================================================================
    # WAITERS — unchanged; included for completeness
    # =========================================================================

    async def register_waiter(self, key: str, waiter_data: dict):
        if not self.client:
            return
        waiter_key = f"verzue:waiters:{key}"
        try:
            await self.client.rpush(waiter_key, json.dumps(waiter_data))
            await self.client.expire(waiter_key, 3600)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def pop_all_waiters(self, key: str):
        if not self.client:
            return []
        waiter_key = f"verzue:waiters:{key}"
        waiters = []
        try:
            while True:
                raw = await self.client.lpop(waiter_key)
                if not raw:
                    break
                waiters.append(json.loads(raw))
            await self.manager.connection._handle_connection_status(True)
            return waiters
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return waiters

    # =========================================================================
    # OPS / DEBUGGING
    # =========================================================================

    async def queue_depths(self) -> dict:
        """Returns current depths of all queues. Useful for monitoring."""
        if not self.client:
            return {}
        try:
            global_depth = await self.client.llen(GLOBAL_QUEUE)
            dead_depth = await self.client.llen(DEAD_LETTER_QUEUE)
            workers = await self.client.smembers(WORKER_REGISTRY)
            processing = {}
            for w in workers:
                processing[w] = await self.client.llen(f"{PROCESSING_PREFIX}{w}")
            await self.manager.connection._handle_connection_status(True)
            return {
                "global": global_depth,
                "dead_letter": dead_depth,
                "processing_by_worker": processing,
            }
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return {}

    async def replay_dead_letter(self, max_count: int = 100) -> int:
        """Move tasks from dead-letter back to the global queue with a reset
        attempt counter. Operator action — call from an admin command after
        you've fixed whatever was breaking them."""
        if not self.client:
            return 0
        replayed = 0
        try:
            for _ in range(max_count):
                raw = await self.client.lpop(DEAD_LETTER_QUEUE)
                if not raw:
                    break
                try:
                    envelope = json.loads(raw)
                    envelope["attempts"] = 0
                    envelope["replayed_at"] = int(time.time())
                    await self.client.rpush(GLOBAL_QUEUE, json.dumps(envelope))
                    replayed += 1
                except (json.JSONDecodeError, KeyError):
                    logger.warning("replay_dead_letter: discarding malformed envelope")
            await self.manager.connection._handle_connection_status(True)
            if replayed:
                logger.warning(f"♻️  Replayed {replayed} tasks from dead-letter")
            return replayed
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return 0
