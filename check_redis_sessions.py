import asyncio
from app.services.redis_manager import RedisManager

async def check():
    redis = RedisManager()
    sessions = await redis.list_sessions("piccoma")
    print(f"Piccoma Sessions: {sessions}")
    for sid in sessions:
        sess = await redis.get_session("piccoma", sid)
        print(f"Session {sid} Status: {sess.get('status')}")

if __name__ == "__main__":
    try:
        asyncio.run(check())
    except Exception as e:
        print(f"Error checking redis: {e}")
