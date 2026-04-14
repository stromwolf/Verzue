import logging
from abc import ABC, abstractmethod

class BaseUnlocker(ABC):
    def __init__(self, provider):
        self.provider = provider
        self.logger = logging.getLogger(f"Unlocker.{self.__class__.__name__}")

    @abstractmethod
    async def unlock(self, context_id: int, task, view, update_progress_callback):
        """
        Abstract method to unlock a chapter.
        Each platform MUST implement this.
        """
        pass
