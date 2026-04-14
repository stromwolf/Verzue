import asyncio
from app.core.logger import setup_logging
from config.settings import Settings
from app.services.gdrive.client import GDriveClient
from app.tasks.manager import TaskQueue

async def main():
    """
    The main entry point for the distributed background worker process.
    This process connects to Redis, listens for jobs, and executes them.
    """
    logger = setup_logging("VerzueWorker")
    logger.info("🚀 Starting Distributed Worker System...")
    
    Settings.ensure_dirs()

    # The worker needs its own instances of the services to perform tasks
    logger.info("☁️  Initializing Google Drive for Worker...")
    try:
        gdrive_client = GDriveClient()
    except Exception as e:
        logger.warning(f"⚠️ Worker GDrive Login Failed: {e}")
        gdrive_client = None

    # The TaskQueue, when run here, acts as the consumer
    queue = TaskQueue(gdrive_client=gdrive_client)

    logger.info("🎧 Worker is now listening to Redis for tasks...")
    # This will block forever, processing jobs as they arrive
    await queue.start_worker()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Worker process shutting down.")
