import logging, sys
import contextvars
from config.settings import Settings

# Global context for Request ID
req_id_context = contextvars.ContextVar("req_id", default=None)

class ContextFilter(logging.Filter):
    """Injects the current request ID into the log record."""
    def filter(self, record):
        record.req_id = req_id_context.get()
        return True

class RequestFileHandler(logging.Handler):
    """Dynamically routes logs to files named after the R-ID."""
    def emit(self, record):
        req_id = getattr(record, "req_id", None)
        if not req_id:
            return
        
        log_path = Settings.REQUEST_LOG_DIR / f"{req_id}.log"
        log_entry = self.format(record)
        
        # Thread-safe appending to specific request file
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry + "\n")

class CustomFormatter(logging.Formatter):
    grey, blue, yellow, red, bold_red, reset = "\x1b[38;20m", "\x1b[34;20m", "\x1b[33;20m", "\x1b[31;20m", "\x1b[31;1m", "\x1b[0m"
    
    def get_fmt(self, req_id):
        # Professional Prefix: [INFO] [R-ID] - Message
        prefix = f" [{req_id}]" if req_id and req_id != "None" else " " * 11
        return f"[%(levelname)-5s]{prefix} - %(message)s"

    def format(self, record):
        req_id = getattr(record, "req_id", None)
        fmt = self.get_fmt(req_id)
        
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
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | [%(req_id)s] | %(name)s | %(message)s"))
        fh.addFilter(ctx_filter)
        logger.addHandler(fh)

        # 3. Request Specific Files (Isolation)
        rfh = RequestFileHandler()
        rfh.setLevel(logging.DEBUG)
        rfh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"))
        rfh.addFilter(ctx_filter)
        logger.addHandler(rfh)
    
    # Suppress noisy library logs
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("httplib2").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.INFO)
    
    return logging.getLogger(name)

logger = setup_logging()
