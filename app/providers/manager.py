import importlib
import inspect
import logging
import os
from typing import Dict, Type
from app.providers.base import BaseProvider
from app.core.exceptions import MechaException

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

        loaded_providers = []
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
                            loaded_providers.append(f"                  -   {platform_name} ({obj.__name__})")
                except Exception as e:
                    logger.error(f"❌ Failed to load provider module {module_name}: {e}")
        
        if loaded_providers:
            summary = "\n" + "\n".join(loaded_providers)
            logger.info(f"🔌 Loaded Provider{summary}")

    def get_provider(self, platform: str) -> BaseProvider:
        """Returns the requested provider instance or raises MechaException."""
        p_name = platform.lower().strip()
        
        # 🟢 S-GRADE: Platform Alias Normalization
        # Handles user-provided names or legacy DB entries
        if "mecha" in p_name: p_name = "mecha"
        elif "kakao" in p_name: p_name = "kakao"
        elif "jumptoon" in p_name: p_name = "jumptoon"
        elif "piccoma" in p_name: p_name = "piccoma"
        elif "kuaikan" in p_name: p_name = "kuaikan"
        elif "ac.qq" in p_name or "acqq" in p_name: p_name = "acqq"
        
        provider = self._providers.get(p_name)
        if not provider:
            logger.error(f"⚠️ Provider lookup failed for platform: {platform} (Mapped: {p_name})")
            raise MechaException(f"No provider found for platform: {platform}", code="SY_002")
        return provider

    def get_provider_for_url(self, url: str) -> BaseProvider:
        """S-Grade URL Routing: Maps a URL to the correct provider or raises MechaException."""
        url_lower = url.lower()
        if "mechacomic.jp" in url_lower: return self.get_provider("mecha")
        if "jumptoon.com" in url_lower: return self.get_provider("jumptoon")
        if "piccoma.com" in url_lower: return self.get_provider("piccoma")
        if "kakao.com" in url_lower: return self.get_provider("kakao")
        if "kuaikanmanhua.com" in url_lower: return self.get_provider("kuaikan")
        if "ac.qq.com" in url_lower: return self.get_provider("acqq")
        
        raise MechaException(f"Unsupported platform URL: {url}", code="SY_002")

    def list_providers(self):
        """Returns a list of all loaded platform identifiers."""
        return list(self._providers.keys())
