import os
import logging
from pathlib import Path
from .secrets import Secrets

class Settings:
    """Central configuration."""

    # --- 1. PATHS ---
    BASE_DIR = Path(__file__).resolve().parent.parent

    DATA_DIR = BASE_DIR / "data"
    DOWNLOAD_DIR = BASE_DIR / "downloads"
    LOG_DIR = BASE_DIR / "logs"
    REQUEST_LOG_DIR = LOG_DIR / "requests"

    # --- NEW SECRETS STRUCTURE ---
    SECRETS_DIR = DATA_DIR / "secrets"

    # All sensitive files go here now
    COOKIES_FILE = SECRETS_DIR / "cookies.json"
    CREDENTIALS_JSON = SECRETS_DIR / "credentials.json"
    TOKEN_PICKLE = SECRETS_DIR / "token.pickle"

    # --- 2. BROWSER ---
    HEADLESS = False 
    WINDOW_SIZE = "390,844"
    SCROLL_PAUSE = 0.5 
    BINARY_LOCATION = None 
    DRIVER_LOCATION = None
    BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"

    # --- 3. DISCORD ---
    DISCORD_TOKEN = Secrets.DISCORD_TOKEN
    REDIS_URL = Secrets.REDIS_URL

    ALLOWED_IDS = [] 

    # --- 4. GOOGLE DRIVE ---
    GDRIVE_ROOT_ID = "1KytugO_3B1TZWN9JbscA_PmQq5-TxPc6"

    # --- 5. LOGGING ---
    LOG_LEVEL = logging.INFO
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # --- 6. SCRAPERS ---
    LOGIN_URL = "https://mechacomic.jp/login"

    # --- CLIENT MAPPING ---
    # Format: { Server_ID_Integer : "Folder Name String" }
    SERVER_MAP = {
        1443643769751736523: "Timeless Toons"
    }
    
    # Fallback name if an unknown server uses the bot
    DEFAULT_CLIENT_NAME = "Timeless Toons"

    @classmethod
    def ensure_dirs(cls):
        """Creates necessary directories."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.SECRETS_DIR.mkdir(parents=True, exist_ok=True) # Creates data/secrets
        cls.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True) # Persistent identity
        cls.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.REQUEST_LOG_DIR.mkdir(parents=True, exist_ok=True)