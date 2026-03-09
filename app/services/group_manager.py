import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from config.settings import Settings

logger = logging.getLogger("GroupManager")


def _group_filename(group_name: str) -> Path:
    """Converts 'Timeless Toons' → data/groups/Timeless_Toons.json"""
    safe = re.sub(r'[^\w\s-]', '', group_name).strip()
    safe = re.sub(r'[\s]+', '_', safe)
    return Settings.GROUPS_DIR / f"{safe}.json"


def load_group(group_name: str) -> dict:
    """Loads a group profile JSON. Returns a default structure if not found."""
    path = _group_filename(group_name)
    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[GroupManager] Failed to load {path.name}: {e}")
    return {"subscriptions": []}


def save_group(group_name: str, data: dict):
    """Saves a group profile JSON to disk."""
    path = _group_filename(group_name)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[GroupManager] Failed to save {path.name}: {e}")


def ensure_group_file(group_name: str):
    """Creates the group profile JSON if it doesn't already exist."""
    path = _group_filename(group_name)
    if not path.exists():
        save_group(group_name, {"subscriptions": []})
        logger.info(f"[GroupManager] Created profile: {path.name}")


def add_subscription(group_name: str, sub: dict) -> bool:
    """
    Adds a subscription to a group profile.
    Returns False if the series_id is already subscribed in this group.
    """
    data = load_group(group_name)
    existing_ids = {s["series_id"] for s in data["subscriptions"]}
    if sub["series_id"] in existing_ids:
        return False
    data["subscriptions"].append(sub)
    save_group(group_name, data)
    return True


def remove_subscription(group_name: str, target_url: str) -> bool:
    """
    Removes a subscription by URL.
    Returns True if removed, False if not found.
    """
    data = load_group(group_name)
    before = len(data["subscriptions"])
    
    # Strip trailing slashes and ignore http/https differences for robust matching
    def clean_url(u): return u.replace("http://", "https://").rstrip("/")
    
    target_clean = clean_url(target_url)
    
    data["subscriptions"] = [
        s for s in data["subscriptions"]
        if clean_url(s["series_url"]) != target_clean
    ]
    if len(data["subscriptions"]) == before:
        return False
    save_group(group_name, data)
    return True


def set_release_day(group_name: str, target_url: str, day: str) -> bool:
    """
    Sets the weekly release day for a subscription via URL.
    Returns True if updated, False if series not found.
    """
    data = load_group(group_name)
    def clean_url(u): return u.replace("http://", "https://").rstrip("/")
    target_clean = clean_url(target_url)
    
    for sub in data["subscriptions"]:
        if clean_url(sub["series_url"]) == target_clean:
            sub["release_day"] = day.capitalize()
            save_group(group_name, data)
            return True
    return False


def update_last_chapter(group_name: str, series_id: str, chapter_id: str):
    """Updates the last known chapter ID after an auto-download."""
    data = load_group(group_name)
    for sub in data["subscriptions"]:
        if sub["series_id"] == series_id:
            sub["last_known_chapter_id"] = chapter_id
            save_group(group_name, data)
            return


def is_series_subscribed_globally(series_id: str) -> tuple[bool, str]:
    """
    Checks across ALL group profiles if a series is already subscribed.
    Returns (True, group_name) if found, (False, '') otherwise.
    """
    if not Settings.GROUPS_DIR.exists():
        return False, ''
    for path in Settings.GROUPS_DIR.glob("*.json"):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for sub in data.get("subscriptions", []):
                if sub.get("series_id") == series_id:
                    # Derive group name from filename (reverse of _group_filename)
                    name = path.stem.replace('_', ' ')
                    return True, name
        except Exception:
            continue
    return False, ''


def get_all_subscriptions() -> list[tuple[str, dict]]:
    """
    Returns a flat list of (group_name, subscription_dict) tuples
    for all groups and all subscriptions.
    """
    results = []
    if not Settings.GROUPS_DIR.exists():
        return results
    for path in Settings.GROUPS_DIR.glob("*.json"):
        group_name = path.stem.replace('_', ' ')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for sub in data.get("subscriptions", []):
                results.append((group_name, sub))
        except Exception as e:
            logger.error(f"[GroupManager] Error reading {path.name}: {e}")
    return results


def set_admin_settings(group_name: str, channel_id: int, role_id: int = None):
    """Saves admin notification settings for a group."""
    data = load_group(group_name)
    data["admin_settings"] = {
        "channel_id": channel_id,
        "role_id": role_id
    }
    save_group(group_name, data)


def get_admin_settings(group_name: str) -> dict:
    """Retrieves admin notification settings for a group."""
    data = load_group(group_name)
    return data.get("admin_settings", {})
