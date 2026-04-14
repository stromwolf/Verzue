import time
import logging

logger = logging.getLogger("RedisManager.Telemetry")

class RedisTelemetry:
    def __init__(self, manager):
        self.manager = manager
        self.client = manager.connection.client

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
