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
    HELPER_TOKEN = Secrets.HELPER_TOKEN
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
    SERVER_MAP_FILE = DATA_DIR / "server_map.json"
    
    # Fallback name if an unknown server uses the bot
    DEFAULT_CLIENT_NAME = "Verzue"

    # Default memory map
    SERVER_MAP = {}

    # --- CDN ACCESS LIST ---
    CDN_USERS_FILE = DATA_DIR / "cdn_users.json"
    CDN_ALLOWED_USERS = set()

    # --- GROUP PROFILES ---
    # Registered group names (pre-created via $group-add, e.g. "Thunder Scan")
    GROUP_PROFILES_FILE = DATA_DIR / "group_profiles.json"
    GROUP_PROFILES = set()  # e.g. {"Timeless Toons", "Thunder Scan"}

    # --- GROUP SUBSCRIPTION PROFILES ---
    # Per-group JSON files live here: data/groups/<GroupName>.json
    GROUPS_DIR = DATA_DIR / "groups"

    @classmethod
    def load_cdn_users(cls):
        """Loads allowed CDN users from disk."""
        import json
        if cls.CDN_USERS_FILE.exists():
            try:
                with open(cls.CDN_USERS_FILE, 'r') as f:
                    loaded = json.load(f)
                    cls.CDN_ALLOWED_USERS = set(int(x) for x in loaded)
            except Exception as e:
                logging.error(f"Failed to load CDN Users Map: {e}")

    @classmethod
    def save_cdn_users(cls):
        """Saves allowed CDN users to disk."""
        import json
        try:
            with open(cls.CDN_USERS_FILE, 'w') as f:
                json.dump(list(cls.CDN_ALLOWED_USERS), f)
        except Exception as e:
            logging.error(f"Failed to save CDN Users Map: {e}")

    @classmethod
    def load_group_profiles(cls):
        """Loads registered group profile names from disk."""
        import json
        if cls.GROUP_PROFILES_FILE.exists():
            try:
                with open(cls.GROUP_PROFILES_FILE, 'r') as f:
                    cls.GROUP_PROFILES = set(json.load(f))
            except Exception as e:
                logging.error(f"Failed to load Group Profiles: {e}")

    @classmethod
    def save_group_profiles(cls):
        """Saves registered group profile names to disk."""
        import json
        try:
            with open(cls.GROUP_PROFILES_FILE, 'w') as f:
                json.dump(list(cls.GROUP_PROFILES), f)
        except Exception as e:
            logging.error(f"Failed to save Group Profiles: {e}")

    @classmethod
    def load_server_map(cls):
        """Loads the dynamically set scan names from disk."""
        import json
        if cls.SERVER_MAP_FILE.exists():
            try:
                with open(cls.SERVER_MAP_FILE, 'r') as f:
                    # JSON stores keys as strings, so we convert back to int
                    loaded = json.load(f)
                    cls.SERVER_MAP = {int(k): v for k, v in loaded.items()}
            except Exception as e:
                logging.error(f"Failed to load Server Map: {e}")

    @classmethod
    def save_server_map(cls):
        """Saves the current scan names to disk."""
        import json
        try:
            with open(cls.SERVER_MAP_FILE, 'w') as f:
                json.dump(cls.SERVER_MAP, f)
        except Exception as e:
            logging.error(f"Failed to save Server Map: {e}")

    @classmethod
    def ensure_dirs(cls):
        """Creates necessary directories."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.SECRETS_DIR.mkdir(parents=True, exist_ok=True) # Creates data/secrets
        cls.BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True) # Persistent identity
        cls.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.REQUEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.GROUPS_DIR.mkdir(parents=True, exist_ok=True)  # Per-group subscription profiles
        cls.load_server_map()
        cls.load_cdn_users()
        cls.load_group_profiles()
        # Seed default profiles on first run so existing groups still work
        if not cls.GROUP_PROFILES and cls.SERVER_MAP:
            cls.GROUP_PROFILES = set(cls.SERVER_MAP.values())
            cls.save_group_profiles()