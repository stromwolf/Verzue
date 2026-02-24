from abc import ABC, abstractmethod
from typing import Tuple, List, Dict, Optional
from app.models.chapter import ChapterTask

class BaseScraper(ABC):
    """
    The Base Interface for all comic scrapers.
    Any new site (e.g. Piccoma, Naver) must implement these methods.
    """

    @abstractmethod
    def get_series_info(self, url: str) -> Tuple[str, int, List[Dict], Optional[str], str]:
        """
        Phase 1: Intelligence.
        Fetches metadata and the full chapter list from a series URL.
        
        Returns:
            Tuple containing:
            - title (str): The cleaned series title.
            - total_chapters (int): Total count of episodes.
            - chapters (List[Dict]): List of dicts with {'id', 'title', 'url', 'number_text', 'is_locked'}.
            - image_url (str/None): The high-res poster URL.
            - series_id (str): The unique site-specific series ID (e.g. JT00130).
        """
        pass

    @abstractmethod
    def scrape_chapter(self, task: ChapterTask, output_dir: str) -> str:
        """
        Phase 5: Execution.
        Downloads and handles the specific decryption/extraction for a chapter.
        
        Args:
            task (ChapterTask): The task object containing IDs and URLs.
            output_dir (str): The local path where raw images should be saved.
            
        Returns:
            str: The path to the directory where the images were saved.
        """
        pass