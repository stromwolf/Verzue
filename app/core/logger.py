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
        
        # 🟢 Developer Mode: Elevate console to DEBUG if enabled
        console_level = logging.DEBUG if getattr(Settings, "DEVELOPER_MODE", False) else logging.INFO
        sh.setLevel(console_level)
        
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


logger = setup_logging()

