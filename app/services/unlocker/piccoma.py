from .base import BaseUnlocker

class PiccomaUnlocker(BaseUnlocker):
    async def unlock(self, context_id: int, task, view, update_progress):
        update_progress(15, "API Coin Purchase Attempt")
        try:
            success = await self.provider.fast_purchase(task)
            
            if success:
                update_progress(90, "Coin Purchase Successful")
                return True
            
            raise Exception("Piccoma coin purchase failed via API")
                
        except Exception as e:
            self.logger.error(f"Worker {context_id} Piccoma purchase failed: {e}")
            raise e
