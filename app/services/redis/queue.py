import json
import logging
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger("RedisManager.Queue")

class RedisQueue:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client

    async def enqueue_task(self, queue_name: str, task_dict: dict):
        """Pushes a serialized task to the back of the Redis List."""
        if not self.client: return False
        try:
            await self.client.rpush(queue_name, json.dumps(task_dict))
            await self.manager.connection._handle_connection_status(True)
            return True
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return False

    async def dequeue_task(self, queue_name: str, timeout: int = 5):
        """Blocks and pops a task from the front of the Redis List."""
        if not self.client: return None
        try:
            result = await self.client.blpop(queue_name, timeout=timeout)
            await self.manager.connection._handle_connection_status(True)
            if result:
                return json.loads(result[1]) # result is a tuple: (queue_key, data)
            return None
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return None

    async def push_task(self, task_dict: dict):
        """Standardized global queue entry."""
        return await self.enqueue_task("verzue:queue:global", task_dict)

    async def pop_task(self, timeout: int = 5):
        """Standardized global queue exit."""
        return await self.dequeue_task("verzue:queue:global", timeout=timeout)

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

    async def register_waiter(self, key: str, waiter_data: dict):
        """Registers a secondary requester for an in-flight task."""
        if not self.client: return
        waiter_key = f"verzue:waiters:{key}"
        await self.client.rpush(waiter_key, json.dumps(waiter_data))
        await self.client.expire(waiter_key, 3600) # 1hr TTL safety

    async def pop_all_waiters(self, key: str):
        """Drains and returns all registered waiters for a task."""
        if not self.client: return []
        waiter_key = f"verzue:waiters:{key}"
        waiters = []
        while True:
            raw = await self.client.lpop(waiter_key)
            if not raw: break
            waiters.append(json.loads(raw))
        return waiters
