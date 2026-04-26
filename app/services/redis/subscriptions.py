import json
import logging
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger("RedisManager.Subscriptions")

class RedisSubscriptions:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client

    async def update_subs_index(self, series_id: str, group_name: str, title: str = None, channel_id: int = None, url: str = None):
        """Caches a subscription mapping for fast global lookups."""
        if not self.client: return
        payload = {"group": group_name}
        if title: payload["title"] = title
        if channel_id: payload["channel_id"] = channel_id
        if url: payload["url"] = url
        try:
            await self.client.hset("verzue:index:subs", series_id, json.dumps(payload))
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def update_schedule_index(self, group_name: str, day: str, series_id: str, platform: str = None):
        """Adds a series to group's daily release schedule and group-wide sets."""
        if not self.client: return
        try:
            # 1. Daily schedule
            key = f"verzue:schedule:{group_name}:{day.capitalize()}"
            await self.client.sadd(key, series_id)
            
            # 2. Group-wide all set
            await self.client.sadd(f"verzue:group:{group_name}:all", series_id)
            
            # 3. Platform-specific group set
            if platform:
                await self.client.sadd(f"verzue:group:{group_name}:platform:{platform.lower()}", series_id)
            
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def remove_from_schedule_index(self, group_name: str, day: str, series_id: str, platform: str = None):
        """Removes a series from group's daily schedule and global group sets."""
        if not self.client: return
        try:
            key = f"verzue:schedule:{group_name}:{day.capitalize()}"
            await self.client.srem(key, series_id)
            await self.client.srem(f"verzue:group:{group_name}:all", series_id)
            if platform:
                await self.client.srem(f"verzue:group:{group_name}:platform:{platform.lower()}", series_id)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

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
            
        try:
            await self.client.delete(*keys)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def get_group_subs(self, group_name: str, platform: str = None):
        """Returns all hydrated subscriptions for a group, optionally filtered by platform."""
        if not self.client: return []
        key = f"verzue:group:{group_name}:platform:{platform.lower()}" if platform else f"verzue:group:{group_name}:all"
        try:
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
                        results.append(sub_data)
            await self.manager.connection._handle_connection_status(True)
            return results
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return []

    async def get_schedule_for_group(self, group_name: str, day: str):
        """Returns hydrated subscription data for a specific day."""
        if not self.client: return []
        key = f"verzue:schedule:{group_name}:{day.capitalize()}"
        try:
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
            await self.manager.connection._handle_connection_status(True)
            return results
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return []

    async def get_indexed_sub(self, series_id: str):
        """O(1) lookup for series subscriptions."""
        if not self.client: return None
        try:
            data = await self.client.hget("verzue:index:subs", series_id)
            await self.manager.connection._handle_connection_status(True)
            return json.loads(data) if data else None
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return None

    async def remove_indexed_sub(self, series_id: str):
        """Removes a sub from the fast index."""
        if not self.client: return
        try:
            await self.client.hdel("verzue:index:subs", series_id)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
