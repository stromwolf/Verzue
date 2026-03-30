import redis.asyncio as redis
import time
import json
import logging
from config.settings import Settings
from app.core.lua_scripts import TOKEN_BUCKET_SCRIPT

logger = logging.getLogger("RedisManager")

class RedisManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisManager, cls).__new__(cls)
            try:
                logger.info(f"🔌 Connecting to Redis: {Settings.REDIS_URL}")
                cls._instance.pool = redis.ConnectionPool.from_url(
                    Settings.REDIS_URL, decode_responses=True, max_connections=50
                )
                cls._instance.client = redis.Redis(connection_pool=cls._instance.pool)
                cls._instance._lua_limiter = None
            except Exception as e:
                logger.critical(f"Redis Setup Failed: {e}")
                cls._instance.client = None
        return cls._instance

    # --- RATE LIMITING (Existing) ---
    async def get_token(self, bucket_name: str, rate: int = 40, capacity: int = 50):
        if not self.client: return True, 0
        try:
            if not self._lua_limiter:
                self._lua_limiter = self.client.register_script(TOKEN_BUCKET_SCRIPT)
            bucket_key = "global_ui_limit" if "discord_ui" in bucket_name else bucket_name
            result = await self._lua_limiter(keys=[f"limiter:{bucket_key}"], args=[capacity, rate, time.time()])
            return result[0] == 1, result[1]
        except Exception as e:
            logger.error(f"Redis Limiter Error: {e}")
            return True, 0

    async def check_connection(self):
        if not self.client: return False
        try: return await self.client.ping()
        except: return False

    # --- DISTRIBUTED QUEUE METHODS (Phase 3) ---
    async def enqueue_task(self, queue_name: str, task_dict: dict):
        """Pushes a serialized task to the back of the Redis List."""
        if not self.client: return False
        await self.client.rpush(queue_name, json.dumps(task_dict))
        return True

    async def dequeue_task(self, queue_name: str, timeout: int = 5):
        """Blocks and pops a task from the front of the Redis List. (timeout=0 means wait forever)"""
        if not self.client: return None
        result = await self.client.blpop(queue_name, timeout=timeout)
        if result:
            return json.loads(result[1]) # result is a tuple: (queue_key, data)
        return None

    # --- S-GRADE DISTRIBUTED QUEUE (Approach A) ---
    async def push_task(self, task_dict: dict):
        """Standardized global queue entry."""
        return await self.enqueue_task("verzue:queue:global", task_dict)

    async def pop_task(self, timeout: int = 5):
        """Standardized global queue exit."""
        return await self.dequeue_task("verzue:queue:global", timeout=timeout)

    # --- ACTIVE TASK TRACKING (Deduplication) ---
    async def set_active_task(self, key: str, task_id: str):
        """Marks a series:episode as 'in progress' across all workers."""
        if not self.client: return
        await self.client.hset("verzue:active_tasks", key, task_id)

    async def get_active_task(self, key: str):
        """Checks if a task is already being handled elsewhere."""
        if not self.client: return None
        return await self.client.hget("verzue:active_tasks", key)

    async def remove_active_task(self, key: str):
        """Clears the active flag after completion/failure."""
        if not self.client: return
        await self.client.hdel("verzue:active_tasks", key)

    # --- SUBSCRIPTION INDEXING ---
    async def update_subs_index(self, series_id: str, group_name: str, title: str = None, channel_id: int = None, url: str = None):
        """Caches a subscription mapping for fast global lookups."""
        if not self.client: return
        payload = {"group": group_name}
        if title: payload["title"] = title
        if channel_id: payload["channel_id"] = channel_id
        if url: payload["url"] = url
        await self.client.hset("verzue:index:subs", series_id, json.dumps(payload))

    async def update_schedule_index(self, group_name: str, day: str, series_id: str, platform: str = None):
        """Adds a series to group's daily release schedule and group-wide sets."""
        if not self.client: return
        # 1. Daily schedule
        key = f"verzue:schedule:{group_name}:{day.capitalize()}"
        await self.client.sadd(key, series_id)
        
        # 2. Group-wide all set
        await self.client.sadd(f"verzue:group:{group_name}:all", series_id)
        
        # 3. Platform-specific group set
        if platform:
            await self.client.sadd(f"verzue:group:{group_name}:platform:{platform.lower()}", series_id)

    async def remove_from_schedule_index(self, group_name: str, day: str, series_id: str, platform: str = None):
        """Removes a series from group's daily schedule and global group sets."""
        if not self.client: return
        key = f"verzue:schedule:{group_name}:{day.capitalize()}"
        await self.client.srem(key, series_id)
        await self.client.srem(f"verzue:group:{group_name}:all", series_id)
        if platform:
            await self.client.srem(f"verzue:group:{group_name}:platform:{platform.lower()}", series_id)

    async def clear_group_schedule(self, group_name: str):
        """Clears all schedule and group-wide sets for a group."""
        if not self.client: return
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        keys = [f"verzue:schedule:{group_name}:{d}" for d in days]
        keys.append(f"verzue:group:{group_name}:all")
        
        # Find all platform keys dynamically
        platforms = ["jumptoon", "piccoma", "mecha", "acqq", "kakao", "kuaikan"]
        for p in platforms:
            keys.append(f"verzue:group:{group_name}:platform:{p}")
            
        await self.client.delete(*keys)

    async def get_group_subs(self, group_name: str, platform: str = None):
        """Returns all hydrated subscriptions for a group, optionally filtered by platform."""
        if not self.client: return []
        key = f"verzue:group:{group_name}:platform:{platform.lower()}" if platform else f"verzue:group:{group_name}:all"
        series_ids = await self.client.smembers(key)
        
        results = []
        if series_ids:
            pipe = self.client.pipeline()
            for s_id in series_ids:
                pipe.hget("verzue:index:subs", s_id)
            raw_data = await pipe.execute()
            for r in raw_data:
                if r:
                    sub_data = json.loads(r)
                    if not sub_data.get("series_id"): # Ensure series_id is present
                        # We might need to inject it if not in JSON
                        pass
                    results.append(sub_data)
        return results

    async def get_schedule_for_group(self, group_name: str, day: str):
        """Returns hydrated subscription data for a specific day."""
        if not self.client: return []
        key = f"verzue:schedule:{group_name}:{day.capitalize()}"
        series_ids = await self.client.smembers(key)
        
        results = []
        if series_ids:
            # Fetch metadata from index hash
            pipe = self.client.pipeline()
            for s_id in series_ids:
                pipe.hget("verzue:index:subs", s_id)
            
            raw_data = await pipe.execute()
            for r in raw_data:
                if r:
                    sub_data = json.loads(r)
                    results.append(sub_data)
        return results

    async def get_indexed_sub(self, series_id: str):
        """O(1) lookup for series subscriptions."""
        if not self.client: return None
        data = await self.client.hget("verzue:index:subs", series_id)
        return json.loads(data) if data else None

    async def remove_indexed_sub(self, series_id: str):
        """Removes a sub from the fast index."""
        if not self.client: return
        await self.client.hdel("verzue:index:subs", series_id)

    # --- PUB/SUB FOR REAL-TIME DISCORD UI UPDATES (Phase 3) ---
    async def publish_event(self, channel: str, event_type: str, payload: dict):
        """Workers use this to tell the Bot that a task updated."""
        if not self.client: return
        message = json.dumps({"event": event_type, "data": payload})
        await self.client.publish(channel, message)

    def get_subscriber(self):
        """Returns a PubSub object for the Bot to listen for worker events."""
        if not self.client: return None
        return self.client.pubsub()

    # --- SESSION VAULT METHODS (Phase 1) ---
    async def set_session(self, platform: str, account_id: str, session_data: dict):
        """Saves session data (cookies, tokens, metadata) to Redis."""
        if not self.client: return
        key = f"verzue:session:{platform}:{account_id}"
        await self.client.set(key, json.dumps(session_data))
        logger.debug(f"💾 Redis: Saved session for {platform}:{account_id}")

    async def get_session(self, platform: str, account_id: str):
        """Retrieves session data from Redis."""
        if not self.client: return None
        key = f"verzue:session:{platform}:{account_id}"
        data = await self.client.get(key)
        return json.loads(data) if data else None

    async def list_sessions(self, platform: str):
        """Lists all account IDs for a given platform."""
        if not self.client: return []
        pattern = f"verzue:session:{platform}:*"
        keys = await self.client.keys(pattern)
        return [k.split(":")[-1] for k in keys]

    async def delete_session(self, platform: str, account_id: str):
        """Removes a session from Redis."""
        if not self.client: return
        key = f"verzue:session:{platform}:{account_id}"
        await self.client.delete(key)

    # --- TELEMETRY & METRICS (Phase 5) ---
    async def record_request(self, platform: str, success: bool, error_type: str = None):
        """Tracks success/failure metrics for S-Grade monitoring."""
        if not self.client: return
        pipe = self.client.pipeline()
        date_str = time.strftime("%Y-%m-%d")
        base_key = f"verzue:metrics:{platform}:{date_str}"
        
        await pipe.hincrby(base_key, "total_requests", 1)
        if success:
            await pipe.hincrby(base_key, "success_count", 1)
        else:
            await pipe.hincrby(base_key, "failure_count", 1)
            if error_type:
                await pipe.hincrby(f"{base_key}:errors", error_type, 1)
        
        await pipe.execute()

    async def get_metrics(self, platform: str, date_str: str = None):
        """Retrieves metrics for a specific platform and date."""
        if not self.client: return {}
        if not date_str: date_str = time.strftime("%Y-%m-%d")
        base_key = f"verzue:metrics:{platform}:{date_str}"
        
        stats = await self.client.hgetall(base_key)
        errors = await self.client.hgetall(f"{base_key}:errors")
        return {"stats": stats, "errors": errors}
