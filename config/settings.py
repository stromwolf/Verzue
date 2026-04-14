import json
import logging
from pathlib import Path

from .secrets import Secrets


class Settings:
    """Static configuration — paths, constants, IDs. Never mutated."""

    # --- Paths ---
    BASE_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = BASE_DIR / "data"
    DOWNLOAD_DIR = BASE_DIR / "downloads"
    LOG_DIR = BASE_DIR / "logs"
    REQUEST_LOG_DIR = LOG_DIR / "requests"
    SECRETS_DIR = DATA_DIR / "secrets"
    GROUPS_DIR = DATA_DIR / "groups"

    COOKIES_FILE = SECRETS_DIR / "cookies.json"
    CREDENTIALS_JSON = SECRETS_DIR / "credentials.json"
    TOKEN_PICKLE = SECRETS_DIR / "token.pickle"
    GROUPS_REGISTRY_FILE = DATA_DIR / "groups_registry.json"
    CDN_USERS_FILE = DATA_DIR / "cdn_users.json"

    # --- Discord ---
    ADMIN_LOG_CHANNEL_ID = 1488184233229811724
    SUBSCRIPTION_LOG_CHANNEL_ID = 1488459767952445480
    ALLOWED_IDS: list = []

    # --- Google Drive ---
    GDRIVE_ROOT_ID = "1KytugO_3B1TZWN9JbscA_PmQq5-TxPc6"

    # --- Logging ---
    LOG_LEVEL = logging.INFO
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # --- Scrapers ---
    LOGIN_URL = "https://mechacomic.jp/login"
    STITCH_HEIGHT = 13000
    DOWNLOAD_DELAY = 1.5

    # --- Secrets (populated after load()) ---
    DISCORD_TOKEN: str
    HELPER_TOKEN: str | None
    STAGING_TOKEN: str | None
    REDIS_URL: str
    SCRAPING_PROXY: str | None
    DEVELOPER_MODE: bool
    DEFAULT_CLIENT_NAME = "Verzue"

    @classmethod
    def ensure_dirs(cls) -> None:
        """Creates required directories. Safe to call multiple times."""
        Secrets.load()
        cls.DISCORD_TOKEN = Secrets.DISCORD_TOKEN
        cls.HELPER_TOKEN = Secrets.HELPER_TOKEN
        cls.STAGING_TOKEN = Secrets.STAGING_TOKEN
        cls.REDIS_URL = Secrets.REDIS_URL
        cls.SCRAPING_PROXY = Secrets.SCRAPING_PROXY
        cls.DEVELOPER_MODE = Secrets.DEVELOPER_MODE

        for directory in (
            cls.DATA_DIR,
            cls.SECRETS_DIR,
            cls.DOWNLOAD_DIR,
            cls.LOG_DIR,
            cls.REQUEST_LOG_DIR,
            cls.GROUPS_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class AppState:
    """Runtime mutable state. Instantiate once; inject into services.

    Separating this from Settings means:
    - Settings stays testable without touching disk or env vars.
    - State has a clear owner and is not globally mutated.
    - Multiple isolated states can exist in tests.
    """

    def __init__(self) -> None:
        self.group_profiles: set[str] = set()
        self.server_map: dict[int, str] = {}
        self.cdn_allowed_users: set[int] = set()

    # --- CDN users ---

    def load_cdn_users(self) -> None:
        if not Settings.CDN_USERS_FILE.exists():
            return
        try:
            with open(Settings.CDN_USERS_FILE) as f:
                self.cdn_allowed_users = {int(x) for x in json.load(f)}
        except Exception as e:
            logging.error(f"Failed to load CDN users: {e}")

    def save_cdn_users(self) -> None:
        try:
            with open(Settings.CDN_USERS_FILE, "w") as f:
                json.dump(list(self.cdn_allowed_users), f)
        except Exception as e:
            logging.error(f"Failed to save CDN users: {e}")

    # --- Group registry ---

    def load_group_registry(self) -> None:
        if not Settings.GROUPS_REGISTRY_FILE.exists():
            return
        try:
            with open(Settings.GROUPS_REGISTRY_FILE) as f:
                data = json.load(f)
            groups: dict = data.get("groups", {})
            self.group_profiles = set(groups.keys())
            self.server_map = {
                int(sid): name
                for name, ids in groups.items()
                for sid in ids
            }
        except Exception as e:
            logging.error(f"Failed to load group registry: {e}")

    def save_group_registry(self) -> None:
        try:
            output: dict = {"groups": {}}
            for name in self.group_profiles:
                output["groups"][name] = [
                    str(sid)
                    for sid, gname in self.server_map.items()
                    if gname == name
                ]
            with open(Settings.GROUPS_REGISTRY_FILE, "w") as f:
                json.dump(output, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save group registry: {e}")

    def save_group_profiles(self) -> None:
        """Alias for save_group_registry (backward compatibility)."""
        self.save_group_registry()

    def save_server_map(self) -> None:
        """Alias for save_group_registry (backward compatibility)."""
        self.save_group_registry()

    def load_state(self) -> None:
        """Load all runtime state from disk."""
        self.load_group_registry()
        self.load_cdn_users()
        if not self.group_profiles and self.server_map:
            self.group_profiles = set(self.server_map.values())
            self.save_group_registry()

    def migrate_legacy_data(self) -> None:
        """One-time migration from old split files to unified registry.

        Safe to call on every startup — exits immediately if already migrated
        or if no legacy files exist.
        """
        old_profiles = Settings.DATA_DIR / "group_profiles.json"
        old_map = Settings.DATA_DIR / "server_map.json"

        if Settings.GROUPS_REGISTRY_FILE.exists():
            return
        if not old_profiles.exists() and not old_map.exists():
            return

        try:
            logging.info("Migrating group data to consolidated registry...")
            if old_profiles.exists():
                with open(old_profiles) as f:
                    self.group_profiles = set(json.load(f))
            if old_map.exists():
                with open(old_map) as f:
                    loaded = json.load(f)
                    self.server_map = {int(k): v for k, v in loaded.items()}
                    for v in self.server_map.values():
                        self.group_profiles.add(v)
            self.save_group_registry()
            logging.info(f"Migration complete. Saved to {Settings.GROUPS_REGISTRY_FILE}")
        except Exception as e:
            logging.error(f"Migration failed: {e}")