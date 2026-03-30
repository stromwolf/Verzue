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

    # --- 2. VACANT ---
    # Browser purged for API fast-path.


    # --- 3. DISCORD ---
    DISCORD_TOKEN = Secrets.DISCORD_TOKEN
    HELPER_TOKEN = Secrets.HELPER_TOKEN
    REDIS_URL = Secrets.REDIS_URL
    SCRAPING_PROXY = Secrets.SCRAPING_PROXY

    ADMIN_LOG_CHANNEL_ID = 1488184233229811724
    
    ALLOWED_IDS = [] 
    
    @classmethod
    def get_proxy(cls):
        """Returns the proxy URL if configured, otherwise None."""
        if not cls.SCRAPING_PROXY:
            return None
        return cls.SCRAPING_PROXY

    # --- 4. GOOGLE DRIVE ---
    GDRIVE_ROOT_ID = "1KytugO_3B1TZWN9JbscA_PmQq5-TxPc6"

    # --- 5. LOGGING ---
    LOG_LEVEL = logging.INFO
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # --- 6. SCRAPERS ---
    LOGIN_URL = "https://mechacomic.jp/login"

    # --- 7. RATE LIMITING ---
    DOWNLOAD_DELAY = 3  # Seconds to wait between chapters of the same platform

    # --- CLIENT MAPPING ---
    # --- GROUP REGISTRY ---
    # Consolidated mapping: { "Timeless": [id1, id2], "Verzue": [id3] }
    GROUPS_REGISTRY_FILE = DATA_DIR / "groups_registry.json"

    # Memory state
    GROUP_PROFILES = set()  # Registered names: {"Timeless", "Verzue"}
    SERVER_MAP = {}         # ID -> Name mapping: { 123: "Timeless" }
    DEFAULT_CLIENT_NAME = "Verzue"

    # --- CDN ACCESS LIST ---
    CDN_USERS_FILE = DATA_DIR / "cdn_users.json"
    CDN_ALLOWED_USERS = set()

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
    def load_group_registry(cls):
        """Loads consolidated group profiles and mappings from disk."""
        import json
        if cls.GROUPS_REGISTRY_FILE.exists():
            try:
                with open(cls.GROUPS_REGISTRY_FILE, 'r') as f:
                    data = json.load(f)
                    groups = data.get("groups", {})
                    
                    cls.GROUP_PROFILES = set(groups.keys())
                    cls.SERVER_MAP = {}
                    for name, ids in groups.items():
                        for sid in ids:
                            cls.SERVER_MAP[int(sid)] = name
            except Exception as e:
                logging.error(f"Failed to load Group Registry: {e}")

    @classmethod
    def save_group_registry(cls):
        """Saves consolidated group profiles and mappings to disk."""
        import json
        try:
            # Reconstruct the Group -> [IDs] structure
            output = {"groups": {}}
            for name in cls.GROUP_PROFILES:
                output["groups"][name] = [
                    str(sid) for sid, gname in cls.SERVER_MAP.items() 
                    if gname == name
                ]
            
            with open(cls.GROUPS_REGISTRY_FILE, 'w') as f:
                json.dump(output, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save Group Registry: {e}")

    @classmethod
    def save_group_profiles(cls):
        """Alias for save_group_registry (Backward Compatibility)."""
        cls.save_group_registry()

    @classmethod
    def save_server_map(cls):
        """Alias for save_group_registry (Backward Compatibility)."""
        cls.save_group_registry()

    @classmethod
    def ensure_dirs(cls):
        """Creates necessary directories."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.SECRETS_DIR.mkdir(parents=True, exist_ok=True) # Creates data/secrets
        cls.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cls.LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.REQUEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
        cls.GROUPS_DIR.mkdir(parents=True, exist_ok=True)  # Per-group subscription profiles
        cls.load_group_registry()
        cls.load_cdn_users()
        
        # --- MIGRATION LOGIC ---
        old_profiles_path = cls.DATA_DIR / "group_profiles.json"
        old_map_path = cls.DATA_DIR / "server_map.json"

        if not cls.GROUPS_REGISTRY_FILE.exists() and (old_profiles_path.exists() or old_map_path.exists()):
            try:
                import json
                logging.info("Migrating group data to consolidated registry...")
                
                # Load old profiles
                if old_profiles_path.exists():
                    with open(old_profiles_path, 'r') as f:
                        cls.GROUP_PROFILES = set(json.load(f))
                
                # Load old map
                if old_map_path.exists():
                    with open(old_map_path, 'r') as f:
                        loaded_map = json.load(f)
                        cls.SERVER_MAP = {int(k): v for k, v in loaded_map.items()}
                        # Ensure all names in map are in profiles
                        for v in cls.SERVER_MAP.values():
                            cls.GROUP_PROFILES.add(v)
                
                cls.save_group_registry()
                logging.info(f"Migration complete. Registry saved to {cls.GROUPS_REGISTRY_FILE}")
                
                # Optional: Delete legacy files
                # old_profiles_path.unlink(missing_ok=True)
                # old_map_path.unlink(missing_ok=True)
            except Exception as e:
                logging.error(f"Migration failed: {e}")

        # Seed default profiles on first run
        if not cls.GROUP_PROFILES and cls.SERVER_MAP:
            cls.GROUP_PROFILES = set(cls.SERVER_MAP.values())
            cls.save_group_registry()