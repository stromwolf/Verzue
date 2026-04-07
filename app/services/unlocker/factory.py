import logging
from .mecha import MechaUnlocker
from .piccoma import PiccomaUnlocker
from .jumptoon import JumptoonUnlocker

logger = logging.getLogger("UnlockerFactory")

class UnlockerFactory:
    _registry = {
        "mecha": MechaUnlocker,
        "piccoma": PiccomaUnlocker,
        "jumptoon": JumptoonUnlocker
    }

    @classmethod
    def get_unlocker(cls, service_name: str, provider_manager):
        """
        Returns an instance of the appropriate platform unlocker.
        """
        unlocker_cls = cls._registry.get(service_name)
        if not unlocker_cls:
            return None
        
        try:
            provider = provider_manager.get_provider(service_name)
            return unlocker_cls(provider)
        except Exception as e:
            logger.error(f"Failed to initialize unlocker for {service_name}: {e}")
            return None
