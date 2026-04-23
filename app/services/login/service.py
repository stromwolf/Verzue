import logging
import json
import os
import asyncio
from app.services.session_service import SessionService
from app.core.secret_store import SecretStore
from .piccoma_login import PiccomaLoginHandler
from .mecha_login import MechaLoginHandler

logger = logging.getLogger("LoginService")

class LoginService:
    def __init__(self):
        self.session_service = SessionService()
        self.secret_store = SecretStore()
        self.base_secrets_path = os.path.join(os.getcwd(), "data", "secrets")
        self.piccoma_handler = PiccomaLoginHandler(self)
        self.mecha_handler = MechaLoginHandler(self)

    def _migrate_account_json_if_present(self, platform: str, account_id: str):
        """One-time migration from account.json to SecretStore."""
        path = os.path.join(self.base_secrets_path, platform, "account.json")
        if os.path.exists(path):
            try:
                logger.info(f"📦 Legacy account.json found for {platform}. Migrating...")
                with open(path, "r") as f:
                    data = json.load(f)
                
                key = f"creds:{platform}:{account_id}"
                if not self.secret_store.get(key):
                    self.secret_store.set(key, json.dumps(data))
                    logger.info(f"🚚 Migrated credentials for {platform} to SecretStore.")
                
                os.remove(path)
                # Try to remove empty dir
                try:
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass 
            except Exception as e:
                logger.error(f"Failed to migrate credentials for {platform}: {e}")

    async def get_credentials(self, platform: str, account_id: str = "primary"):
        """Retrieves stored credentials for a platform."""
        self._migrate_account_json_if_present(platform, account_id)
        
        key = f"creds:{platform}:{account_id}"
        data_str = self.secret_store.get(key)
        if not data_str:
            return None
        
        try:
            return json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to read/parse credentials for {platform}: {e}")
            return None

    async def save_credentials(self, platform: str, email: str, password: str, account_id: str = "primary"):
        """Saves credentials for a platform."""
        key = f"creds:{platform}:{account_id}"
        data = {"email": email, "password": password, "account_id": account_id}
        
        try:
            self.secret_store.set(key, json.dumps(data))
            logger.info(f"💾 Saved credentials for {platform}:{account_id} to SecretStore")
            return True
        except Exception as e:
            logger.error(f"Failed to save credentials for {platform}: {e}")
            return False

    async def delete_credentials(self, platform: str, account_id: str = "primary"):
        """Removes credentials for a platform."""
        key = f"creds:{platform}:{account_id}"
        try:
            self.secret_store.delete(key)
            logger.info(f"🗑️ Deleted credentials for {platform}:{account_id} from SecretStore")
            return True
        except Exception as e:
            logger.error(f"Failed to delete credentials for {platform}: {e}")
            return False

    async def auto_login(self, platform: str, account_id: str = "primary"):
        """Attempts to log in and refresh cookies with a 3-attempt retry bridge."""
        creds = await self.get_credentials(platform, account_id)
        if not creds:
            logger.warning(f"⚠️ No credentials found for {platform}:{account_id}. Automated login skipped.")
            return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"🔑 Attempting automated login for {platform}:{account_id} (Attempt {attempt}/{max_retries})...")
                
                if platform == "piccoma":
                    success = await self.piccoma_handler.login(creds)
                elif platform == "mecha":
                    success = await self.mecha_handler.login(creds)
                else:
                    logger.warning(f"🤷 No implementation for platform: {platform}")
                    return False

                if success:
                    return True
                
                logger.warning(f"⚠️ Login attempt {attempt} failed. Retrying...")

            except Exception as e:
                logger.error(f"❌ Attempt {attempt} failed with System/Proxy Error: {e}")
                if attempt < max_retries:
                    wait_time = attempt * 2 
                    logger.info(f"⏳ Waiting {wait_time}s before next retry...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.critical(f"💀 All {max_retries} attempts failed for {platform}.")
        
        return False
