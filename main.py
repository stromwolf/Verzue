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

    # 1. Init Services (But don't start them yet)
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
    except KeyboardInterrupt:
        logger.info("👋 Keyboard Interrupt.")
    except Exception as e:
        logger.critical(f"💀 Crash: {e}", exc_info=True)
    finally:
        logger.info("🛑 Shutting down...")
        # The new BrowserService is ephemeral and doesn't need to be stopped.
        # This call is left in case of future changes but it does nothing now.
        # browser.stop() 
        for task in asyncio.all_tasks():
            task.cancel()
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass