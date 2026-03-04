import asyncio
import logging
from config.settings import Settings
from app.models.chapter import ChapterTask

logger = logging.getLogger("BatchController")

class BatchController:
    def __init__(self, bot):
        self.bot = bot
        self.uploader = bot.task_queue.uploader
        self.unlocker = bot.task_queue.scraper_registry.unlocker

    async def prepare_batch(self, interaction, selected_indices, all_chapters, title, url, view_ref=None, series_id=None):
        guild_id = interaction.guild.id if interaction.guild else 0
        scan_group = Settings.SERVER_MAP.get(guild_id, Settings.DEFAULT_CLIENT_NAME)
        req_id = view_ref.req_id if view_ref else "UNKNOWN"

        if view_ref: 
            view_ref.phases["analyze"] = "loading"
            view_ref.trigger_refresh()

        if not self.uploader:
            return self._create_local_tasks(selected_indices, all_chapters, title, url, scan_group, interaction, series_id, req_id)

        # 1. SETUP DRIVE (Top-Level Only)
        # We only create the Series, MAIN, and Client folders. Individual chapters are offloaded to workers.
        drive_series_id = await asyncio.to_thread(self.uploader.create_folder, title, Settings.GDRIVE_ROOT_ID)
        main_id = await asyncio.to_thread(self.uploader.create_folder, "MAIN", drive_series_id)
        client_folder_name = f"{scan_group}_{title}"
        client_id = await asyncio.to_thread(self.uploader.create_folder, client_folder_name, drive_series_id)
        
        # Ensure they are public (inherited by children)
        await asyncio.to_thread(self.uploader.make_public, main_id)
        await asyncio.to_thread(self.uploader.make_public, client_id)

        # Quick check of what already exists to avoid redundant task queueing
        main_manifest = await asyncio.to_thread(self.uploader.list_all_items, main_id)
        client_manifest = await asyncio.to_thread(self.uploader.list_all_items, client_id)

        tasks_to_queue = []
        chapters_to_unlock = []

        for idx in selected_indices:
            ch_data = all_chapters[idx]
            task = self._make_task(idx, ch_data, title, url, scan_group, interaction, series_id, req_id)
            folder_name = task.folder_name
            
            # Pass top-level IDs so the worker knows where to put things
            task.main_folder_id = main_id
            task.client_folder_id = client_id
            task.series_title_id = drive_series_id
            task.final_folder_name = folder_name
            
            is_locked = ch_data.get('is_locked', "jumptoon.com" in url)
            
            # Check if this chapter is already finished
            main_existing_id = main_manifest.get(folder_name)
            if main_existing_id:
                # If it's in MAIN but not the client folder, create the shortcut now (fast)
                if folder_name not in client_manifest:
                    await asyncio.to_thread(self.uploader.create_shortcut, main_existing_id, client_id, folder_name)
                # Skip queueing if it already exists in MAIN
                continue
            
            # Check if it's currently being uploaded by another worker
            temp_name = f"[Uploading] {folder_name}"
            if temp_name in main_manifest:
                # Task is already active or failed halfway, we can re-queue it
                task.pre_created_folder_id = main_manifest[temp_name]
                task.final_folder_name = folder_name
            
            tasks_to_queue.append(task)
            if is_locked:
                chapters_to_unlock.append(task)

        # 2. PHASE 3 UNLOCK
        if chapters_to_unlock:
            if view_ref: 
                view_ref.purchase_count = len(chapters_to_unlock)
                view_ref.phases["purchase"] = "loading"
                view_ref.trigger_refresh()
            
            await self.unlocker.unlock_batch(chapters_to_unlock, view_ref=view_ref)
            
            if "jumptoon.com" in url:
                self.bot.task_queue.scraper_registry.jumptoon._load_cookies()
            
            if view_ref: view_ref.phases["purchase"] = "done"
        else:
            if view_ref: view_ref.phases["purchase"] = "done"

        # 3. FINAL LINK
        # For batches, we link to the Client folder. For single, we'd ideally link to the chapter, 
        # but since we haven't created it yet, we link to the Client folder as a reliable fallback.
        if view_ref:
            view_ref.final_link = await asyncio.to_thread(self.uploader.get_share_link, client_id)
            view_ref.phases["analyze"] = "done"
            view_ref.trigger_refresh()

        return tasks_to_queue

    def _make_task(self, idx, ch, title, url, group, interaction, sid, req_id):
        return ChapterTask(
            id=idx+1, title=ch.get('title', ''), chapter_str=ch.get('number_text', str(idx+1)),
            url=ch['url'], series_title=title, requester_id=interaction.user.id, channel_id=interaction.channel_id,
            guild_id=interaction.guild.id if interaction.guild else 0, guild_name=interaction.guild.name if interaction.guild else "DM",
            scan_group=group, series_id_key=str(sid), episode_id=str(ch['id']), is_smartoon=True, req_id=req_id
        )

    def _create_local_tasks(self, idxs, chs, title, url, group, interaction, sid, req_id):
        return [self._make_task(i, chs[i], title, url, group, interaction, sid, req_id) for i in idxs]
