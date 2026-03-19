import asyncio
import json
from app.services.redis_manager import RedisManager

async def check_mecha_sessions():
    redis = RedisManager()
    await redis.check_connection()
    
    platform = "mecha"
    keys = await redis.client.keys(f"verzue:session:{platform}:*")
    print(f"Found {len(keys)} session keys for {platform}")
    
    for key in keys:
        data = await redis.client.get(key)
        session = json.loads(data)
        print(f"Session: {session.get('account_id')} | Status: {session.get('status')} | Cookies: {len(session.get('cookies', []))}")

if __name__ == "__main__":
    asyncio.run(check_mecha_sessions())
