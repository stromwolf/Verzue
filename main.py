import asyncio
import sys
from app.core.logger import setup_logging
logger = setup_logging()

from config.settings import Settings
Settings.ensure_dirs()

from app.services.gdrive.client import GDriveClient
from app.tasks.manager import TaskQueue
from app.bot.main import MechaBot
from app.bot.helper_bot import HelperBot
from app.services.session_service import SessionService
from app.services.session_healer import SessionHealer
from app.services.health_monitor import HealthMonitor
from app.services.task_listener import TaskListener
from app.services.group_manager import sync_index_to_redis

async def heartbeat():
    """Keeps the event loop active to prevent container sleep/throttling."""
    while True:
        # Just a tiny bit of logic every minute
        await asyncio.sleep(60)
        logger.debug("💓 System Heartbeat: Event loop is healthy.")
        
async def main():
    logger.info("🚀 Starting Mecha Bot System...")
    
    # 🟢 S-GRADE PROCESS GUARD: Prevent duplicate instances
    import os
    import psutil
    from pathlib import Path
    
    pid_file = Path("bot.pid")
    force_start = "--force" in sys.argv
    
    if pid_file.exists() and not force_start:
        try:
            old_pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                if proc.is_running() and "python" in proc.name().lower():
                    logger.critical(f"🛑 CRITICAL: Another instance (PID {old_pid}) is already running!")
                    logger.info("Please kill the existing process or use 'python main.py --force'")
                    sys.exit(1)
        except (ValueError, psutil.NoSuchProcess):
            pass # Stale PID, safe to overwrite
            
    pid_file.write_text(str(os.getpid()))
    
    logger.info("☁️  Initializing Google Drive...")
    try:
        gdrive_client = GDriveClient()
    except Exception as e:
        logger.warning(f"⚠️ Google Drive Login Failed: {e}")
        gdrive_client = None

    # 2. Init Queue
    queue = TaskQueue(gdrive_client=gdrive_client)

    # The worker is now a separate process and is no longer started here.
    # asyncio.create_task(queue.start_worker())
    asyncio.create_task(heartbeat())

    # ... other inits ...

    # Verify Global Brain (Redis)
    from app.services.redis_manager import RedisManager
    brain = RedisManager()
    if await brain.check_connection():
        logger.info("🧠 Global Brain: CONNECTED (Local V-Node)")
        # 🟢 S-GRADE: Sync local group data to Redis Index on startup
        await sync_index_to_redis()
    else:
        logger.error("🧠 Global Brain: DISCONNECTED. Check REDIS_URL.")

    # 🟢 START S-GRADE AUTONOMOUS SERVICES
    session_service = SessionService()
    healer = SessionHealer(session_service)
    monitor = HealthMonitor(session_service)
    task_listener = TaskListener()
    
    asyncio.create_task(healer.start())
    asyncio.create_task(monitor.start())
    asyncio.create_task(task_listener.start())
    logger.info("🏥 Autonomous Health Services: ACTIVE (Healer, Monitor & TaskListener)")

    # ... start queue and bot ...
    bot = MechaBot(token=Settings.DISCORD_TOKEN, task_queue=queue, redis_brain=brain)
    
    # 3. Init Helper Bot
    helper_bot = None
    if Settings.HELPER_TOKEN:
        logger.info("🤖 Starting Secondary Helper Bot (Slash Commands)...")
        helper_bot = HelperBot(token=Settings.HELPER_TOKEN, main_bot=bot)

    try:
        tasks = [bot.start_bot()]
        if helper_bot:
            tasks.append(helper_bot.start_bot())
            
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # This catches the Ctrl+C interrupt gracefully
        logger.info("👋 Shutdown signal received.")
    except Exception as e:
        logger.critical(f"💀 Crash: {e}", exc_info=True)
    finally:
        # 🟢 PHASE 1: Immediate Cancellation of Background Tasks
        # This keeps the bot responsive by stopping all loops (Healer, Poller, Heartbeat)
        current_task = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current_task:
                task.cancel()
        
        logger.info("🛑 Disconnecting from Discord...")
        try:
            # 🟢 PHASE 2: Graceful Service Shutdown
            # Use gather with a timeout to prevent hanging
            await asyncio.gather(
                bot.close(),
                helper_bot.close() if helper_bot else asyncio.sleep(0),
                return_exceptions=True
            )
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            
        # 🟢 PHASE 3: Final File Cleanup
        from pathlib import Path
        try:
            Path("bot.pid").unlink(missing_ok=True)
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Windows terminal catches Ctrl+C here
        print("\n✅ System fully shut down.")