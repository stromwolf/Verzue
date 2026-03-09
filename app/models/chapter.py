from dataclasses import dataclass
from typing import Optional
from enum import Enum

class TaskStatus(str, Enum): # <-- Add 'str, ' right here!
    QUEUED = "Queued"
    PROCESSING = "Processing"
    DOWNLOADING = "Downloading"
    UNSCRAMBLING = "Unscrambling"
    STITCHING = "Stitching"
    UPLOADING = "Uploading"
    COMPLETED = "Chapter Completed"
    FAILED = "Failed"

@dataclass
class ChapterTask:
    id: int
    title: str
    chapter_str: str
    url: str
    series_title: str
    req_id: str
    
    series_id_key: str
    episode_id: str
    
    requester_id: int
    channel_id: int
    guild_id: int
    guild_name: str
    scan_group: str
    
    waiters: list = None # List of (req_id, view_ref) pairs waiting for this task

    is_smartoon: bool = False
    pre_created_folder_id: Optional[str] = None
    final_folder_name: Optional[str] = None
    main_folder_id: Optional[str] = None
    client_folder_id: Optional[str] = None
    series_title_id: Optional[str] = None

    status: TaskStatus = TaskStatus.QUEUED
    purchase_progress: int = 0 # 0-100%
    purchase_status: str = "Waiting" # e.g., "Navigating", "Clicking Buy", "Done"

    def __post_init__(self):
        if self.waiters is None: self.waiters = []

    @property
    def folder_name(self) -> str:
        seq_idx = f"{int(self.id):02d}"
        
        clean_title = "".join([c for c in self.title if c.isalnum() or c in " -_().話"]).strip()

        # 🟢 Jumptoon Special Handling: Use "第1話 - Title" format
        if "jumptoon.com" in self.url:
            title_sep = f" - {clean_title}" if clean_title else ""
            return f"{self.chapter_str}{title_sep}"

        if self.is_smartoon:
            return f"{seq_idx} - {clean_title}"
        else:
            safe_num = "".join([c for c in self.chapter_str if c.isalnum() or c in " -_().話"]).strip()
            return f"{seq_idx} - {safe_num}_{clean_title}"