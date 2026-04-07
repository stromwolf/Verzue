from .base import BaseUnlocker

class JumptoonUnlocker(BaseUnlocker):
    async def unlock(self, context_id: int, task, view, update_progress):
        update_progress(15, "API Ticket Unlock Attempt")
        try:
            success = await self.provider.fast_purchase(task)
            
            if success:
                update_progress(90, "Ticket Unlock Successful")
                return True
            
            raise Exception("Jumptoon ticket unlock failed via API")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Jumptoon unlock failed: {e}")
            raise e
