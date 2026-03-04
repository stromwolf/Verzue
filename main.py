import asyncio
import sys
from app.core.logger import setup_logging
logger = setup_logging()

from config.settings import Settings
Settings.ensure_dirs()

from app.services.browser.driver import BrowserService
from app.services.gdrive.client import GDriveClient
from app.tasks.manager import TaskQueue
from app.bot.main import MechaBot

async def heartbeat():
    """Keeps the event loop active to prevent container sleep/throttling."""
    while True:
        # Just a tiny bit of logic every minute
        await asyncio.sleep(60)
        logger.debug("💓 System Heartbeat: Event loop is healthy.")
        
async def main():
    logger.info("🚀 Starting Mecha Bot System...")
    
    # Write PID to file to prevent duplicate instances
    from pathlib import Path
    import os
    Path("bot.pid").write_text(str(os.getpid()))
    browser = BrowserService()
    
    logger.info("☁️  Initializing Google Drive...")
    try:
        gdrive_client = GDriveClient()
    except Exception as e:
        logger.warning(f"⚠️ Google Drive Login Failed: {e}")
        gdrive_client = None

    # 2. Init Queue
    queue = TaskQueue(browser_service=browser, gdrive_client=gdrive_client)

    # The worker is now a separate process and is no longer started here.
    # asyncio.create_task(queue.start_worker())
    asyncio.create_task(heartbeat())

    # ... other inits ...

    # Verify Global Brain (Redis)
    from app.services.redis_manager import RedisManager
    brain = RedisManager()
    if await brain.check_connection():
        logger.info("🧠 Global Brain: CONNECTED (Local V-Node)")
    else:
        logger.error("🧠 Global Brain: DISCONNECTED. Check REDIS_URL.")

    # ... start queue and bot ...
    bot = MechaBot(token=Settings.DISCORD_TOKEN, task_queue=queue)
    
    try:
        await bot.start_bot()
    except asyncio.CancelledError:
        # This catches the Ctrl+C interrupt gracefully
        logger.info("👋 Shutdown signal received.")
    except Exception as e:
        logger.critical(f"💀 Crash: {e}", exc_info=True)
    finally:
        logger.info("🛑 Disconnecting from Discord...")
        await bot.close() # 🟢 CRITICAL: This gracefully closes the websocket and aiohttp sessions!
        
        logger.info("🧹 Cleaning up background tasks...")
        current_task = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current_task:
                task.cancel()
                
        # 🟢 Clean up the PID file so the next startup is clean
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