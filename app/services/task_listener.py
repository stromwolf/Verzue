import logging
import asyncio
import json
from app.services.redis_manager import RedisManager
from app.models.chapter import TaskStatus

logger = logging.getLogger("TaskListener")

class TaskListener:
    _instance = None
    _running = False
    redis = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskListener, cls).__new__(cls)
            from app.services.redis_manager import RedisManager
            cls._instance.redis = RedisManager()
            cls._instance._running = False
        return cls._instance

    async def start(self):
        """Starts the background event listener for task updates."""
        if self._running: return
        self._running = True
        logger.info("🎧 TaskListener background listener started.")
        
        subscriber = self.redis.get_subscriber()
        if not subscriber:
            logger.error("❌ Redis client not available for TaskListener.")
            return

        await subscriber.subscribe("verzue:events:tasks")
        
        try:
            while self._running:
                message = await subscriber.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    try:
                        payload = json.loads(message["data"])
                        event = payload.get("event")
                        data = payload.get("data")
                        
                        if event == "task_updated":
                            await self._handle_task_update(data)
                    except Exception as e:
                        logger.error(f"Error parsing task event: {e}")
                
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"🎧 TaskListener Listener Error: {e}")
        finally:
            self._running = False

    async def stop(self):
        self._running = False

    async def _handle_task_update(self, task_dict: dict):
        """Updates the local UniversalDashboard state from a remote task update."""
        req_id = task_dict.get("req_id")
        if not req_id: return

        from app.bot.common.view import UniversalDashboard
        view = UniversalDashboard.active_views.get(req_id)
        if not view:
            # We don't have this view active in this process
            return

        task_id = task_dict.get("id")
        episode_id = task_dict.get("episode_id")

        updated = False
        for t in view.active_tasks:
            # Match task by ID and Episode ID
            if str(t.id) == str(task_id) and str(t.episode_id) == str(episode_id):
                # Update fields
                t.status = TaskStatus(task_dict.get("status", t.status.value))
                t.share_link = task_dict.get("share_link", t.share_link)
                t.pre_created_folder_id = task_dict.get("pre_created_folder_id", t.pre_created_folder_id)
                t.final_folder_name = task_dict.get("final_folder_name", t.final_folder_name)
                updated = True
                break
        
        if updated:
            # Trigger a UI refresh if something changed
            view.trigger_refresh()
