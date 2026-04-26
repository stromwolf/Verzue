import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from config.settings import Settings
from app.services.redis_manager import RedisManager

logger = logging.getLogger("GroupManager")
redis_brain = RedisManager()


def _group_filename(group_name: str) -> Path:
    """Converts 'Timeless Toons' → data/groups/Timeless_Toons.json"""
    safe = re.sub(r'[^\w\s-]', '', group_name).strip()
    safe = re.sub(r'[\s]+', '_', safe)
    return Settings.GROUPS_DIR / f"{safe}.json"


def _clean_url(url: str) -> str:
    """Standardizes URL for matching (https + no trailing slash)."""
    return url.replace("http://", "https://").rstrip("/")


def delete_group(group_name: str) -> bool:
    """Deletes a group profile JSON from disk. Returns True if deleted."""
    path = _group_filename(group_name)
    if path.exists():
        try:
            path.unlink()
            logger.info(f"[GroupManager] Deleted group: {group_name}")
            return True
        except Exception as e:
            logger.error(f"[GroupManager] Failed to delete {path.name}: {e}")
    return False


def rename_group_profile(old_name: str, new_name: str) -> bool:
    """Renames a group's profile file on disk. Returns True if successful."""
    old_path = _group_filename(old_name)
    new_path = _group_filename(new_name)
    if not old_path.exists():
        logger.error(f"[GroupManager] Cannot rename: {old_name} profile does not exist.")
        return False
    if new_path.exists():
        logger.error(f"[GroupManager] Cannot rename: {new_name} profile already exists.")
        return False
    try:
        old_path.rename(new_path)
        logger.info(f"[GroupManager] Renamed group profile: {old_name} → {new_name}")
        return True
    except Exception as e:
        logger.error(f"[GroupManager] Failed to rename {old_path.name} to {new_path.name}: {e}")
        return False


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
    
    # 🟢 S-GRADE: Async update to Redis Index (Fire and Forget)
    async def _bg_sync():
        await redis_brain.clear_group_schedule(group_name)
        for sub in data.get("subscriptions", []):
            series_id = sub.get("series_id")
            title = sub.get("series_title")
            day = sub.get("release_day")
            ch_id = sub.get("channel_id")
            url = sub.get("series_url") or ""
            
            # Determine platform
            platform = None
            if "jumptoon" in url.lower(): platform = "jumptoon"
            elif "piccoma" in url.lower(): platform = "piccoma"
            elif "mecha" in url.lower(): platform = "mecha"
            
            # Update metadata index
            await redis_brain.update_subs_index(series_id, group_name, title, ch_id, url)
            
            # Update daily schedule index
            if day:
                await redis_brain.update_schedule_index(group_name, day, series_id, platform=platform)

    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_bg_sync())
    except Exception:
        pass


def ensure_group_file(group_name: str):
    """Creates the group profile JSON if it doesn't already exist."""
    path = _group_filename(group_name)
    if not path.exists():
        save_group(group_name, {"subscriptions": []})
        logger.info(f"[GroupManager] Created profile: {path.name}")


def add_subscription(group_name: str, sub: dict) -> bool:
    """
    Adds or updates a subscription in a group profile.
    If the series_id exists, it updates the channel and schedule.
    """
    data = load_group(group_name)
    existing_index = -1
    for i, s in enumerate(data["subscriptions"]):
        if s["series_id"] == sub["series_id"] and s.get("channel_id") == sub.get("channel_id"):
            existing_index = i
            break
            
    if existing_index >= 0:
        # Update existing
        data["subscriptions"][existing_index].update(sub)
        logger.info(f"[GroupManager] Updated subscription for {sub['series_id']} (Channel: {sub.get('channel_id')}) in {group_name}")
    else:
        # Add new
        data["subscriptions"].append(sub)
        logger.info(f"[GroupManager] Added new subscription for {sub['series_id']} (Channel: {sub.get('channel_id')}) in {group_name}")
        
    save_group(group_name, data)
    return True


