import logging, sys
import contextvars
from config.settings import Settings

# Global context for Request ID and Categorization
req_id_context = contextvars.ContextVar("req_id", default=None)
group_name_context = contextvars.ContextVar("group_name", default=None)
log_category_context = contextvars.ContextVar("log_category", default="Requests")
chapter_id_context = contextvars.ContextVar("chapter_id", default=None)

class ContextFilter(logging.Filter):
    """Injects the current request ID into the log record."""
    def filter(self, record):
        record.req_id = req_id_context.get()
        record.group_name = group_name_context.get()
        record.log_category = log_category_context.get()
        record.chapter_id = chapter_id_context.get()
        return True

class StructuredFileHandler(logging.Handler):
    """Dynamically routes logs to structured hierarchies: logs/<Category>/<Group>/<ID>.log"""
    def emit(self, record):
        req_id = getattr(record, "req_id", None)
        if not req_id:
            return
        
        # S-GRADE: Determine Category and Group
        category = getattr(record, "log_category", "Requests") or "Requests"
        group = getattr(record, "group_name", "Global") or "Global"
        
        # Sanitize group name for filesystem
        safe_group = "".join([c for c in group if c.isalnum() or c in " -_"]).strip() or "Global"
        
        log_dir = Settings.LOG_DIR / category / safe_group
        log_dir.mkdir(parents=True, exist_ok=True)
        
        log_path = log_dir / f"{req_id}.log"
        log_entry = self.format(record)
        
        # Thread-safe appending to structured log file
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")

class CustomFormatter(logging.Formatter):
    grey, blue, yellow, red, bold_red, reset = "\x1b[38;20m", "\x1b[34;20m", "\x1b[33;20m", "\x1b[31;20m", "\x1b[31;1m", "\x1b[0m"
    
    def get_fmt(self, record):
        req_id = getattr(record, "req_id", None)
        chapter_id = getattr(record, "chapter_id", None)
        
        # Balanced Prefix: [INFO ] [R-ID] [Ch] - Message
        prefix = f" [{req_id}]" if req_id and req_id != "None" else ""
        ch_prefix = f" [{chapter_id}]" if chapter_id else ""
        return f"[%(levelname)-6s]{prefix}{ch_prefix} - %(message)s"

    def format(self, record):
        fmt = self.get_fmt(record)
        
        # Apply color based on level
        color = self.FORMATS.get(record.levelno, self.reset)
        log_fmt = color + fmt + self.reset
        
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

    FORMATS = {
        logging.DEBUG: grey, 
        logging.INFO: blue, 
        logging.WARNING: yellow, 
        logging.ERROR: red, 
        logging.CRITICAL: bold_red
    }

def setup_logging(name: str = "MechaBot"):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    ctx_filter = ContextFilter()

    if not logger.hasHandlers():
        # 1. Console (Professional & Clean)
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(CustomFormatter())
        sh.addFilter(ctx_filter)
        logger.addHandler(sh)
        
        # 2. Main File (Grep-friendly)
        fh = logging.FileHandler(Settings.LOG_DIR / "bot.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | [%(req_id)s] [%(chapter_id)s] | %(name)s | %(message)s"))
        fh.addFilter(ctx_filter)
        logger.addHandler(fh)

        # 3. Structured Task/Notification Files (Isolation)
        sfh = StructuredFileHandler()
        sfh.setLevel(logging.INFO)
        sfh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))

        sfh.addFilter(ctx_filter)
        logger.addHandler(sfh)
    
    # 🧊 S-GRADE: Suppress noisy library logs
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("httplib2").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.INFO)
    

    return logging.getLogger(name)

class ProgressBar:
    """S-Grade Progress Bar for log-compatible console output."""
    def __init__(self, req_id: str, label: str, service: str, total: int, bar_length: int = 20):
        self.req_id = req_id
        self.label = label
        self.service = service
        self.total = total
        self.bar_length = bar_length
        self.completed = 0
        self._last_percent = -1

    def update(self, current: int = None):
        if current is not None:
            self.completed = current
        
        percent = int((self.completed / self.total) * 100) if self.total > 0 else 100
        filled_length = int(self.bar_length * self.completed // self.total) if self.total > 0 else self.bar_length
        bar = '▰' * filled_length + '▱' * (self.bar_length - filled_length)
        msg = f"{self.label}: [{self.service}] {bar} {self.completed}/{self.total} ({percent}%)"

        # 1. Console Real-time Update (Visual only, no new line)
        if sys.stdout.isatty():
            # Apply same formatting for consistency
            from app.core.logger import CustomFormatter
            fmt_msg = f"[INFO ] [{self.req_id}] - {msg}"
            sys.stdout.write(f"\r{fmt_msg}")
            sys.stdout.flush()

        # 2. Logger Throttle: Log to permanent files at certain milestones (25, 50, 75, 100%)
        # This keeps logs visible during background uploads without flooding them.
        milestones = [25, 50, 75, 100]
        current_milestone = next((m for m in milestones if percent >= m and not hasattr(self, f"_logged_{m}")), None)
        
        if current_milestone:
            setattr(self, f"_logged_{current_milestone}", True)
            token = req_id_context.set(self.req_id)
            try:
                if percent == 100 and sys.stdout.isatty(): sys.stdout.write("\n")
                logging.getLogger("ProgressBar").info(msg)
            finally:
                req_id_context.reset(token)

        # 3. Dashboard Sync
        try:
            from app.bot.common.view import UniversalDashboard
            view = UniversalDashboard.active_views.get(self.req_id)
            if view:
                for t in view.active_tasks:
                    if t.status not in ["Chapter Completed", "Failed"]:
                        from app.models.chapter import TaskStatus
                        new_status = TaskStatus.DOWNLOADING if self.label == "Downloading" else TaskStatus.UPLOADING
                        if t.status != new_status:
                            t.status = new_status
                        break
        except: pass

    def finish(self):
        # Ensure 100% is logged at least once if it wasn't already
        if not getattr(self, "_logged_final", False):
            self.update(self.total)


logger = setup_logging()
