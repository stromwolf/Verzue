import asyncio
import os
import sys
import logging
from pathlib import Path

import psutil

from config.settings import Settings, AppState
Settings.ensure_dirs() # Load Secrets & Create Dirs BEFORE any other app imports

from app.core.logger import setup_logging
from app.services.gdrive.client import GDriveClient, NullGDriveClient
from app.services.group_manager import sync_index_to_redis
from app.services.health_monitor import HealthMonitor
from app.services.redis_manager import RedisManager
from app.services.session_healer import SessionHealer
from app.services.session_service import SessionService
from app.services.task_listener import TaskListener
from app.tasks.manager import TaskQueue
from app.bot.main import MechaBot
from app.bot.helper_bot import HelperBot

_PID_FILE = Path("bot.pid")


async def heartbeat() -> None:
    """Keeps the event loop alive; prevents container sleep/throttling."""
    while True:
        await asyncio.sleep(60)
        logging.getLogger(__name__).debug("system.heartbeat: event loop healthy")


def _acquire_pid_lock(force: bool = False) -> None:
    """Atomic PID lock — raises SystemExit if another instance is running."""
    if force:
        _PID_FILE.unlink(missing_ok=True)
    try:
        fd = os.open(_PID_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            proc = psutil.Process(old_pid)
            if proc.is_running() and "python" in proc.name().lower():
                sys.exit(f"Another instance (PID {old_pid}) is running. Use --force to override.")
        except (ValueError, psutil.NoSuchProcess):
            _PID_FILE.unlink(missing_ok=True)
            _acquire_pid_lock()  # retry after stale file removed


async def main() -> None:
    logger = setup_logging()
    
    logger.info("system.start", extra={"component": "entrypoint"})
    _acquire_pid_lock(force="--force" in sys.argv)

    # Google Drive (non-fatal — continues in degraded mode)
    try:
        gdrive_client: GDriveClient | NullGDriveClient = GDriveClient()
        logger.info("gdrive.connected")
    except Exception:
        logger.warning("gdrive.failed — running without Drive", exc_info=True)
        gdrive_client = NullGDriveClient()

    queue = TaskQueue(gdrive_client=gdrive_client)

    brain = RedisManager()
    if not await brain.check_connection():
        logger.critical("redis.disconnected — check REDIS_URL")
        sys.exit(1)
    logger.info("redis.connected")
    await sync_index_to_redis()
    
    # Initialize dynamic state registry
    state = AppState()
    state.load_state()
    state.migrate_legacy_data()

    # Select Bot Identity
    bot_token = Settings.DISCORD_TOKEN
    identity_name = "Main"
    
    if Settings.BOT_MODE == "test" and Settings.TESTING_BOT_TOKEN:
        bot_token = Settings.TESTING_BOT_TOKEN
        identity_name = "Testing"
    elif Settings.BOT_MODE == "admin" and Settings.ADMIN_BOT_TOKEN:
        bot_token = Settings.ADMIN_BOT_TOKEN
        identity_name = "Admin"
    
    logger.info(f"🚀 Bot Identity: {identity_name} Bot (Mode: {Settings.BOT_MODE})")

    session_service = SessionService()
    bot = MechaBot(
        token=bot_token,
        task_queue=queue,
        redis_brain=brain,
    )
    bot.app_state = state
    helper_bot: HelperBot | None = None
    if Settings.HELPER_TOKEN and Settings.HELPER_TOKEN != bot_token:
        helper_bot = HelperBot(token=Settings.HELPER_TOKEN, main_bot=bot)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(heartbeat())
            tg.create_task(SessionHealer(session_service).start())
            tg.create_task(HealthMonitor(session_service).start())
            tg.create_task(TaskListener().start())
            tg.create_task(bot.start_bot())
            if helper_bot:
                tg.create_task(helper_bot.start_bot())
    except* asyncio.CancelledError:
        logger.info("system.shutdown: cancel signal received")
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.critical("system.crash", exc_info=exc)
    finally:
        # Phase 1: graceful close (bot cleans up its own internals)
        await asyncio.wait_for(
            asyncio.gather(
                bot.close(),
                helper_bot.close() if helper_bot else asyncio.sleep(0),
                return_exceptions=True,
            ),
            timeout=10.0,
        )
        # Phase 2: cancel remaining background tasks
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current:
                task.cancel()
        # Phase 3: cleanup
        _PID_FILE.unlink(missing_ok=True)
        logger.info("system.stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSystem fully shut down.")