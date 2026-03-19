import asyncio
import logging
from config.settings import Settings
from app.core.utils import extract_series_id, get_service_name

logger = logging.getLogger("DriveSyncService")

async def sync_group_folder_name(bot, group_name: str, series_url: str, new_title: str):
    """
    Finds the group's shortcut folder for a series on Drive and renames it.
    This runs in the background to avoid blocking the bot.
    """
    try:
        uploader = bot.main_bot.task_queue.uploader
        if not uploader:
            logger.warning("Drive Sync skipped: Uploader not initialized.")
            return

        service_name = get_service_name(series_url)
        series_id = extract_series_id(series_url)
        
        if not series_id or service_name == "Unknown":
            logger.warning(f"Drive Sync skipped: Could not extract ID from {series_url}")
            return

        # 1. Ensure Raws Root
        try:
            # Check if the root folder itself is named "Raws"
            root_info = await asyncio.to_thread(uploader.client.get_service().files().get(fileId=Settings.GDRIVE_ROOT_ID, fields='name', supportsAllDrives=True).execute)
            root_name = root_info.get('name', '').strip()
            
            if root_name.lower() == "raws":
                raws_id = Settings.GDRIVE_ROOT_ID
            else:
                raws_id = await asyncio.to_thread(uploader.find_folder, "Raws", Settings.GDRIVE_ROOT_ID)
        except Exception as e:
            logger.error(f"Failed to check root folder name: {e}")
            raws_id = await asyncio.to_thread(uploader.find_folder, "Raws", Settings.GDRIVE_ROOT_ID)

        if not raws_id:
            logger.warning("Drive Sync skipped: 'Raws' folder not found.")
            return
            
        platform_id = await asyncio.to_thread(uploader.find_folder, service_name, raws_id)
        if not platform_id:
            logger.warning(f"Drive Sync skipped: Platform folder '{service_name}' not found.")
            return

        # 2. Find Series Folder by [ID] prefix
        prefix = f"[{series_id}]"
        series_folder = await asyncio.to_thread(uploader.find_folder_by_prefix, prefix, platform_id)
        if not series_folder:
            logger.info(f"Drive Sync: No series folder found with prefix '{prefix}'")
            return
            
        series_folder_id = series_folder['id']

        # 3. Find Group Folder inside Series Folder
        # Convention: "[GroupName] Team - [AnyTitle]"
        # We search by Group Name prefix
        group_prefix = group_name if " team" in group_name.lower() else f"{group_name} Team"
        
        # We need a prefix search for the group folder too. 
        # uploader has find_folder_by_prefix
        group_folder = await asyncio.to_thread(uploader.find_folder_by_prefix, group_prefix, series_folder_id)
        
        if not group_folder:
            logger.info(f"Drive Sync: No folder found for group '{group_name}' in series '{series_id}'")
            return

        # 4. Rename
        new_folder_name = f"{group_prefix} - {new_title}"
        if group_folder['name'] != new_folder_name:
            logger.info(f"Drive Sync: Renaming '{group_folder['name']}' -> '{new_folder_name}'")
            await asyncio.to_thread(uploader.rename_file, group_folder['id'], new_folder_name)
        else:
            logger.info(f"Drive Sync: Folder '{new_folder_name}' already correctly named.")

    except Exception as e:
        logger.error(f"Drive Sync failed for {group_name} @ {series_url}: {e}")
