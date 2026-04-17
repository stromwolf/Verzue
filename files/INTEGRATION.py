"""
INTEGRATION PATCH: tasks/manager.py and tasks/worker.py

This file is documentation, not code-to-import. It shows the precise diffs
needed in your existing files to consume the new reliable queue.

==============================================================================
PATCH 1: app/tasks/manager.py — register/recover on startup, ack on success
==============================================================================
"""

# ---------------------------------------------------------------------------
# In TaskQueue.__init__ — no changes needed
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Add this method to TaskQueue. Call from your bot startup (main.py setup_hook
# or wherever you currently instantiate TaskQueue), BEFORE you spawn workers.
# ---------------------------------------------------------------------------
async def boot(self):
    """One-shot startup sequence. Call exactly once before workers spin up."""
    # 1. Register this process as an alive worker (starts heartbeat loop)
    await self.redis.queue.register_worker()
    # 2. Sweep dead workers' processing lists back to global queue
    recovered = await self.redis.queue.recover_orphans()
    if recovered:
        logger.warning(f"🔄 Boot recovered {recovered} in-flight tasks from prior crash")

# ---------------------------------------------------------------------------
# Add this for graceful shutdown. Wire into your $restart cog and SIGTERM handler.
# ---------------------------------------------------------------------------
async def shutdown(self):
    """Drain in-flight tasks back to global queue and deregister."""
    self.is_draining = True
    # Wait for active workers to finish current tasks (bounded)
    deadline = asyncio.get_event_loop().time() + 30  # 30s grace
    while self.busy_workers > 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
    await self.redis.queue.deregister_worker()


# ---------------------------------------------------------------------------
# REPLACE the worker_loop body. Old version:
#
#     task_dict = await self.redis.pop_task(timeout=5)
#     ...
#     await self.worker.process_task(task)
#     ...
#     finally:
#         await self.redis.remove_active_task(dedup_key)
#
# New version below.
# ---------------------------------------------------------------------------
async def _worker_loop(self, worker_id):
    self.active_worker_count += 1
    try:
        while True:
            if self.workers_to_kill > 0:
                self.workers_to_kill -= 1
                logger.warning(f"👋 Worker {worker_id} cashing out to free up RAM.")
                break

            # 🟢 RELIABLE POP: returns (payload, envelope) — envelope is opaque,
            # carries retry metadata, must be passed to ack/nack.
            task_dict, envelope = await self.redis.queue.pop_task(timeout=5)

            if task_dict is None:
                continue  # timeout, loop again

            # Reconstruct the ChapterTask from the dict payload
            task = ChapterTask.from_dict(task_dict)

            token = req_id_context.set(task.req_id)
            dedup_key = f"{task.series_id_key}:{task.episode_id}"

            try:
                self.busy_workers += 1
                await EventBus.emit("task_started", {"req_id": task.req_id, "title": task.title})

                # Process the task. process_task should raise on failure.
                await self.worker.process_task(task)

                # 🟢 SUCCESS: ack removes the task from this worker's processing list
                await self.redis.queue.ack_task(envelope)
                await EventBus.emit("task_completed", {"req_id": task.req_id, "title": task.title})

            except Exception as e:
                logger.error(f"❌ Worker {worker_id} crashed on {task.title}: {e}")
                # 🟢 FAILURE: nack with requeue. After MAX_RETRIES it auto-routes
                # to dead-letter. The processing list is cleaned up either way.
                await self.redis.queue.nack_task(envelope, requeue=True, reason=str(e))
                await EventBus.emit("task_failed", task, str(e))

            finally:
                self.busy_workers -= 1
                # Always clear dedup so retries (whether immediate or via recovery)
                # aren't blocked. The dedup hash now has a TTL so even if this
                # line is skipped (process death), the key self-expires in 1hr.
                await self.redis.queue.remove_active_task(dedup_key)
                req_id_context.reset(token)
    finally:
        self.active_worker_count -= 1


# ---------------------------------------------------------------------------
# OPTIONAL but recommended: heartbeat for long-running tasks
# Add this inside TaskWorker.process_task, between scrape and stitch stages.
# ---------------------------------------------------------------------------
async def _refresh_dedup_during_long_work(self, task):
    """For tasks that may exceed the dedup TTL (e.g. huge chapter, slow GDrive),
    extend the lease so the orphan sweep doesn't think we're dead."""
    dedup_key = f"{task.series_id_key}:{task.episode_id}"
    await redis_brain.queue.refresh_active_task(dedup_key, ttl=3600)


"""
==============================================================================
PATCH 2: app/services/redis_manager.py — expose new methods
==============================================================================

In RedisManager._initialize, no change (queue is already a sub-module).

In the delegation section, REPLACE pop_task delegation:
"""

async def pop_task(self, timeout: int = 5):
    """Now returns (payload, envelope) tuple. Caller must ack/nack."""
    return await self.queue.pop_task(timeout)


# Add new delegations:
async def ack_task(self, envelope_json: str):
    return await self.queue.ack_task(envelope_json)

async def nack_task(self, envelope_json: str, requeue: bool = True, reason: str = ""):
    return await self.queue.nack_task(envelope_json, requeue=requeue, reason=reason)


"""
==============================================================================
PATCH 3: app/bot/main.py — boot/shutdown hooks
==============================================================================

In your bot's setup_hook (or wherever task_queue is created):
"""

async def setup_hook(self):
    # ... existing setup ...
    self.task_queue = TaskQueue(self.gdrive_client)
    await self.task_queue.boot()  # 🟢 NEW: registers worker, sweeps orphans
    # ... start worker loops ...


# In your shutdown path (signal handler, $restart cog, on_close):
async def graceful_shutdown(self):
    if hasattr(self, 'task_queue'):
        await self.task_queue.shutdown()  # 🟢 NEW: drains & deregisters
    await self.close()


"""
==============================================================================
ADMIN COG ADDITIONS — operator visibility into the new queues
==============================================================================
"""

@commands.command(name="qstats")
@commands.is_owner()
async def queue_stats(self, ctx):
    """Show current queue depths."""
    depths = await self.bot.task_queue.redis.queue.queue_depths()
    embed = discord.Embed(title="📊 Queue Depths", color=0x3498db)
    embed.add_field(name="Global (pending)", value=str(depths.get("global", 0)))
    embed.add_field(name="Dead Letter", value=str(depths.get("dead_letter", 0)))
    proc = depths.get("processing_by_worker", {})
    if proc:
        embed.add_field(
            name="In-flight by worker",
            value="\n".join(f"`{w}`: {n}" for w, n in proc.items()),
            inline=False,
        )
    await ctx.send(embed=embed)


@commands.command(name="dlq_replay")
@commands.is_owner()
async def replay_dead_letter(self, ctx, max_count: int = 100):
    """Replay failed tasks back into the global queue. Use after you've fixed
    the underlying issue (e.g. a provider was down, sessions were expired)."""
    n = await self.bot.task_queue.redis.queue.replay_dead_letter(max_count=max_count)
    await ctx.send(f"♻️ Replayed {n} tasks from dead-letter back to global queue.")


"""
==============================================================================
WHY THIS WORKS — invariants the design preserves
==============================================================================

1. AT-LEAST-ONCE: A task can only be removed from the system via ack_task.
   Every code path that doesn't ack either nacks (explicit failure) or leaves
   it in the processing list (implicit failure → orphan sweep on next boot).

2. NO DOUBLE-DELIVERY DURING NORMAL OPERATION: BLMOVE is atomic. The task is
   in the global queue OR in exactly one processing list, never both, never
   neither.

3. BOUNDED RECOVERY: Each recovery increments attempts. After MAX_RETRIES the
   task goes to dead-letter and stops cycling. No infinite poison-pill loops.

4. CRASH-SAFE DEDUP: The active_tasks hash now has a parallel TTL hash. Even
   if the cleanup line is skipped (SIGKILL), the dedup self-expires in 1hr.
   Long-running tasks call refresh_active_task to extend the lease.

5. GRACEFUL DRAIN: On planned shutdown, in-flight tasks go to the HEAD of the
   global queue (LPUSH) so they resume immediately, not after all queued work.

6. OPERATOR LEVERAGE: $qstats shows you're healthy at a glance. $dlq_replay
   gives you a one-command recovery after fixing an upstream issue.

==============================================================================
WHAT THIS DOES NOT FIX (intentionally)
==============================================================================

- Idempotency of side effects: if a task uploads to GDrive, then crashes
  before ack, the orphan sweep will redeliver and you'll upload twice. Fix
  this in TaskWorker by checking for existing GDrive items before uploading
  (you already do this for some platforms via the deduplication phase in
  BatchController — extend it to per-chapter level if duplicates show up).

- Multi-host coordination: worker_id uses hostname:pid, so this works
  correctly if you ever scale to multiple VPSes pointing at the same Redis.
  But you'll want a shared clock assumption (NTP) for the heartbeat TTL to
  be meaningful. On a single VPS this is a non-issue.
"""
