import os
import logging
from dotenv import load_dotenv

# Load environment variables from the .env file in the root directory
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Check for critical secrets
_token = os.getenv("DISCORD_TOKEN")
_helper_token = os.getenv("HELPER_TOKEN")
_redis_url = os.getenv("REDIS_URL")

if not _token:
    logging.warning(f"⚠️  DISCORD_TOKEN not found! Searched in {env_path}")
if not _helper_token:
    logging.warning(f"⚠️  HELPER_TOKEN not found! Searched in {env_path}")
if not _redis_url:
    logging.warning(f"⚠️  REDIS_URL not found! Searched in {env_path}")

class Secrets:
    """Access point for secret environment variables."""
    DISCORD_TOKEN = _token
    HELPER_TOKEN = _helper_token
    STAGING_TOKEN = os.getenv("STAGING_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL")
    SCRAPING_PROXY = os.getenv("SCRAPING_PROXY")