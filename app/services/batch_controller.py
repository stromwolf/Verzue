import asyncio
import logging
import discord
from config.settings import Settings
from app.models.chapter import ChapterTask
from app.services.group_manager import get_interested_groups
from typing import List, Optional, Any, Dict

logger = logging.getLogger("BatchController")

class BatchController:
    def __init__(self, bot):
        self.bot = bot
        self.uploader = bot.task_queue.uploader
        self.unlocker = bot.task_queue.unlocker

    async def prepare_batch(self, interaction: discord.Interaction, selected_indices: List[int], all_chapters: List[Dict], title: str, url: str, view_ref: Any = None, series_id: str | None = None, original_title: str | None = None) -> List[ChapterTask]:
        from app.core.logger import req_id_context, group_name_context, log_category_context
        
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel_id if interaction.channel_id else 0
        
        # 🟢 S-GRADE MAPPING: Check channel-specific mapping first, then guild-level, then default
        scan_group = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id, Settings.DEFAULT_CLIENT_NAME)
        req_id = view_ref.req_id if view_ref else "UNKNOWN"

        # 🟢 S-GRADE: Inject Structured Logging Context
        t1 = req_id_context.set(req_id)
        t2 = group_name_context.set(scan_group)
        t3 = log_category_context.set("Requests")

        try:
            if view_ref: 
                view_ref.phases["analyze"] = "loading"
                view_ref.trigger_refresh()

            if not self.uploader:
                return self._create_local_tasks(selected_indices, all_chapters, title, url, scan_group, interaction, series_id, req_id)

            # 1. SETUP DRIVE (Platform -> [ID] - Name -> MAIN/Group)
            # Determine service for platform folder
            service = "Unknown"
            if "mechacomic.jp" in url: service = "Mecha"
            elif "jumptoon.com" in url: service = "Jumptoon"
            elif "piccoma" in url: service = "Piccoma"
            elif "kakao.com" in url: service = "Kakao"
            elif "qq.com" in url: service = "Tencent"
            elif "kuaikanmanhua.com" in url: service = "Kuaikan"

            # 1a. Raws Root Folder
            try:
                root_info = await asyncio.to_thread(self.uploader.client.get_service().files().get(fileId=Settings.GDRIVE_ROOT_ID, fields='name', supportsAllDrives=True).execute)
                root_name = root_info.get('name', '').strip()
                logger.info(f"🏠 Drive Root Name: '{root_name}' (ID: {Settings.GDRIVE_ROOT_ID})")
                
                if root_name.lower() == "raws":
                    logger.info("🎯 Root is already 'Raws', skipping root-level folder creation.")
                    raws_root_id = Settings.GDRIVE_ROOT_ID
                else:
                    raws_root_id = await asyncio.to_thread(self.uploader.create_folder, "Raws", Settings.GDRIVE_ROOT_ID)
            except Exception as e:
                logger.error(f"Failed to check root folder name: {e}")
                raws_root_id = await asyncio.to_thread(self.uploader.create_folder, "Raws", Settings.GDRIVE_ROOT_ID)

            # 1b. Platform Folder (e.g. Jumptoon)
            platform_id = await asyncio.to_thread(self.uploader.create_folder, service, raws_root_id)
            
            # 1c. Series Folder (Search by [ID] prefix)
            prefix = f"[{series_id}]"
            logger.info(f"🔍 Searching for series folder with prefix '{prefix}' for '{original_title}'...")
            folder_data = await asyncio.to_thread(self.uploader.find_folder_by_prefix, prefix, platform_id)
            
            target_series_name = f"{prefix} - {original_title or title}"
            
            if not folder_data:
                logger.info(f"📁 Creating new series folder: '{target_series_name}'")
                drive_series_id = await asyncio.to_thread(self.uploader.create_folder, target_series_name, platform_id)
            else:
                drive_series_id = folder_data['id']
                current_name = folder_data['name']
                if current_name.strip() != target_series_name.strip():
                    logger.info(f"🔄 Renaming series folder: '{current_name}' -> '{target_series_name}'")
                    await asyncio.to_thread(self.uploader.rename_file, drive_series_id, target_series_name)
                else:
                    logger.info(f"✅ Using existing series folder: '{current_name}'")

            # 1d. MAIN & Multi-Group Folders in Parallel
            interested_groups = get_interested_groups(url)
            
            current_group_in_list = False
            for g_name, _ in interested_groups:
                if g_name.lower() == scan_group.lower():
                    current_group_in_list = True
                    break
            
            if not current_group_in_list:
                interested_groups.append((scan_group, title))

            logger.info(f"📡 Parallel Setup: Creating MAIN and {len(interested_groups)} group folders...")

            main_folder_coro = asyncio.to_thread(self.uploader.create_folder, "MAIN", drive_series_id)
            group_folder_coros = []
            for g_name, g_title in interested_groups:
                display_group = g_name if " team" in g_name.lower() else f"{g_name} Team"
                client_folder_name = f"{display_group} - {g_title}"
                group_folder_coros.append(asyncio.to_thread(self.uploader.create_folder, client_folder_name, drive_series_id))
                
            results = await asyncio.gather(main_folder_coro, *group_folder_coros)
            main_id = results[0]
            
            all_interested_folders = []
            requester_folder = None
            for i, (g_name, g_title) in enumerate(interested_groups):
                c_id = results[i+1] # Skip index 0 (MAIN)
                display_group = g_name if " team" in g_name.lower() else f"{g_name} Team"
                client_folder_name = f"{display_group} - {g_title}"
                
                folder_info = {'id': c_id, 'name': client_folder_name, 'group': g_name}
                all_interested_folders.append(folder_info)
                if g_name.lower() == scan_group.lower():
                    requester_folder = folder_info

            # 1e. Parallel Permissions
            logger.info("🌍 Parallel Permissions: Making folders public...")
            permission_coros = [asyncio.to_thread(self.uploader.make_public, f['id']) for f in all_interested_folders]
            permission_coros.append(asyncio.to_thread(self.uploader.make_public, main_id))
            await asyncio.gather(*permission_coros)

            main_manifest = await asyncio.to_thread(self.uploader.list_all_items, main_id)

            tasks_to_queue = []
            chapters_to_unlock = []
            task_client_folders = [requester_folder] if requester_folder else []

            if view_ref and hasattr(view_ref, '_full_scan_task') and view_ref._full_scan_task:
                logger.info("⏳ Finalizing chapter mapping before download...")
                await view_ref._full_scan_task
                all_chapters = view_ref.all_chapters

            shortcuts_to_create = [] # (main_existing_id, folder_name)
            if view_ref: view_ref.existing_links = {}

            for idx in selected_indices:
                ch_data = all_chapters[idx]
                task = self._make_task(idx, ch_data, title, url, scan_group, interaction, series_id, req_id)
                folder_name = task.folder_name
                
                task.main_folder_id = main_id
                task.client_folder_id = requester_folder.get('id') if requester_folder else None
                task.client_folders = task_client_folders
                task.series_title_id = drive_series_id
                task.final_folder_name = folder_name
                
                is_locked = ch_data.get('is_locked', "jumptoon.com" in url)
                main_existing_id = main_manifest.get(folder_name)
                
                if main_existing_id:
                    if requester_folder:
                        shortcuts_to_create.append((main_existing_id, folder_name))
                    if view_ref:
                        link = await asyncio.to_thread(self.uploader.get_share_link, main_existing_id)
                        view_ref.existing_links[task.chapter_str] = {"link": link, "title": task.title}
                    continue
                
                temp_name = f"[Uploading] {folder_name}"
                if temp_name in main_manifest:
                    task.pre_created_folder_id = main_manifest.get(temp_name)
                    task.final_folder_name = folder_name
                
                tasks_to_queue.append(task)
                if is_locked and ("mechacomic.jp" in url or "piccoma.com" in url or "jumptoon.com" in url):
                    chapters_to_unlock.append(task)

            if shortcuts_to_create:
                logger.info(f"🔗 Parallel Shortcuts: Creating {len(shortcuts_to_create)} shortcuts...")
                req_folder_id = requester_folder.get('id')
                await asyncio.gather(*[
                    asyncio.to_thread(self.uploader.create_shortcut, sid, req_folder_id, name)
                    for sid, name in shortcuts_to_create
                ])

            if chapters_to_unlock:
                if view_ref: 
                    view_ref.purchase_count = len(chapters_to_unlock)
                    view_ref.phases["purchase"] = "loading"
                    view_ref.trigger_refresh()
                await self.unlocker.unlock_batch(chapters_to_unlock, view_ref=view_ref)
                if view_ref: view_ref.phases["purchase"] = "done"
            else:
                if view_ref: view_ref.phases["purchase"] = "done"

            if view_ref:
                if len(selected_indices) == 1:
                    idx = list(selected_indices)[0]
                    ch_data = all_chapters[idx]
                    folder_name = self._make_task(idx, ch_data, title, url, scan_group, interaction, series_id, req_id).folder_name
                    main_existing_id = main_manifest.get(folder_name)
                    view_ref.final_link = await asyncio.to_thread(self.uploader.get_share_link, main_existing_id) if main_existing_id else None
                else:
                    view_ref.final_link = await asyncio.to_thread(self.uploader.get_share_link, requester_folder['id']) if requester_folder else None
                
                view_ref.phases["analyze"] = "done"
                view_ref.trigger_refresh()

            return tasks_to_queue
        finally:
            req_id_context.reset(t1)
            group_name_context.reset(t2)
            log_category_context.reset(t3)

    def _make_task(self, idx, ch, title, url, group, interaction, sid, req_id):
        ch_url = ch.get('url')
        if not ch_url:
            if "jumptoon.com" in url and 'id' in ch:
                if sid is not None and sid != "None":
                    ch_url = f"https://jumptoon.com/series/{sid}/episodes/{ch['id']}/"
                else:
                    ch_url = f"{url.rstrip('/')}/episodes/{ch['id']}/"
            else:
                ch_url = url

        # 🟢 Detect service for progress reporting
        service = "unknown"
        if "mechacomic.jp" in url: service = "Mecha"
        elif "jumptoon.com" in url: service = "Jumptoon"
        elif "piccoma" in url: service = "Piccoma"
        elif "kakao.com" in url: service = "Kakao"
        elif "qq.com" in url: service = "Tencent"
        elif "kuaikanmanhua.com" in url: service = "Kuaikan"

        # 🟢 Jumptoon Special Handling: Use semantic sub-index for hiatus folders (e.g. 45.1 - 休載)
        if service == "Jumptoon":
            nt = ch.get('notation', '').strip()
            chapter_str = nt if nt else (ch.get('_display_idx') or str(idx + 1))
            # 🟢 S-GRADE: Combine notation and title for the Drive folder name
            tt = ch.get('title', '').strip()
            title_val = f"{nt} - {tt}" if nt and tt else (nt or tt or "Chapter")
        elif service == "Mecha":
            # 🟢 S-GRADE: Use explicit notation (e.g. 001話) instead of index (1, 2, 3)
            chapter_str = ch.get('notation') or str(idx + 1)
            title_val = ch.get('title', '')
        elif service == "Piccoma":
            # 🟢 S-GRADE: Use explicit notation (e.g. 第1話) instead of index (1, 2, 3)
            chapter_str = ch.get('notation') or str(idx + 1)
            title_val = ch.get('title', '')
        else:
            chapter_str = ch.get('notation') or ch.get('number_text') or str(idx + 1)
            title_val = ch.get('title', '')

        return ChapterTask(
            id=idx+1, title=title_val, chapter_str=chapter_str,
            url=ch_url, series_title=title, requester_id=interaction.user.id, channel_id=interaction.channel_id,
            guild_id=interaction.guild.id if interaction.guild else 0, guild_name=interaction.guild.name if interaction.guild else "DM",
            scan_group=group, series_id_key=str(sid), episode_id=str(ch.get('id', '')), episode_number=str(ch.get('number', '')), is_smartoon=True, req_id=req_id,
            service=service
        )

    def _create_local_tasks(self, idxs, chs, title, url, group, interaction, sid, req_id):
        return [self._make_task(i, chs[i], title, url, group, interaction, sid, req_id) for i in idxs]
