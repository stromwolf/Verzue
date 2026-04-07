import logging
import json
import os
import asyncio
from app.services.session_service import SessionService
from .piccoma_login import PiccomaLoginHandler
from .mecha_login import MechaLoginHandler

logger = logging.getLogger("LoginService")

class LoginService:
    def __init__(self):
        self.session_service = SessionService()
        self.base_secrets_path = os.path.join(os.getcwd(), "data", "secrets")
        self.piccoma_handler = PiccomaLoginHandler(self)
        self.mecha_handler = MechaLoginHandler(self)

    async def get_credentials(self, platform: str, account_id: str = "primary"):
        """Retrieves stored credentials for a platform."""
        path = os.path.join(self.base_secrets_path, platform, "account.json")
        if not os.path.exists(path):
            return None
        
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read credentials for {platform}: {e}")
            return None

    async def save_credentials(self, platform: str, email: str, password: str, account_id: str = "primary"):
        """Saves credentials for a platform."""
        dir_path = os.path.join(self.base_secrets_path, platform)
        os.makedirs(dir_path, exist_ok=True)
        
        path = os.path.join(dir_path, "account.json")
        data = {"email": email, "password": password, "account_id": account_id}
        
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=4)
            logger.info(f"💾 Saved credentials for {platform}:{account_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save credentials for {platform}: {e}")
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