def remove_subscription(group_name: str, target_url: str) -> bool:
    """
    Removes a subscription by URL.
    Returns True if removed, False if not found.
    """
    data = load_group(group_name)
    target_clean = _clean_url(target_url)
    
    # S-GRADE: Find series_id BEFORE removing so we can clean Redis Index
    series_id_to_remove = None
    for s in data.get("subscriptions", []):
        if _clean_url(s.get("series_url", "")) == target_clean:
            series_id_to_remove = s.get("series_id")
            break
    
    if not series_id_to_remove:
        return False
        
    data["subscriptions"] = [
        s for s in data["subscriptions"]
        if _clean_url(s["series_url"]) != target_clean
    ]
    
    save_group(group_name, data)

    # 🟢 S-GRADE: Async removal from Redis Index (Fire and Forget)
    if series_id_to_remove:
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(redis_brain.remove_indexed_sub(group_name, series_id_to_remove))
        except Exception:
            pass

    return True


def set_release_day(group_name: str, target_url: str, day: str) -> bool:
    """
    Sets the weekly release day for a subscription via URL.
    Returns True if updated, False if series not found.
    """
    data = load_group(group_name)
    target_clean = _clean_url(target_url)
    
    for sub in data["subscriptions"]:
        if _clean_url(sub["series_url"]) == target_clean:
            sub["release_day"] = day.capitalize()
            save_group(group_name, data)
            return True
    return False


def update_last_chapter(group_name: str, series_id: str, chapter_id: str):
    data = load_group(group_name)
    changed = False
    for sub in data["subscriptions"]:
        if sub["series_id"] == series_id:
            sub["last_known_chapter_id"] = chapter_id
            changed = True
    
    if changed:
        save_group(group_name, data)
        logger.info(f"[GroupManager] Updated all subscriptions for {series_id} in {group_name} to chapter {chapter_id}")


def update_last_up_chapter(group_name: str, series_id: str, chapter_id: str):
    """S-Grade: Specifically tracks the last notified 'UP' flag for Piccoma to avoid spam."""
    data = load_group(group_name)
    changed = False
    for sub in data["subscriptions"]:
        if sub["series_id"] == series_id:
            sub["last_up_chapter_id"] = chapter_id
            changed = True
    
    if changed:
        save_group(group_name, data)
        logger.info(f"[GroupManager] Updated UP-notification tracker for {series_id} in {group_name} to chapter {chapter_id}")


async def is_series_subscribed_for_group(series_id: str, group_name: str) -> bool:
    """
    Checks if a series is already tracked within a specific group.
    Returns True if subscribed in this group.
    """
    # 1. Check Redis Fast Index (Group-Scoped)
    indexed = await redis_brain.get_indexed_sub(group_name, series_id)
    if indexed:
        return True

    # 2. Fallback to Disk & Update Index
    path = _group_filename(group_name)
    if not path.exists():
        return False
        
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for sub in data.get("subscriptions", []):
            if sub.get("series_id") == series_id:
                # Repopulate index
                await redis_brain.update_subs_index(series_id, group_name, sub.get("series_title"))
                return True
    except Exception:
        pass
        
    return False


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


def get_series_by_channel(channel_id: int) -> tuple[str, dict] | None:
    """Finds which series is subscribed to the given channel ID."""
    all_subs = get_all_subscriptions()
    for group_name, sub in all_subs:
        if sub.get("channel_id") == channel_id:
            return group_name, sub
    return None


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


def set_title_override(group_name: str, series_url: str, english_title: str):
    """Saves a custom English title for a series within a group profile."""
    data = load_group(group_name)
    overrides = data.setdefault("title_overrides", {})
    clean = _clean_url(series_url)
    overrides[clean] = english_title
    save_group(group_name, data)
    logger.info(f"[GroupManager] Title override set for {group_name}: {clean} → {english_title}")


def get_title_override(group_name: str, series_url: str) -> str | None:
    """Returns the custom English title for a series in a group, or None."""
    data = load_group(group_name)
    overrides = data.get("title_overrides", {})
    clean = _clean_url(series_url)
    return overrides.get(clean)


def set_group_emoji(group_name: str, emoji: str):
    """Saves a custom emoji for the group profile."""
    data = load_group(group_name)
    data["emoji"] = emoji.strip()
    save_group(group_name, data)
    logger.info(f"[GroupManager] Emoji set for {group_name}: {data['emoji']}")


