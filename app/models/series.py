from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class SeriesChapterItem:
    """
    Lightweight representation of a chapter found on the landing page.
    Used only for the UI selection menu.
    """
    index: int
    display_id: str
    title: str
    url: str
    is_paid: bool = False
    is_free: bool = False

@dataclass
class SeriesInfo:
    """
    Metadata about a Series.
    """
    title: str
    url: str
    total_chapters: int
    provider: str
    image_url: Optional[str] = None
    
    chapters: List[SeriesChapterItem] = field(default_factory=list)

    @property
    def clean_title(self) -> str:
        """Returns filesystem-safe title."""
        return "".join([c for c in self.title if c.isalnum() or c in " -_()"]).strip()