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
    source: str = "standalone"   # "dashboard" | "subscription" | "standalone"
    episode_number: Optional[str] = None
    service: str = "Unknown"
    
    waiters: list = None # List of (req_id, view_ref) pairs waiting for this task

    is_smartoon: bool = False
    # Piccoma: from episode list API/HTML — False = coin/point only, True = 待てば¥0 style, None = unknown
    piccoma_wait_free: Optional[bool] = None
    pre_created_folder_id: Optional[str] = None
    final_folder_name: Optional[str] = None
    main_folder_id: Optional[str] = None
    client_folder_id: Optional[str] = None
    client_folders: list[dict] = None # [{'id': '...', 'name': '...', 'shortcut_name': '...'}]
    series_title_id: Optional[str] = None

    status: TaskStatus = TaskStatus.QUEUED
    share_link: Optional[str] = None
    purchase_progress: int = 0 # 0-100%
    purchase_status: str = "Waiting" # e.g., "Navigating", "Clicking Buy", "Done"
    error_message: Optional[str] = None # 🔴 Why this task failed

    def __post_init__(self):
        if self.waiters is None: self.waiters = []
        if self.client_folders is None: self.client_folders = []

    @property
    def folder_name(self) -> str:
        seq_idx = f"{int(self.id):02d}"
        
        clean_title = "".join([c for c in self.title if c.isalnum() or c in " -_().話＃#「」『』"]).strip()

        # 🟢 Jumptoon, Mecha, Piccoma Special Handling: Use only the semantic name for Drive (no numeric index)
        # 🟢 S-GRADE: Priority over is_smartoon to satisfy user request for "第1話" instead of "01 - 第1話"
        if self.service in ["Jumptoon", "Mecha", "Piccoma"] or (self.url and "piccoma" in self.url.lower()):
            return clean_title

        if self.is_smartoon:
            return f"{seq_idx} - {clean_title}"
        else:
            safe_num = "".join([c for c in self.chapter_str if c.isalnum() or c in " -_().話"]).strip()
            return f"{seq_idx} - {safe_num}_{clean_title}"

    def to_dict(self) -> dict:
        """Serializes the task for Redis."""
        d = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        if isinstance(d.get('status'), TaskStatus):
            d['status'] = d['status'].value
        return d

    @classmethod
    def from_dict(cls, data: dict):
        """Deserializes the task from Redis."""
        import inspect
        sig = inspect.signature(cls)
        valid_keys = sig.parameters.keys()
        init_data = {k: v for k, v in data.items() if k in valid_keys}
        
        task = cls(**init_data)
        if 'status' in data:
            task.status = TaskStatus(data['status'])
        return task