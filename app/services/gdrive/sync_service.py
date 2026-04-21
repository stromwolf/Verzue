import asyncio
import logging
from config.settings import Settings
from app.core.utils import extract_series_id, get_service_name

logger = logging.getLogger("DriveSyncService")

async def sync_group_folder_name(bot, group_name: str, series_url: str, 
                                  override_title: str,
                                  original_title: str | None = None,
                                  fix_series_folder: bool = False):
    """
    Finds the series and group folders on Drive and renames them.
    fix_series_folder: If True, renames the parent series folder back to the original scraped title.
    override_title: The custom English title for the group's subfolder.
    """
    try:
        # 🟢 Handle both Main Bot and Helper Bot contexts
        uploader = bot.task_queue.uploader if hasattr(bot, 'task_queue') and bot.task_queue.uploader \
                   else getattr(getattr(bot, 'main_bot', None), 'task_queue', None) and bot.main_bot.task_queue.uploader

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

        # 2. Find Series Folder
        prefix = f"[{series_id}]"
        series_folder = await asyncio.to_thread(uploader.find_folder_by_prefix, prefix, platform_id)
        if not series_folder:
            logger.info(f"Drive Sync: No series folder found with prefix '{prefix}'")
            return

        series_folder_id = series_folder['id']

        # 🟢 POLICY: Restore Series Folder name back to original if it was wrongly renamed
        if fix_series_folder and original_title:
            correct_series_name = f"{prefix} - {original_title}"
            if series_folder['name'].strip() != correct_series_name.strip():
                logger.info(f"Drive Sync: Restoring series folder '{series_folder['name']}' -> '{correct_series_name}'")
                await asyncio.to_thread(uploader.rename_file, series_folder_id, correct_series_name)

        # 3. Find and Rename Group Folder inside Series Folder
        group_prefix = group_name if " team" in group_name.lower() else f"{group_name} Team"
        group_folder = await asyncio.to_thread(uploader.find_folder_by_prefix, group_prefix, series_folder_id)
        
        if not group_folder:
            logger.info(f"Drive Sync: No group folder found for '{group_name}' in series '{series_id}'")
            return

        new_group_name = f"{group_prefix} - {override_title}"
        if group_folder['name'].strip() != new_group_name.strip():
            logger.info(f"Drive Sync: Renaming group folder '{group_folder['name']}' -> '{new_group_name}'")
            await asyncio.to_thread(uploader.rename_file, group_folder['id'], new_group_name)
        else:
            logger.info(f"Drive Sync: Group folder '{new_group_name}' already correctly named.")

    except Exception as e:
        logger.error(f"Drive Sync failed for {group_name} @ {series_url}: {e}")
