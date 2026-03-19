import os
import json
import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config.settings import Settings
from app.services.redis_manager import RedisManager

async def migrate():
    redis = RedisManager()
    if not await redis.check_connection():
        print("❌ Could not connect to Redis. Ensure Redis is running.")
        return

    secrets_dir = Settings.SECRETS_DIR
    platforms = ["jumptoon", "mecha", "piccoma", "kakao", "acqq", "kuaikan"]

    print("🚀 Starting Cookie-to-Redis Migration...")

    total_migrated = 0
    for platform in platforms:
        platform_dir = secrets_dir / platform
        if not platform_dir.exists():
            continue

        print(f"\n📂 Processing {platform}...")
        for cookie_file in platform_dir.glob("*.json"):
            account_id = cookie_file.stem
            try:
                with open(cookie_file, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                
                # Wrap in a standard session object
                session_data = {
                    "account_id": account_id,
                    "platform": platform,
                    "cookies": cookies,
                    "last_updated": os.path.getmtime(cookie_file),
                    "status": "HEALTHY"
                }

                await redis.set_session(platform, account_id, session_data)
                print(f"  ✅ Migrated: {account_id}")
                total_migrated += 1
            except Exception as e:
                print(f"  ❌ Failed to migrate {cookie_file.name}: {e}")

    print(f"\n✨ Finished! Migrated {total_migrated} sessions to Redis.")

if __name__ == "__main__":
    asyncio.run(migrate())
