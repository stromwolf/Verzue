import asyncio
import logging
from typing import Callable, Dict, List

logger = logging.getLogger("EventBus")

class EventBus:
    _listeners: Dict[str, List[Callable]] = {}

    @classmethod
    def subscribe(cls, event_name: str, callback: Callable):
        if event_name not in cls._listeners:
            cls._listeners[event_name] = []
        cls._listeners[event_name].append(callback)

    @classmethod
    async def emit(cls, event_name: str, *args, **kwargs):
        if event_name in cls._listeners:
            for callback in cls._listeners[event_name]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(*args, **kwargs)
                    else:
                        callback(*args, **kwargs)
                except Exception as e:
                    logger.error(f"Event {event_name} Error: {e}")