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

    async def dequeue_task(self, queue_name: str, timeout: int = 0):
        """Blocks and pops a task from the front of the Redis List. (timeout=0 means wait forever)"""
        if not self.client: return None
        result = await self.client.blpop(queue_name, timeout=timeout)
        if result:
            return json.loads(result[1]) # result is a tuple: (queue_name, data)
        return None

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
