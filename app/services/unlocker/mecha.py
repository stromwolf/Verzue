from .base import BaseUnlocker

class MechaUnlocker(BaseUnlocker):
    async def unlock(self, context_id: int, task, view, update_progress):
        update_progress(15, "API Fast-Path Attempt")
        try:
            # provider is passed in __init__
            success = await self.provider.fast_purchase(task)
            
            if success:
                update_progress(90, "API Purchase Successful")
                return True
            
            raise Exception("Mecha API fast-purchase failed. Session might be expired or chapter requires purchase.")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Mecha task failed: {e}")
            raise e
