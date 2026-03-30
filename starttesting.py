import asyncio
import sys
import os
import psutil
from pathlib import Path
import logging

# 1. Initialize Loggers and Base Settings
from app.core.logger import setup_logging
logger = setup_logging()

from config.settings import Settings
Settings.ensure_dirs()

# 2. Imports from the main bot app
from app.services.gdrive.client import GDriveClient
from app.tasks.manager import TaskQueue
from app.bot.main import MechaBot
from app.bot.helper_bot import HelperBot
from app.services.session_service import SessionService
from app.services.session_healer import SessionHealer
from app.services.health_monitor import HealthMonitor
from app.services.task_listener import TaskListener
from app.services.group_manager import sync_index_to_redis

# 🟢 S-GRADE: Isolated Staging Token (Sanitized for Iron Mask)
STAGING_TOKEN = os.getenv("STAGING_TOKEN", "YOUR_LOCAL_STAGING_TOKEN_HERE")

async def heartbeat():
    """Keeps the event loop active to prevent container sleep/throttling."""
    while True:
        await asyncio.sleep(60)
        logger.debug("💓 STAGING Heartbeat: Event loop is healthy.")
        
async def main():
    logger.info("🧪 Starting [Code Testing Bot] Staging System...")
    
    # 🟢 S-GRADE PROCESS GUARD: Isolated PID for Staging
    pid_file = Path("testing_bot.pid")
    force_start = "--force" in sys.argv
    
    if pid_file.exists() and not force_start:
        try:
            old_pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if proc.is_running() and "python" in proc.name().lower():
                    logger.critical(f"🛑 STAGING: Instance (PID {old_pid}) already active!")
                    logger.info("Run with 'python starttesting.py --force' to override.")
                    sys.exit(1)
        except (ValueError, psutil.NoSuchProcess):
            pass # Stale PID
            
    pid_file.write_text(str(os.getpid()))
    
    # Initialize GDrive
    logger.info("☁️  Staging: Connecting to Google Drive...")
    try:
        gdrive_client = GDriveClient()
    except Exception as e:
        logger.warning(f"⚠️ Staging GDrive Failed: {e}")
        gdrive_client = None

    # Init Queue
    queue = TaskQueue(gdrive_client=gdrive_client)
    asyncio.create_task(heartbeat())

    # Sync Global Brain (Redis)
    from app.services.redis_manager import RedisManager
    brain = RedisManager()
    if await brain.check_connection():
        logger.info("🧠 Staging Brain: CONNECTED")
        await sync_index_to_redis()
    else:
        logger.error("🧠 Staging Brain: [!] Redis is disconnected.")

    # Autonomous Health Services
    session_service = SessionService()
    healer = SessionHealer(session_service)
    monitor = HealthMonitor(session_service)
    task_listener = TaskListener()
    
    asyncio.create_task(healer.start())
    asyncio.create_task(monitor.start())
    asyncio.create_task(task_listener.start())
    logger.info("🏥 Staging Health Services: ACTIVE")

    # Start the specific Staging Identity
    logger.info("🎭 Identity: [Code Testing Bot]")
    bot = MechaBot(token=STAGING_TOKEN, task_queue=queue, redis_brain=brain)
    
    try:
        await bot.start_bot()
    except asyncio.CancelledError:
        logger.info("👋 Staging shutdown signal received.")
    except Exception as e:
        logger.critical(f"💀 Staging Crash: {e}", exc_info=True)
    finally:
        current_task = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current_task:
                task.cancel()
        
        logger.info("🛑 Staging: Disconnecting...")
        try:
            await asyncio.gather(
                bot.close(),
                return_exceptions=True
            )
        except: pass
            
        Path("testing_bot.pid").unlink(missing_ok=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✅ Staging System fully shut down.")