def get_group_emoji(group_name: str) -> str | None:
    """Returns the custom emoji for the group profile, or None."""
    data = load_group(group_name)
    return data.get("emoji")


def get_next_notification_id(group_name: str) -> int:
    """
    Returns the next N-ID for a group and increments the counter.
    The counter is stored as 'next_notification_id' in the group JSON.
    """
    data = load_group(group_name)
    nid = data.get("next_notification_id", 1)
    data["next_notification_id"] = nid + 1
    save_group(group_name, data)
    return nid


def get_interested_groups(series_url: str) -> list[tuple[str, str]]:
    """
    Finds all groups that have a title override for the given series URL.
    Returns a list of (group_name, overridden_title) tuples.
    """
    results = []
    if not Settings.GROUPS_DIR.exists():
        return results
    
    clean_target = _clean_url(series_url)
    
    for path in Settings.GROUPS_DIR.glob("*.json"):
        group_name = path.stem.replace('_', ' ')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            overrides = data.get("title_overrides", {})
            for url, title in overrides.items():
                if _clean_url(url) == clean_target:
                    results.append((group_name, title))
                    break # Only one override per series per group
        except Exception as e:
            logger.error(f"[GroupManager] Error reading {path.name}: {e}")
    return results


def get_drive_folder_cache(group_name: str, series_url: str) -> dict:
    """Returns cached Drive folder IDs for a series, or empty dict."""
    data = load_group(group_name)
    clean = _clean_url(series_url)
    for sub in data.get("subscriptions", []):
        if _clean_url(sub.get("series_url", "")) == clean:
            return {
                "platform_folder_id": sub.get("drive_platform_folder_id"),
                "series_folder_id":   sub.get("drive_series_folder_id"),
                "main_folder_id":     sub.get("drive_main_folder_id"),
                "group_folder_id":    sub.get("drive_group_folder_id"),
            }
    return {}


def set_drive_folder_cache(group_name: str, series_url: str, ids: dict):
    """Persists resolved Drive folder IDs into the subscription entry."""
    data = load_group(group_name)
    clean = _clean_url(series_url)
    for sub in data.get("subscriptions", []):
        if _clean_url(sub.get("series_url", "")) == clean:
            sub["drive_platform_folder_id"] = ids.get("platform_folder_id")
            sub["drive_series_folder_id"]   = ids.get("series_folder_id")
            sub["drive_main_folder_id"]     = ids.get("main_folder_id")
            sub["drive_group_folder_id"]    = ids.get("group_folder_id")
            save_group(group_name, data)
            logger.info(f"[GroupManager] Drive folder cache saved for {series_url}")
            return


async def sync_index_to_redis():
    """One-time startup sync to populate Redis from JSON files (Per-Group Indexing)."""
    logger.info("🔄 [GroupManager] Syncing local group data to Redis Index (Per-Group)...")
    if not Settings.GROUPS_DIR.exists(): return
    
    count = 0
    for path in Settings.GROUPS_DIR.glob("*.json"):
        group_name = path.stem.replace('_', ' ')
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for sub in data.get("subscriptions", []):
                series_id = sub.get("series_id")
                title = sub.get("series_title")
                day = sub.get("release_day")
                ch_id = sub.get("channel_id")
                url = sub.get("series_url") or ""
                
                # Determine platform
                platform = None
                if "jumptoon" in url.lower(): platform = "jumptoon"
                elif "piccoma" in url.lower(): platform = "piccoma"
                elif "mecha" in url.lower(): platform = "mecha"
                
                await redis_brain.update_subs_index(series_id, group_name, title, ch_id, url)
                if day:
                    await redis_brain.update_schedule_index(group_name, day, series_id, platform=platform)
                count += 1
        except Exception as e:
            logger.error(f"Failed to sync {path.name}: {e}")
            
    # --- MIGRATION: Cleanup Legacy Global Index ---
    try:
        # If the old global key exists, delete it after we've re-indexed everything into per-group hashes
        await redis_brain.client.delete("verzue:index:subs")
        logger.info("🧹 [GroupManager] Legacy global index 'verzue:index:subs' removed.")
    except Exception as e:
        logger.debug(f"Legacy index cleanup skipped: {e}")

    logger.info(f"✅ [GroupManager] Synced {count} subscriptions to Redis.")
