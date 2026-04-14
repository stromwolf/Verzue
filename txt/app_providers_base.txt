from abc import ABC, abstractmethod
import logging

logger = logging.getLogger("BaseProvider")

class BaseProvider(ABC):
    """
    S-Grade Abstract Provider Interface.
    All scrapers must migrate to this interface to support Phase 6 Plugin Architecture.
    """
    
    @abstractmethod
    async def get_series_info(self, url: str) -> tuple[str, int, list[dict], str | None, str, str | None, str | None, str | None, str | None]:
        """
        Returns:
            (title, total_chapters, all_chapters, image_url, series_id, release_day, release_time, status_label, genre_label)
        """
        pass

    @abstractmethod
    async def scrape_chapter(self, task, output_dir: str):
        """Downloads and processes a single chapter."""
        pass

    @abstractmethod
    async def is_session_valid(self, session) -> bool:
        """Validates if the current session is authenticated."""
        pass

    @abstractmethod
    async def fast_purchase(self, task) -> bool:
        """Attempts to unlock/purchase a chapter via API if possible."""
        pass

    async def run_ritual(self, session):
        """
        S-Grade Behavioral Modeling (Phase 7).
        Default implementation is a no-op. Providers should override to 
        simulate human behavior (scrolling, home visits, etc.)
        """
        logger.debug(f"[{self.__class__.__name__}] Ritual: Default no-op.")
        pass
