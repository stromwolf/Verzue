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
    VAULT_FILE = SECRETS_DIR / ".vault.json"
    VAULT_KEY_FILE = SECRETS_DIR / ".vault_key"
    FLAGS_FILE = DATA_DIR / "feature_flags.json"

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
    TESTING_BOT_TOKEN: str | None
    ADMIN_BOT_TOKEN: str | None
    REDIS_URL: str
    SCRAPING_PROXY: str | None
    DEVELOPER_MODE: bool
    DEFAULT_CLIENT_NAME = "Verzue"

    @classmethod
    def ensure_dirs(cls) -> None:
        """Creates required directories and applies safety checks."""
        Secrets.load()
        cls.DISCORD_TOKEN = Secrets.DISCORD_TOKEN
        cls.TESTING_BOT_TOKEN = Secrets.TESTING_BOT_TOKEN
        cls.ADMIN_BOT_TOKEN = Secrets.ADMIN_BOT_TOKEN
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

        cls._tighten_secret_files()
        cls._assert_redis_safety()

    @classmethod
    def _tighten_secret_files(cls) -> None:
        """Walks SECRETS_DIR and chmod 0600s .json/.pickle files."""
        if not cls.SECRETS_DIR.exists():
            return

        import os
        for root, _, files in os.walk(cls.SECRETS_DIR):
            for file in files:
                if file.endswith((".json", ".pickle", ".vault_key")):
                    path = Path(root) / file
                    try:
                        os.chmod(path, 0o600)
                    except Exception as e:
                        logging.warning(f"Failed to tighten permissions for {path}: {e}")

    @classmethod
    def _assert_redis_safety(cls) -> None:
        """Blocks remote plain redis://, allows loopback + rediss://."""
        import os
        if os.getenv("VERZUE_ALLOW_INSECURE_REDIS") == "1":
            return

        url = cls.REDIS_URL.lower()
        if url.startswith("rediss://"):
            return

        # Check for loopback
        is_loopback = any(x in url for x in ("localhost", "127.0.0.1", "::1"))
        if not is_loopback:
            raise RuntimeError(
                f"Insecure remote Redis detected: {cls.REDIS_URL}. "
                "Only rediss:// or loopback are allowed in production. "
                "Set VERZUE_ALLOW_INSECURE_REDIS=1 to bypass."
            )

    @classmethod
    def get_proxy(cls) -> str | None:
        """Returns the global scraping proxy URL if configured."""
        return getattr(cls, 'SCRAPING_PROXY', None)


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
        self.feature_flags: dict[str, bool] = {
            "notifications": True,
            "downloads": True,
            "downloads.piccoma": True,
            "downloads.jumptoon": True,
            "downloads.mecha": True,
            "downloads.kakao": True,
            "downloads.kuaikan": True,
            "downloads.acqq": True,
            "notifications.piccoma": True,
            "notifications.jumptoon": True,
            "notifications.mecha": True,
            "notifications.kakao": True,
            "notifications.kuaikan": True,
            "notifications.acqq": True,
        }
        self.group_flags: dict[str, dict[str, bool]] = {}

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

    # --- Feature Flags ---

    def is_enabled(self, feature: str, group: str | None = None) -> bool:
        """
        Check if feature is enabled.
        Group-specific override wins over global.
        """
        # 1. Group override wins — check both exact feature AND its parent
        if group and group in self.group_flags:
            g_flags = self.group_flags[group]
            if feature in g_flags:
                return g_flags[feature]

            # 🟢 FIX: If checking a child (e.g. downloads.jumptoon),
            # also check group override for parent (downloads)
            parts = feature.split(".")
            if len(parts) > 1:
                parent = parts[0]
                if parent in g_flags:
                    return g_flags[parent]

        # 2. Parent flag (global scope only)
        parts = feature.split(".")
        if len(parts) > 1:
            parent = parts[0]
            if not self.feature_flags.get(parent, True):
                return False

        # 3. Global fallback
        return self.feature_flags.get(feature, True)

    def set_flag(self, feature: str, value: bool, group: str | None = None) -> bool:
        """Set flag. Returns False if unknown feature."""
        if feature not in self.feature_flags:
            return False
        if group:
            if group not in self.group_flags:
                self.group_flags[group] = {}
            self.group_flags[group][feature] = value
        else:
            self.feature_flags[feature] = value
        self.save_feature_flags()
        return True

    def clear_group_flag(self, feature: str, group: str) -> bool:
        """Remove group-specific override, revert to global."""
        if group in self.group_flags and feature in self.group_flags[group]:
            del self.group_flags[group][feature]
            if not self.group_flags[group]:
                del self.group_flags[group]
            self.save_feature_flags()
            return True
        return False

    def load_feature_flags(self) -> None:
        if not Settings.FLAGS_FILE.exists():
            return
        try:
            with open(Settings.FLAGS_FILE) as f:
                saved = json.load(f)
            # Only load known keys, ignore stale ones
            for k in self.feature_flags:
                if k in saved:
                    self.feature_flags[k] = bool(saved[k])
            
            overrides = saved.get("_group_overrides", {})
            for g, flags in overrides.items():
                self.group_flags[g] = {k: bool(v) for k, v in flags.items()}
        except Exception as e:
            logging.error(f"Failed to load feature flags: {e}")

    def save_feature_flags(self) -> None:
        try:
            Settings.FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = dict(self.feature_flags)
            if self.group_flags:
                data["_group_overrides"] = self.group_flags
            with open(Settings.FLAGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logging.error(f"Failed to save feature flags: {e}")

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
        self.load_feature_flags()
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