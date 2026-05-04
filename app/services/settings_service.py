import json
import redis.asyncio as redis
from typing import Literal, TypedDict

NOTIFY_LIMIT = 5
RELEASE_NOTIFY_LIMIT = 5
SETTINGS_KEY = "verzue:settings:{user_id}"
SUB_SETTINGS_KEY = "verzue:subscription:{user_id}:{series_id}"


class NotifyTarget(TypedDict):
    type: Literal["user", "role"]
    id: str


class SettingsService:
    _instance = None

    def __new__(cls, redis_client=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._redis = redis_client
        return cls._instance

    async def get(self, user_id: int) -> dict:
        raw = await self._redis.get(SETTINGS_KEY.format(user_id=user_id))
        return json.loads(raw) if raw else {"notify_targets": []}

    async def get_notify_targets(self, user_id: int) -> list[NotifyTarget]:
        return (await self.get(user_id)).get("notify_targets", [])

    async def add_notify_target(
        self, user_id: int, target_type: Literal["user", "role"], target_id: int
    ) -> tuple[bool, str]:
        settings = await self.get(user_id)
        targets: list[NotifyTarget] = settings.get("notify_targets", [])

        target_id_str = str(getattr(target_id, "id", target_id))
        if any(t["id"] == target_id_str and t["type"] == target_type for t in targets):
            return False, "Already in list."
        if len(targets) >= NOTIFY_LIMIT:
            return False, f"Limit reached ({NOTIFY_LIMIT}). Remove one first."

        targets.append({"type": target_type, "id": target_id_str})
        settings["notify_targets"] = targets
        await self._redis.set(SETTINGS_KEY.format(user_id=user_id), json.dumps(settings))
        return True, "Added."

    async def remove_notify_target(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        settings = await self.get(user_id)
        targets = settings.get("notify_targets", [])
        new_targets = [
            t for t in targets if not (t["id"] == target_id and t["type"] == target_type)
        ]
        if len(new_targets) == len(targets):
            return False
        settings["notify_targets"] = new_targets
        await self._redis.set(SETTINGS_KEY.format(user_id=user_id), json.dumps(settings))
        return True

    async def get_release_notify_targets(self, user_id: int) -> list[NotifyTarget]:
        return (await self.get(user_id)).get("release_notify_targets", [])

    async def add_release_notify_target(
        self, user_id: int, target_type: Literal["user", "role"], target_id: int
    ) -> tuple[bool, str]:
        settings = await self.get(user_id)
        targets: list[NotifyTarget] = settings.get("release_notify_targets", [])
        target_id_str = str(getattr(target_id, "id", target_id))
        if any(t["id"] == target_id_str and t["type"] == target_type for t in targets):
            return False, "Already in list."
        if len(targets) >= RELEASE_NOTIFY_LIMIT:
            return False, f"Limit reached ({RELEASE_NOTIFY_LIMIT}). Remove one first."
        targets.append({"type": target_type, "id": target_id_str})
        settings["release_notify_targets"] = targets
        await self._redis.set(SETTINGS_KEY.format(user_id=user_id), json.dumps(settings))
        return True, "Added."

    async def remove_release_notify_target(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        settings = await self.get(user_id)
        targets = settings.get("release_notify_targets", [])
        new_targets = [
            t for t in targets if not (t["id"] == target_id and t["type"] == target_type)
        ]
        if len(new_targets) == len(targets):
            return False
        settings["release_notify_targets"] = new_targets
        await self._redis.set(SETTINGS_KEY.format(user_id=user_id), json.dumps(settings))
        return True

    @staticmethod
    def format_mentions(targets: list[NotifyTarget]) -> str:
        """Build mention string for chapter completion pings."""
        return " ".join(
            f"<@{getattr(t['id'], 'id', t['id'])}>" 
            if t["type"] == "user" else 
            f"<@&{getattr(t['id'], 'id', t['id'])}>"
            for t in targets
        )

    async def get_subscription_settings(self, user_id: int, series_id: str) -> dict:
        raw = await self._redis.get(SUB_SETTINGS_KEY.format(user_id=user_id, series_id=series_id))
        return json.loads(raw) if raw else {"enabled": True, "custom_title": None}

    async def update_subscription_settings(self, user_id: int, series_id: str, updates: dict):
        settings = await self.get_subscription_settings(user_id, series_id)
        settings.update(updates)
        await self._redis.set(
            SUB_SETTINGS_KEY.format(user_id=user_id, series_id=series_id), 
            json.dumps(settings)
        )
