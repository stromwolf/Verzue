import os
import logging
from pathlib import Path
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_loaded = False


def _load_once() -> None:
    global _loaded
    if _loaded:
        return
    load_dotenv(dotenv_path=_ENV_PATH)
    _loaded = True


class Secrets:
    """Read-only access point for secret environment variables.

    Attributes are populated on first access via load().
    Required secrets raise EnvironmentError if absent.
    Optional secrets return None.
    """

    DISCORD_TOKEN: str
    REDIS_URL: str
    HELPER_TOKEN: str | None
    STAGING_TOKEN: str | None
    SCRAPING_PROXY: str | None
    DEVELOPER_MODE: bool

    @classmethod
    def load(cls) -> None:
        """Load and validate all secrets. Safe to call multiple times."""
        _load_once()

        # --- Required secrets — raise immediately if missing ---
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise EnvironmentError(
                f"DISCORD_TOKEN is required but not set. Checked: {_ENV_PATH}"
            )

        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise EnvironmentError(
                f"REDIS_URL is required but not set. Checked: {_ENV_PATH}"
            )

        cls.DISCORD_TOKEN = token
        cls.REDIS_URL = redis_url

        # --- Optional secrets — None if absent, no noise ---
        cls.HELPER_TOKEN = os.getenv("HELPER_TOKEN") or None
        cls.STAGING_TOKEN = os.getenv("STAGING_TOKEN") or None
        cls.SCRAPING_PROXY = os.getenv("SCRAPING_PROXY") or None
        cls.DEVELOPER_MODE = os.getenv("DEVELOPER_MODE", "false").lower() == "true"

        log.debug("secrets.loaded", extra={"env_path": str(_ENV_PATH)})