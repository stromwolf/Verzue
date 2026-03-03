import asyncio
import logging
from .redis_manager import RedisManager

logger = logging.getLogger("UIManager")

class UIManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(UIManager, cls).__new__(cls)
            # THREAD-SAFE QUEUE
            cls._instance.queue = asyncio.Queue()
            cls._instance.is_running = False
            cls._instance.loop = None
            cls._instance.locks = {} # Per-request locks to serialize edits
        return cls._instance

    def request_update(self, req_id: str, view: any):
        """
        Thread-safe update request.
        Can be called from blocking threads (BatchController) safely.
        """
        if self.loop and self.loop.is_running():
            logger.debug(f"Queuing UI update for R-ID: {req_id}")
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (req_id, view))
        else:
            logger.warning(f"UIManager loop not ready for R-ID: {req_id}")

    async def start(self):
        if self.is_running: return
        self.is_running = True
        self.loop = asyncio.get_running_loop()
        logger.info("📡 UIManager starting broadcast loop...")
        asyncio.create_task(self._broadcast_loop())

    async def _broadcast_loop(self):
        redis_brain = RedisManager()
        logger.info("📡 Global UI Dispatcher Online (Thread-Safe).")
        
        while True:
            try:
                # 1. Non-blocking Get
                req_id, view = await self.queue.get()
                
                # 2. Ask Redis for a token (Rate limit: 30 edits per sec)
                allowed, wait_time = await redis_brain.get_token("global_ui_limit_discord_ui", rate=30)
                
                if not allowed:
                    logger.debug(f"UI Update for R-ID: {req_id} rate limited. Waiting {wait_time:.2f}s.")
                    await asyncio.sleep(wait_time or 0.1)
                    await self.queue.put((req_id, view))
                    continue

                # 3. Build the current state via V2 JSON Payload hash
                try:
                    payload_components = view.build_v2_payload()
                    task_states = "".join([t.status.value for t in view.active_tasks])
                    
                    state_blob = (
                        f"{str(payload_components)}"
                        f"{view.phases}"
                        f"{task_states}"
                        f"{len(view.active_tasks)}"
                        f"{view.final_link}"
                        f"{getattr(view, 'sub_status', '')}"
                    )
                    new_hash = hash(state_blob)

                    if getattr(view, "_last_hash", 0) == new_hash:
                        self.queue.task_done()
                        continue
                    
                    view._last_hash = new_hash
                    
                    if req_id not in self.locks:
                        self.locks[req_id] = asyncio.Lock()
                    
                    asyncio.create_task(self._serialized_edit(req_id, view))
                    
                except Exception as e:
                    logger.error(f"UI Build Error for R-ID: {req_id}: {e}", exc_info=True)
                
                self.queue.task_done()

            except Exception as e:
                logger.error(f"UI Dispatcher Critical Error: {e}")
                await asyncio.sleep(1)

    async def _serialized_edit(self, req_id, view):
        """Ensures only one edit is in flight per request."""
        async with self.locks[req_id]:
            await self._safe_edit(view, req_id)

    async def _safe_edit(self, view, req_id: str):
        try:
            logger.debug(f"[_safe_edit] Attempting V2 background edit for R-ID: {req_id}")
            # The view handles the raw HTTP patch natively now!
            await view.update_view()
        except Exception as e:
            logger.error(f"[_safe_edit] FAILED for R-ID: {req_id}: {e}", exc_info=True)
