"""
app/services/login/service.py
Phase 1: account.json plaintext storage replaced with SecretStore.
         Credentials stored as encrypted JSON under namespace "credentials/<platform>".
         Legacy account.json auto-migrated on first get_credentials call.
"""

import json
import logging
import os
import asyncio
from pathlib import Path

from app.services.session_service import SessionService
from app.security.secret_store import SecretStore
from .piccoma_login import PiccomaLoginHandler
from .mecha_login import MechaLoginHandler

logger = logging.getLogger("LoginService")

_NS = "credentials"   # SecretStore namespace for all platform credentials


class LoginService:
    def __init__(self):
        self.session_service = SessionService()
        # Legacy path — only used during one-time migration, never written after Phase 1.
        self._legacy_secrets_path = Path(os.getcwd()) / "data" / "secrets"
        self.piccoma_handler = PiccomaLoginHandler(self)
        self.mecha_handler = MechaLoginHandler(self)

    # ------------------------------------------------------------------
    # One-time migration: account.json → SecretStore
    # ------------------------------------------------------------------

    def _migrate_account_json_if_present(self, platform: str, account_id: str) -> None:
        """
        If a legacy account.json exists for this platform and the SecretStore
        has no entry yet, read the JSON, write to SecretStore, delete the file.
        Safe to call on every get_credentials — no-op after first run.
        """
        legacy_path = self._legacy_secrets_path / platform / "account.json"
        store_key = f"{account_id}"

        if not legacy_path.exists():
            return
        if SecretStore.get(_NS, f"{platform}/{store_key}") is not None:
            # Already migrated — remove the leftover file.
            try:
                legacy_path.unlink()
                logger.info(f"[LoginService] Removed legacy account.json for {platform} (already in vault).")
            except OSError:
                pass
            return

        try:
            with open(legacy_path, "r") as f:
                data = json.load(f)
            SecretStore.put(_NS, f"{platform}/{store_key}", json.dumps(data))
            legacy_path.unlink()
            logger.info(f"[LoginService] Migrated {platform}/account.json → SecretStore and deleted file.")
        except Exception as e:
            logger.warning(f"[LoginService] Could not migrate account.json for {platform}: {e}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_credentials(self, platform: str, account_id: str = "primary") -> dict | None:
        """Retrieve stored credentials for a platform from SecretStore."""
        # Migrate legacy file on first call.
        await asyncio.to_thread(self._migrate_account_json_if_present, platform, account_id)

        raw = SecretStore.get(_NS, f"{platform}/{account_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception as e:
            logger.error(f"[LoginService] Failed to deserialize credentials for {platform}: {e}")
            return None

    async def save_credentials(
        self,
        platform: str,
        email: str,
        password: str,
        account_id: str = "primary",
    ) -> bool:
        """Save credentials for a platform via SecretStore (encrypted, never plaintext)."""
        data = {"email": email, "password": password, "account_id": account_id}
        try:
            await asyncio.to_thread(SecretStore.put, _NS, f"{platform}/{account_id}", json.dumps(data))
            logger.info(f"💾 Saved credentials for {platform}:{account_id}")
            return True
        except Exception as e:
            logger.error(f"[LoginService] Failed to save credentials for {platform}: {e}")
            return False

    async def delete_credentials(self, platform: str, account_id: str = "primary") -> None:
        """Remove credentials from the vault (e.g. on account de-registration)."""
        await asyncio.to_thread(SecretStore.delete, _NS, f"{platform}/{account_id}")
        logger.info(f"🗑️ Deleted credentials for {platform}:{account_id}")

    # ------------------------------------------------------------------
    # Login orchestration (unchanged from original)
    # ------------------------------------------------------------------

    async def auto_login(self, platform: str, account_id: str = "primary") -> bool:
        """Attempts to log in and refresh cookies with a 3-attempt retry bridge."""
        creds = await self.get_credentials(platform, account_id)
        if not creds:
            logger.warning(f"⚠️ No credentials found for {platform}:{account_id}. Automated login skipped.")
            return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"🔑 Attempting login for {platform}:{account_id} (Attempt {attempt}/{max_retries})...")

                if platform == "piccoma":
                    success = await self.piccoma_handler.login(creds)
                elif platform == "mecha":
                    success = await self.mecha_handler.login(creds)
                else:
                    logger.warning(f"🤷 No login implementation for platform: {platform}")
                    return False

                if success:
                    return True

                logger.warning(f"⚠️ Login attempt {attempt} failed for {platform}:{account_id}.")

            except Exception as e:
                logger.error(f"Login attempt {attempt} raised: {e}")

        logger.error(f"❌ All {max_retries} login attempts failed for {platform}:{account_id}.")
        return False
