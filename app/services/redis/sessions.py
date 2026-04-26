import json
import logging
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger("RedisManager.Sessions")

class RedisSessionStore:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client

    async def set_session(self, platform: str, account_id: str, session_data: dict):
        """Saves session data (cookies, tokens, metadata) to Redis."""
        if not self.client: return
        key = f"verzue:session:{platform}:{account_id}"
        try:
            await self.client.set(key, json.dumps(session_data))
            await self.manager.connection._handle_connection_status(True)
            logger.debug(f"💾 Redis: Saved session for {platform}:{account_id}")
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)

    async def get_session(self, platform: str, account_id: str):
        """Retrieves session data from Redis."""
        if not self.client: return None
        key = f"verzue:session:{platform}:{account_id}"
        try:
            data = await self.client.get(key)
            await self.manager.connection._handle_connection_status(True)
            return json.loads(data) if data else None
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return None

    async def get_sessions_batch(self, platform: str, account_ids: list[str]):
        """S-Grade: Retrieves multiple sessions in a single MGET call."""
        if not self.client or not account_ids: return []
        keys = [f"verzue:session:{platform}:{aid}" for aid in account_ids]
        try:
            raw_data = await self.client.mget(keys)
            await self.manager.connection._handle_connection_status(True)
            return [json.loads(d) for d in raw_data if d]
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return []

    async def list_sessions(self, platform: str):
        """Lists all account IDs for a given platform."""
        if not self.client: return []
        pattern = f"verzue:session:{platform}:*"
        try:
            keys = await self.client.keys(pattern)
            await self.manager.connection._handle_connection_status(True)
            return [k.split(":")[-1] for k in keys]
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
            return []

    async def delete_session(self, platform: str, account_id: str):
        """Removes a session from Redis."""
        if not self.client: return
        key = f"verzue:session:{platform}:{account_id}"
        try:
            await self.client.delete(key)
            await self.manager.connection._handle_connection_status(True)
        except (ConnectionError, TimeoutError):
            await self.manager.connection._handle_connection_status(False)
