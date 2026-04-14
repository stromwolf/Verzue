import json
import logging

logger = logging.getLogger("RedisManager.PubSub")

class RedisPubSub:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client

    async def publish_event(self, channel: str, event_type: str, payload: dict):
        """Workers use this to tell the Bot that a task updated."""
        if not self.client: return
        message = json.dumps({"event": event_type, "data": payload})
        await self.client.publish(channel, message)

    def get_subscriber(self):
        """Returns a PubSub object for the Bot to listen for worker events."""
        if not self.client: return None
        return self.client.pubsub()
