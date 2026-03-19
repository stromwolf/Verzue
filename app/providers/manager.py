import importlib
import inspect
import logging
import os
from typing import Dict, Type
from app.providers.base import BaseProvider

logger = logging.getLogger("ProviderManager")

class ProviderManager:
    """
    S-Grade Dynamic Discovery Engine (Phase 6).
    Responsible for auto-loading providers from the app/providers/platforms directory.
    """
    _instance = None
    _providers: Dict[str, BaseProvider] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ProviderManager, cls).__new__(cls)
            cls._instance._load_providers()
        return cls._instance

    def _load_providers(self):
        """Discovers and instantiates all providers in the platforms directory."""
        platforms_dir = os.path.join("app", "providers", "platforms")
        if not os.path.exists(platforms_dir):
            os.makedirs(platforms_dir, exist_ok=True)
            return

        for filename in os.listdir(platforms_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                module_name = f"app.providers.platforms.{filename[:-3]}"
                try:
                    module = importlib.import_module(module_name)
                    for name, obj in inspect.getmembers(module):
                        if (inspect.isclass(obj) and 
                            issubclass(obj, BaseProvider) and 
                            obj is not BaseProvider):
                            
                            # Use a convention or attribute for the platform name
                            platform_name = getattr(obj, "IDENTIFIER", name.lower().replace("provider", ""))
                            self._providers[platform_name] = obj()
                            logger.info(f"🔌 Loaded Provider: {platform_name} ({obj.__name__})")
                except Exception as e:
                    logger.error(f"❌ Failed to load provider module {module_name}: {e}")

    def get_provider(self, platform: str) -> BaseProvider:
        """Returns the requested provider instance."""
        provider = self._providers.get(platform.lower())
        if not provider:
            logger.error(f"⚠️ Provider not found for platform: {platform}")
        return provider

    def get_provider_for_url(self, url: str) -> BaseProvider:
        """S-Grade URL Routing: Maps a URL to the correct provider."""
        url_lower = url.lower()
        if "mechacomic.jp" in url_lower: return self.get_provider("mecha")
        if "jumptoon.com" in url_lower: return self.get_provider("jumptoon")
        if "piccoma.com" in url_lower: return self.get_provider("piccoma")
        if "kakao.com" in url_lower: return self.get_provider("kakao")
        if "kuaikanmanhua.com" in url_lower: return self.get_provider("kuaikan")
        if "ac.qq.com" in url_lower: return self.get_provider("acqq")
        return None

    def list_providers(self):
        """Returns a list of all loaded platform identifiers."""
        return list(self._providers.keys())
