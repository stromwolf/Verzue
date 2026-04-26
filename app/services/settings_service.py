import json
import redis.asyncio as redis
from typing import Literal, TypedDict

NOTIFY_LIMIT = 5
SETTINGS_KEY = "verzue:settings:{user_id}"


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

        if any(t["id"] == str(target_id) and t["type"] == target_type for t in targets):
            return False, "Already in list."
        if len(targets) >= NOTIFY_LIMIT:
            return False, f"Limit reached ({NOTIFY_LIMIT}). Remove one first."

        targets.append({"type": target_type, "id": str(target_id)})
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

    @staticmethod
    def format_mentions(targets: list[NotifyTarget]) -> str:
        """Build mention string for chapter completion pings."""
        return " ".join(
            f"<@{t['id']}>" if t["type"] == "user" else f"<@&{t['id']}>"
            for t in targets
        )
