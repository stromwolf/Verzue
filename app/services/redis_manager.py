import logging
import json
from .redis import (
    RedisConnection,
    RedisQueue,
    RedisSessionStore,
    RedisSubscriptions,
    RedisPubSub,
    RedisTelemetry
)

logger = logging.getLogger("RedisManager")

class RedisManager:
    """
    S-Grade Orchestrator for Redis Services.
    Maintains backward compatibility while delegating logic to specialized sub-modules.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisManager, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        # 1. Connection (Core)
        self.connection = RedisConnection(self)
        self.client = self.connection.client
        
        # 2. Specialized Services
        self.queue = RedisQueue(self)
        self.sessions = RedisSessionStore(self)
        self.subscriptions = RedisSubscriptions(self)
        self.pubsub = RedisPubSub(self)
        self.telemetry = RedisTelemetry(self)

    @property
    def _is_connected(self):
        return self.connection._is_connected

    # --- Connection & Rate Limiting Delegation ---
    
    async def _handle_connection_status(self, is_success: bool):
        return await self.connection._handle_connection_status(is_success)

    async def get_token(self, bucket_name: str, rate: int = 40, capacity: int = 50):
        return await self.connection.get_token(bucket_name, rate, capacity)

    async def check_connection(self):
        return await self.connection.check_connection()

    # --- Queue Delegation ---

    async def enqueue_task(self, queue_name: str, task_dict: dict):
        return await self.queue.enqueue_task(queue_name, task_dict)

    async def dequeue_task(self, queue_name: str, timeout: int = 5):
        return await self.queue.dequeue_task(queue_name, timeout)

    async def push_task(self, task_dict: dict):
        return await self.queue.push_task(task_dict)

    async def pop_task(self, timeout: int = 5):
        return await self.queue.pop_task(timeout)

    async def set_active_task(self, key: str, task_id: str):
        return await self.queue.set_active_task(key, task_id)

    async def get_active_task(self, key: str):
        return await self.queue.get_active_task(key)

    async def remove_active_task(self, key: str):
        return await self.queue.remove_active_task(key)

    # --- Subscription Delegation ---

    async def update_subs_index(self, *args, **kwargs):
        return await self.subscriptions.update_subs_index(*args, **kwargs)

    async def update_schedule_index(self, *args, **kwargs):
        return await self.subscriptions.update_schedule_index(*args, **kwargs)

    async def remove_from_schedule_index(self, *args, **kwargs):
        return await self.subscriptions.remove_from_schedule_index(*args, **kwargs)

    async def clear_group_schedule(self, *args, **kwargs):
        return await self.subscriptions.clear_group_schedule(*args, **kwargs)

    async def get_group_subs(self, *args, **kwargs):
        return await self.subscriptions.get_group_subs(*args, **kwargs)

    async def get_schedule_for_group(self, *args, **kwargs):
        return await self.subscriptions.get_schedule_for_group(*args, **kwargs)

    async def get_indexed_sub(self, *args, **kwargs):
        return await self.subscriptions.get_indexed_sub(*args, **kwargs)

    async def remove_indexed_sub(self, *args, **kwargs):
        return await self.subscriptions.remove_indexed_sub(*args, **kwargs)

    # --- Pub/Sub Delegation ---

    async def publish_event(self, channel: str, event_type: str, payload: dict):
        return await self.pubsub.publish_event(channel, event_type, payload)

    def get_subscriber(self):
        return self.pubsub.get_subscriber()

    # --- Session Vault Delegation ---

    async def set_session(self, platform: str, account_id: str, session_data: dict):
        return await self.sessions.set_session(platform, account_id, session_data)

    async def get_session(self, platform: str, account_id: str):
        return await self.sessions.get_session(platform, account_id)

    async def get_sessions_batch(self, platform: str, account_ids: list[str]):
        return await self.sessions.get_sessions_batch(platform, account_ids)

    async def list_sessions(self, platform: str):
        return await self.sessions.list_sessions(platform)

    async def delete_session(self, platform: str, account_id: str):
        return await self.sessions.delete_session(platform, account_id)

    # --- Telemetry Delegation ---

    async def record_request(self, platform: str, success: bool, error_type: str = None):
        return await self.telemetry.record_request(platform, success, error_type)

    async def get_metrics(self, platform: str, date_str: str = None):
        return await self.telemetry.get_metrics(platform, date_str)
