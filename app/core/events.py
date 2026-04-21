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
        
        # ─── Prevent duplicate subscriptions ───────────────────────────────────
        # Compare by qualname + id to catch both bound method and closure duplicates
        for existing in cls._listeners[event_name]:
            if existing == callback or (
                hasattr(existing, '__func__') and hasattr(callback, '__func__')
                and existing.__func__ is callback.__func__
                and existing.__self__.__class__ is callback.__self__.__class__
            ):
                logger.warning(
                    f"[EventBus] Duplicate subscription rejected for '{event_name}': "
                    f"{callback.__qualname__ if hasattr(callback, '__qualname__') else callback}"
                )
                return
        # ───────────────────────────────────────────────────────────────────────
        
        cls._listeners[event_name].append(callback)
        logger.debug(f"[EventBus] Subscribed {callback.__qualname__ if hasattr(callback, '__qualname__') else callback} to '{event_name}' ({len(cls._listeners[event_name])} total)")

    @classmethod
    def unsubscribe(cls, event_name: str, callback: Callable):
        if event_name in cls._listeners:
            try:
                # Use standard list removal. Comparison logic in subscribe ensures
                # that what we added is what we remove.
                cls._listeners[event_name].remove(callback)
                logger.debug(f"[EventBus] Unsubscribed {callback.__qualname__ if hasattr(callback, '__qualname__') else callback} from '{event_name}' ({len(cls._listeners[event_name])} left)")
            except ValueError:
                # Callback wasn't in the list
                pass

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