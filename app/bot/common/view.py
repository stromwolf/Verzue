from __future__ import annotations
import discord, asyncio, math, logging, time, re
from typing import TYPE_CHECKING, List, Dict, Any, Optional
from app.models.chapter import ChapterTask, TaskStatus

if TYPE_CHECKING:
    pass

logger = logging.getLogger("Dashboard")
ICONS = {"load": "<a:waiting:1482424619746201601>", "tick": "[DONE]", "wait": "[   ]"}
COLORS = {"mecha": 0xe67e22, "smartoon": 0x2ecc71, "jumptoon": 0x9b59b6, "piccoma": 0xffd600, "kuaikan": 0xf1c40f}

class UniversalDashboard:
    active_views: dict[str, UniversalDashboard] = {}  # Global router for raw V2 interactions

    # --- Type Hints for Linter ---
    bot: Any
    url: str
    title: str
    all_chapters: list[dict]
    original_title: str
    total_chapters: int
    image_url: str
    req_id: str
    series_id: str
    status_label: Optional[str]
    user: str
    service_type: str
    genre_label: Optional[str]
    color: int
    page: int
    per_page: int
    max_page: int
    selected_indices: set[int]
    active_tasks: list[ChapterTask]
    phases: dict[str, str]
    interaction: Optional[discord.Interaction]
    sub_status: Any
    processing_mode: bool
    _last_hash: int
    retry_active: bool
    existing_links: dict[str, Any]
    message_id: Optional[int]
    channel_id: Optional[int]

    def __init__(self, bot, ctx_data, service_type):
        self.bot = bot
        self.url, self.title, self.all_chapters = ctx_data['url'], ctx_data['title'], ctx_data['chapters']
        self.original_title = ctx_data.get('original_title', self.title)
        # Fallback to length of chapters if total_chapters isn't provided (e.g. for older scraper implementations)
        self.total_chapters = ctx_data.get('total_chapters', len(self.all_chapters))
        self.image_url, self.req_id, self.series_id, self.user = ctx_data['image_url'], ctx_data['req_id'], ctx_data['series_id'], ctx_data['user']
        self.status_label = ctx_data.get('status_label')
        self.genre_label = ctx_data.get('genre_label')
        self.message_id: Optional[int] = None
        self.channel_id: Optional[int] = None
        self.service_type, self.color = service_type, COLORS.get(service_type, 0x2b2d31)
        
        
        if self.service_type == "mecha" and getattr(self.bot.task_queue, "browser_service", None):
            self.bot.task_queue.browser_service.inc_session()

        self.page, self.per_page = 1, 10
        self.max_page = max(1, math.ceil(self.total_chapters / self.per_page)) if self.total_chapters else 1
        self.selected_indices, self.active_tasks = set(), []
        
        self.phases = {"analyze": "waiting", "purchase": "waiting", "download": "waiting"}
        self.interaction, self.sub_status, self.processing_mode, self._last_hash = None, None, False, 0
        self.retry_active = False
        self.existing_links = {} # 🟢 S-GRADE: {chapter_str: link} for pre-existing chapters
        self.any_waiters = False # 🟢 S-GRADE: Flag for in-flight tasks
        self._latest_ui_update: float = 0.0 # 🟢 Throttle for background updates
        
        # 🟢 UI Toggle for Selection Mode Menu
        self.show_selection_menu = False
        
        # Session Timeout Tracking (15 mins)
        self.last_interaction_time = time.time()
        self.creation_time = time.time()
        self.timeout_task = asyncio.create_task(self._auto_timeout_loop())
        
        # Register to global router
        UniversalDashboard.active_views[self.req_id] = self

        # 🟢 BACKGROUND SCAN: Fetch remaining chapters for Mecha/Jumptoon (Faster initial load)
        self._full_scan_task: asyncio.Task | None = None
        self._bg_scanning = False
        if self.service_type in ["mecha", "jumptoon"] and self.total_chapters > len(self.all_chapters):
            self._bg_scanning = True
            logger.info(f"[{self.req_id}] 📡 Initial Load: {len(self.all_chapters)}/{self.total_chapters} chapters. Starting Background Scan.")
            
            async def _delayed_scan():
                # Wait until message_id is set (max 5s) before triggering any refresh
                for _ in range(50):  # 50 × 0.1s = 5s max wait
                    if self.message_id is not None:
                        break
                    await asyncio.sleep(0.1)
                
                if self.message_id is None:
                    logger.warning(f"[{self.req_id}] Background scan aborted: message_id never set.")
                    return
                    
                await self._perform_full_scan()

            self._full_scan_task = asyncio.create_task(_delayed_scan())

    async def _auto_timeout_loop(self):
        """Background loop to clear memory if abandoned for >30 minutes."""
        timeout_seconds = 1800  # 30 mins (as requested)
        while True:
            await asyncio.sleep(60)  # Check every minute
            
            # Stop if the view was manually cancelled/closed by the user
            if self.req_id not in UniversalDashboard.active_views:
                break
                
            if self.phases.get("download") in ["loading", "done"]:
                # 🟢 PERMANENCE: Once downloading starts or completes, we never expire the session.
                # This ensures the user can see progress and final results indefinitely.
                break 

            if time.time() - self.last_interaction_time > timeout_seconds:
                if self.service_type == "mecha" and getattr(self.bot.task_queue, "browser_service", None):
                    try: self.bot.task_queue.browser_service.dec_session()
                    except: pass
                UniversalDashboard.active_views.pop(self.req_id, None)
                logger.info(f"[{self.req_id}] ⏳ Terminated inactive session (Auto-Cleanup).")
                # Attempt to edit the message to show it expired using the fallback route
                if getattr(self, 'interaction', None):
                    try:
                        route = discord.http.Route(
                            'DELETE',
                            f'/channels/{self.interaction.channel_id}/messages/{self.interaction.message.id}'
                        )
                        await self.bot.http.request(route)
                    except: pass
                break

    def _get_footer_action_row(self):
        """Constructs a unified, premium footer Action Row for help/recovery (No error button)."""
        has_failures = any(t.status == TaskStatus.FAILED for t in self.active_tasks)
        
        # 1. Retry Failed Button (Only if failures exist)
        footer_components = []
        if has_failures:
            footer_components.append({
                "type": 2, "style": 3, # Success (Green) for recovery
                "label": "Retry Failed",
                "emoji": {"name": "🔄"},
                "custom_id": f"btn_error_retry_{self.req_id}"
            })
        
        # NOTE: Error Button (btn_report_error_) removed from here; it now only appears in the 'done' state.

        return {"type": 1, "components": footer_components} if footer_components else None

    def build_v2_payload(self):
        """Constructs the pure Discord V2 Container Layout"""
        sel_count = len(self.selected_indices)
        if sel_count == 0:
            sel_text = "None"
        elif sel_count == len(self.all_chapters):
            sel_text = f"Ch1-{len(self.all_chapters)} (SR)"
        else:
            idxs = sorted(list(self.selected_indices))
            ranges, s, p = [], idxs[0], idxs[0]
            for i in idxs[1:]:
                if i == p + 1: p = i
                else:
                    ranges.append(f"Ch{s+1}-{p+1}" if s != p else f"Ch{s+1}")
                    s = p = i
            ranges.append(f"Ch{s+1}-{p+1}" if s != p else f"Ch{s+1}")
            sel_text = ", ".join(ranges)
            if len(sel_text) > 35: sel_text = sel_text[:32] + "..."

        footer_text = f"-# R-ID: {self.req_id} | S-ID: {self.series_id}"
        
        # 1. Service Name Header with Logo
        logos = {
            "jumptoon": "<:Jumptoon:1478367963928068168>",
            "piccoma": "<:Piccoma:1478368704164134912>",
            "mecha": "<:Mechacomic:1478369141957333083>"
        }
        logo = logos.get(self.service_type, "")
        platform_display = self.service_type.capitalize()
        if self.service_type == "mecha": platform_display = "Mecha Comic"
        
        # 🔵 SHARED UI COMPONENTS
        service_header = {"type": 10, "content": f"-# {logo} Requested from **[{platform_display}]({self.url})**" }
        divider = {"type": 14, "divider": True, "spacing": 1}
        
        poster_component = None
        if self.image_url:
            poster_component = {
                "type": 12, # Media Gallery (Hero Style)
                "items": [{
                    "media": {
                        "url": self.image_url,
                        "width": 1024,
                        "height": 1024
                    }
                }]
            }

        title_components = [
            {"type": 10, "content": f"# {self.title}"},
            {"type": 10, "content": f"-# **{self.original_title}**"}
        ]

        # 🟢 S-GRADE: Unified collection and sorting of all result items
        all_sorted_items = []
        if self.active_tasks:
            for t in self.active_tasks:
                try: sk = float(t.chapter_str)
                except: sk = 9999.0
                all_sorted_items.append({"sk": sk, "ch": t.chapter_str, "task": t, "type": "active"})
        
        if hasattr(self, 'existing_links') and self.existing_links:
            for ch_str, info in self.existing_links.items():
                if any(x["ch"] == ch_str for x in all_sorted_items): continue
                try: sk = float(ch_str)
                except: sk = 9999.0
                all_sorted_items.append({"sk": sk, "ch": ch_str, "info": info, "type": "existing"})
        
        all_sorted_items.sort(key=lambda x: x["sk"])

        # 🟢 FINAL DESIGN FOR DONE STATE (V3)
        if self.phases.get("download") == "done":
            inner_components = [service_header, divider]
            if poster_component: inner_components.append(poster_component)
            
            # Consolidate title block
            inner_components.append({"type": 10, "content": f"# {self.title}\n-# **{self.original_title}**"})
            inner_components.append(divider)
            
            # 🟢 S-GRADE: Interactive Results Pagination
            per_page = 10
            total_items = len(all_sorted_items)
            total_pages = math.ceil(total_items / per_page)
            
            # Clamp page to valid range (since the same self.page is used for selection)
            results_page = min(self.page, total_pages)
            if results_page < 1: results_page = 1
            
            start_idx = (results_page - 1) * per_page
            visible_items = all_sorted_items[start_idx : start_idx + per_page]

            for item in visible_items:
                link: Optional[str] = None
                if item["type"] == "active":
                    task: ChapterTask = item["task"] # type: ignore
                    ch_str = task.chapter_str
                    link: Optional[str] = task.share_link
                else:
                    ch_str = item["ch"]
                    info: dict = item["info"]
                    link: Optional[str] = info['link'] if isinstance(info, dict) else info

                if link:
                    inner_components.append({
                        "type": 9, # Section
                        "components": [{"type": 10, "content": f"> **{ch_str}**"}],
                        "accessory": {
                            "type": 2, "style": 5,
                            "label": "Drive",
                            "emoji": {"id": "1482676886680113172", "name": "drive"},
                            "url": link
                        }
                    })

            # Add results pagination dropdown if needed
            if total_items > per_page:
                options = []
                for p in range(1, total_pages + 1):
                    opt = {"label": f"Results Page {p}", "value": str(p), "emoji": {"name": "📄"}}
                    if p == results_page:
                        opt["description"] = "(Current Page)"
                        opt["default"] = True
                    options.append(opt)
                
                inner_components.append({
                    "type": 1,
                    "components": [{
                        "type": 3, "custom_id": f"page_select_{self.req_id}", "options": options
                    }]
                })
            

            # Fallback ONLY if both are empty (Safeguard)
            if not all_sorted_items:
                idxs = sorted(list(self.selected_indices))
                chapter_names = [self.all_chapters[i].get('notation', f"Ch.{i+1}") for i in idxs]
                chapter_names_str = "\n".join([f"> **{n}**" for n in chapter_names]) # 🟢 S-GRADE: Professional notation
                
                # 🟢 S-GRADE: Attempt to resolve series root if specific link is missing
                fallback_link = f"https://drive.google.com/drive/folders/{self.series_id_key}" if hasattr(self, 'series_id_key') and self.series_id_key else "https://drive.google.com"
                
                inner_components.append({
                    "type": 9, # Section
                    "components": [{"type": 10, "content": chapter_names_str}],
                    "accessory": {
                        "type": 2, "style": 5, 
                        "label": "Visit Series", 
                        "emoji": {"id": "1482676886680113172", "name": "drive"},
                        "url": fallback_link
                    }
                })

            inner_components.append(divider)


            has_failures = any(t.status == TaskStatus.FAILED for t in self.active_tasks)
            
            # --- Results Footer Section ---
            # 1. Retry Action Row (Only if failures exist)
            if has_failures:
                inner_components.append({
                    "type": 1,
                    "components": [{
                        "type": 2, "style": 3, # Success (Green) for recovery
                        "label": "Retry Failed",
                        "emoji": {"name": "🔄"},
                        "custom_id": f"btn_error_retry_{self.req_id}"
                    }]
                })

            # 2. Combined ID Footer + Report Bug Button (Premium Section Layout)
            inner_components.append({
                "type": 9, # Section
                "components": [{"type": 10, "content": footer_text}],
                "accessory": {
                    "type": 2, 
                    "style": 4 if has_failures else 2, # Red if failure, Grey if success/idle
                    "emoji": {"id": "1480954865516548126", "name": "Error_Chapter"},
                    "custom_id": f"btn_report_error_{self.req_id}"
                }
            })
            
            return [{
                "type": 17,
                "accent_color": 0x2ecc71,
                "components": inner_components
            }]

        # 🔵 STANDARD DESIGN (Processing or Selection)
        
        selection_text = f"**Selected:** {sel_count} ({sel_text})"
        
        desc = ""
        if not self.processing_mode:
            desc += "### Chapter List\n"
            start_idx = (self.page - 1) * self.per_page
            display_chapters = self.all_chapters[start_idx : start_idx + self.per_page]
            
            # 🟢 S-GRADE: Calculate semantic indices (handling hiatuses)
            # We must calculate from the beginning of all_chapters to keep main_idx consistent
            main_idx = 0
            sub_idx = 0
            for i, ch in enumerate(self.all_chapters):
                is_hiatus = any(x in (ch.get('notation', '') + ch.get('title', '')) for x in ["休載", "Hiatus", "Break"])
                if is_hiatus:
                    sub_idx += 1
                    ch['_display_idx'] = f"{main_idx}.{sub_idx}"
                    ch['_main_idx'] = main_idx # Still belongs to the previous main chapter's range
                else:
                    main_idx += 1
                    sub_idx = 0
                    ch['_display_idx'] = str(main_idx)
                    ch['_main_idx'] = main_idx

            for i, ch in enumerate(display_chapters):
                real_idx = start_idx + i
                sel = ""
                if real_idx in self.selected_indices:
                    sel = "🗸"
                elif ch.get('is_new'):
                    sel = "<a:New:1482422261104382033>"
                else:
                    sel = ch.get('_display_idx', str(real_idx + 1))
                    
                notation = ch.get('notation', f"第{real_idx+1}話")
                title_val = ch.get('title_only') or ch.get('title', '').strip()
                
                # If title field is actually the combined one (e.g. from a background scan or older data)
                if title_val.startswith(notation):
                    title_val = title_val[len(notation):].strip(" -")
                
                title_sep = f" - {title_val}" if title_val else ""
                
                # 🟢 S-GRADE: Only use backticks for numbers, not for the "New" emoji or checkmark
                idx_text = f"[{sel}]"
                if isinstance(sel, str) and (":" in sel or "🗸" in sel):
                    # It's an emoji/status, don't wrap in code block
                    clean_line = f"{idx_text} **{notation}**{title_sep}"
                else:
                    # It's a number (or sub-index), use code block for clean alignment
                    clean_line = f"`{idx_text}` **{notation}**{title_sep}"

                if len(clean_line) > 100: clean_line = clean_line[:97] + "..."
                desc += clean_line + "\n"

            # 🟢 BACKGROUND SCAN INDICATOR
            if getattr(self, '_bg_scanning', False):
                missing = self.total_chapters - len(self.all_chapters)
                if missing > 0 and self.page == 1:
                    desc += f"\n{ICONS['load']} *Loading {missing} more chapters...*\n"
            
            start_idx = (self.page - 1) * self.per_page

        footer_text = f"-# R-ID: {self.req_id} | S-ID: {self.series_id}"
        if self.processing_mode:
            inner_components = [service_header, divider]
            if poster_component: inner_components.append(poster_component)
            
            # Combine Titles and Metadata into a single block
            titles_text = f"# {self.title}\n-# {self.original_title}"
            
            inner_components.append({"type": 10, "content": titles_text})
            inner_components.append(divider)
            
            # --- Direct Per-Chapter Progress ---
            consolidated_lines = []
            
            # Determine global phase status if individual tasks haven't fully taken over
            global_phase = None
            if self.phases["analyze"] != "done": global_phase = "Analyzing"
            
            if global_phase and not self.active_tasks:
                # Early state: Scraper/Unlocker is working on the series as a whole
                inner_components.append({"type": 10, "content": f"{global_phase} Series..."})
            else:
                # 🟢 SIMPLIFIED DESIGN: Vertical text-based status report
                for item in all_sorted_items:
                    link = None
                    if item["type"] == "active":
                        task: ChapterTask = item["task"] # type: ignore
                        if task.status == TaskStatus.FAILED: continue
                        
                        if task.status == TaskStatus.COMPLETED:
                            link = task.share_link
                            status_line = f"> **{task.chapter_str}**"
                        else:
                            # Status text assembly
                            # 🟡 S-GRADE: Show "Analyzing..." only if the task is STILL queued and metadata isn't ready
                            if global_phase and task.status == TaskStatus.QUEUED:
                                status_text = f"{global_phase}..."
                            else:
                                # Show the specific task status (Downloading, Uploading, etc.)
                                status_text = f"{task.status.value}"
                                if task.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                                    status_text += "..."
                            
                            status_line = f"> {task.chapter_str}: {status_text}"
                    else:
                        ch_str = item["ch"]
                        info: dict = item["info"]
                        link = info['link'] if isinstance(info, dict) else info
                        status_line = f"> **{ch_str}**"

                    if link:
                        # Flush consolidated lines first
                        if consolidated_lines:
                            inner_components.append({"type": 10, "content": "\n".join(consolidated_lines)})
                            consolidated_lines = []
                        
                        # Chapters with links get their own Section/Button
                        if len(inner_components) < 18:
                            inner_components.append({
                                "type": 9, # Section
                                "components": [{"type": 10, "content": status_line}],
                                "accessory": {
                                    "type": 2, "style": 5,
                                    "label": "Visit Drive" if item["type"] == "existing" else "Drive",
                                    "emoji": {"id": "1482676886680113172", "name": "drive"},
                                    "url": link
                                }
                            })
                        else:
                            consolidated_lines.append(status_line + " [Link Ready]")
                    else:
                        consolidated_lines.append(status_line)
            
            if consolidated_lines:
                inner_components.append({"type": 10, "content": "\n".join(consolidated_lines)})

            if self.phases["download"] == "done":
                inner_components.append({"type": 10, "content": f"{ICONS['tick']} Download Completed."})

            inner_components.append(divider)
            inner_components.append({ "type": 10, "content": footer_text })
            
            footer = self._get_footer_action_row()
            if footer: inner_components.append(footer)
        else:
            header_text = f"## {self.title}"
            if self.original_title and self.original_title != self.title:
                header_text += f"\n-# {self.original_title}"
            header_text += f"\n**Total Ch:** {self.total_chapters}"
            
            inner_components = []
            if self.image_url:
                inner_components.append({
                    "type": 9, # Section
                    "components": [{"type": 10, "content": header_text}],
                    "accessory": {"type": 11, "media": {"url": self.image_url}}
                })
            else:
                inner_components.append({"type": 10, "content": header_text})

            # Safety: guarantee desc is non-empty and within Discord's 4000-char limit
            if not desc.strip() or desc.strip() == "### Chapter List":
                desc = "### Chapter List\n-# *No chapters loaded yet. Background scan in progress...*"
            if len(desc) > 3900:
                desc = desc[:3900] + "\n-# *...truncated*"

            inner_components.append({"type": 14, "divider": True, "spacing": 1})
            inner_components.append({"type": 10, "content": desc})
            inner_components.append({"type": 10, "content": selection_text})
            inner_components.append({"type": 14, "divider": True, "spacing": 1})
            
            options = []
            s_page = max(1, self.page - 12)
            e_page = min(max(self.max_page, 1), s_page + 24)
            for p in range(s_page, e_page + 1):
                opt = {"label": f"Page {p}", "value": str(p), "emoji": {"name": "📄"}}
                if p == self.page:
                    opt["description"] = "(Current Page)"
                    opt["emoji"] = {"name": "🐜"}
                    opt["default"] = True
                options.append(opt)
            
            # Safety: never emit an empty-options select, and only show if > 1 page
            if len(options) > 1:
                inner_components.append({
                    "type": 1, 
                    "components": [{
                        "type": 3, "custom_id": f"page_select_{self.req_id}", "options": options
                    }]
                })

            action_buttons = [{"type": 2, "style": 1, "label": "Select Chapters", "custom_id": f"btn_open_menu_{self.req_id}"}]
            if len(self.selected_indices) > 0:
                action_buttons.append({"type": 2, "style": 3, "label": "Start", "custom_id": f"btn_start_{self.req_id}"})
            
            action_buttons.append({"type": 2, "style": 2, "label": "Cancel", "custom_id": f"btn_cancel_{self.req_id}"})

            inner_components.append({"type": 1, "components": action_buttons})
            inner_components.append({"type": 14, "divider": True, "spacing": 1})
            inner_components.append({ "type": 10, "content": footer_text })
            
            footer = self._get_footer_action_row()
            if footer: inner_components.append(footer)

        return [{
            "type": 17,
            "accent_color": self.color,
            "components": inner_components
        }]

    async def update_view(self, interaction: discord.Interaction = None):
        """Pushes raw V2 JSON natively via HTTP"""
        self.last_interaction_time = time.time()
        
        # 🟢 MANDATORY: For V2 Components (Flag 32768), the 'content' key MUST be omitted entirely.
        payload_data = {
            "flags": 32768, 
            "components": self.build_v2_payload()
        }
        # Final safety: remove ANY top-level content field
        payload_data.pop("content", None)
        
        try:
            if interaction:
                if interaction.response.is_done():
                    # 🟢 ALREADY DEFERRED: Use Webhook PATCH instead of raw callback
                    route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                    await self.bot.http.request(route, json=payload_data)
                else:
                    # 🟢 INITIAL RESPONSE: Use UPDATE_MESSAGE (Type 7)
                    payload = {"type": 7, "data": payload_data} 
                    route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                    try:
                        await self.bot.http.request(route, json=payload)
                    except discord.HTTPException as e:
                        if e.code == 40060:
                            # Fallback if somehow it became "done" between the check and the request
                            route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{interaction.token}/messages/@original')
                            await self.bot.http.request(route, json=payload_data)
                        else:
                            raise e
            else:
                # ─── Background update — no live interaction token ────────────────
                # 1. PRIMARY: Channel message PATCH (never expires)
                if getattr(self, "message_id", None) and getattr(self, "channel_id", None):
                    route = discord.http.Route(
                        'PATCH', 
                        f'/channels/{self.channel_id}/messages/{self.message_id}'
                    )
                    await self.bot.http.request(route, json=payload_data)
                    return

                # 2. FALLBACK: Webhook token (expires in 15 min)
                if not self.interaction: return
                try:
                    # Primary Route: Interaction Webhook (Fast, but expires in 15 mins)
                    route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{self.interaction.token}/messages/@original')
                    await self.bot.http.request(route, json=payload_data)
                except discord.HTTPException as e:
                    # 🟢 Error 50027 (Invalid Token), 10015 (Unknown Webhook), 10062 (Unknown Interaction), or 10008 (Unknown Message)
                    if e.code in [50027, 10015, 10062, 10008] and getattr(self.interaction, 'message', None):
                        # Fallback Route: Standard Channel Message Edit (Never expires!)
                        route = discord.http.Route(
                            'PATCH', 
                            f'/channels/{self.interaction.channel_id}/messages/{self.interaction.message.id}'
                        )
                        # Discord allows modifying Webhook messages with Bot token if you specify the channel/message
                        try:
                            payload_data.pop("content", None)
                            await self.bot.http.request(route, json=payload_data)
                        except: pass
                    else:
                        raise e
        except Exception as e:
            logger.error(f"[{self.req_id}] V2 UI Update Failed: {e}", exc_info=True)

    def trigger_refresh(self):
        from app.services.ui_manager import UIManager
        UIManager().request_update(self.req_id, self)

    async def _perform_full_scan(self):
        """
        Fetches all missing chapter metadata in the background.

        For Jumptoon: single gather-all call (provider handles parallelism internally).
        For Mecha: sequential incremental scan (preserved from original behavior).
        """
        _did_work = False
        try:
            logger.info(f"[{self.req_id}] 📡 Starting background scan for {self.service_type}...")
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(self.url)

            pg_size = 30 if self.service_type == "jumptoon" else 10
            total_pages = math.ceil(self.total_chapters / pg_size) if self.total_chapters else 1

            seen_ids = {ch['id'] for ch in self.all_chapters}

            # ─── JUMPTOON: single parallel background call ───────────────────────
            # Provider auto-skips page 1 + last page (both already in all_chapters
            # from the tail-first foreground fetch). Internal semaphore keeps proxy
            # happy. One UI refresh when all middle pages land.
            if self.service_type == "jumptoon":
                if total_pages <= 2:
                    # No middle pages to fetch (series fits in 2 pages or less)
                    logger.debug(f"[{self.req_id}] Jumptoon series has ≤2 pages; no background scan needed")
                    return

                logger.debug(f"[{self.req_id}] 📡 Jumptoon parallel background scan: "
                             f"{total_pages - 2} middle pages")

                # scraper.fetch_more_chapters auto-skips [1, total_pages] when
                # skip_pages=None, so we pass None explicitly to get the default.
                new_chaps = await scraper.fetch_more_chapters(
                    self.url, total_pages, seen_ids, skip_pages=None
                )

                if new_chaps:
                    _did_work = True
                    self.all_chapters.extend(new_chaps)
                    self.all_chapters.sort(key=self._jumptoon_sort_key)

                    logger.info(f"[{self.req_id}] ✅ Jumptoon background scan complete: "
                                f"{len(self.all_chapters)} chapters mapped")
                    self.trigger_refresh()
                    self._latest_ui_update = time.time()
                else:
                    logger.warning(f"[{self.req_id}] ⚠️ Jumptoon background scan returned NO new chapters.")
                return

            # ─── MECHA (and any future provider): sequential incremental scan ────
            # Preserved from original behavior — Mecha's fetch_more_chapters is
            # still page-by-page sequential, and the incremental UI updates
            # provide good UX for the small number of pages it typically has.
            for p in range(1, total_pages + 1):
                # Page 1 is already fast-fetched; skip if we have enough
                if p == 1 and len(self.all_chapters) >= pg_size:
                    continue

                logger.debug(f"[{self.req_id}] 📡 Background fetching {self.service_type} page {p}...")
                new_chaps = await scraper.fetch_more_chapters(
                    self.url, p, seen_ids,
                    skip_pages=[i for i in range(1, p)]
                )

                if new_chaps:
                    _did_work = True
                    self.all_chapters.extend(new_chaps)

                    # Generic numeric sort (works for Mecha; Jumptoon uses its own above)
                    def extract_num(ch):
                        m = re.search(r'\d+', ch.get('notation', ''))
                        if m:
                            return int(m.group())
                        raw_id = ch.get('id')
                        return int(raw_id) if raw_id and str(raw_id).isdigit() else 0

                    self.all_chapters.sort(key=extract_num)

                    # Throttled UI refresh (max once per 5s)
                    now = time.time()
                    if now - self._latest_ui_update > 5:
                        logger.info(f"[{self.req_id}] 🔄 Throttled UI update: "
                                    f"{len(self.all_chapters)} chapters mapped.")
                        self.trigger_refresh()
                        self._latest_ui_update = now

            # Final update once fully complete
            _did_work = True
            logger.info(f"[{self.req_id}] ✅ Background scan complete. "
                        f"Total mapped: {len(self.all_chapters)}")
            self.trigger_refresh()
            self._latest_ui_update = time.time()

        except Exception as e:
            logger.error(f"[{self.req_id}] ❌ Background full scan failed: {e}")
            _did_work = False
        finally:
            self._bg_scanning = False
            if _did_work:
                self.trigger_refresh()
                self._latest_ui_update = time.time()

    @staticmethod
    def _jumptoon_sort_key(ch):
        """
        Jumptoon-specific sort matching JumptoonProvider._extract_sort_key.
        Kept in sync deliberately — Jumptoon has hiatus chapters (45.1, 45.2, etc.)
        where generic regex extraction would collide.
        """
        # 1. Primary: numeric 'number' field
        num = ch.get('number')
        if num and str(num).isdigit():
            return int(num)
        # 2. Secondary: regex from notation
        import re as _re
        not_match = _re.search(r'(\d+)', ch.get('notation', ''))
        if not_match:
            return int(not_match.group(1))
        # 3. Tertiary: numeric ID fallback
        raw_id = ch.get('id')
        if raw_id and str(raw_id).isdigit():
            return int(raw_id)
        return 0

    async def monitor_tasks(self):
        last_state_snapshot = None
        while self.phases["download"] != "done":
            if self.active_tasks:

                if all(t.status in [TaskStatus.COMPLETED, TaskStatus.FAILED] for t in self.active_tasks): 
                    self.phases["download"] = "done"
                    # 🟢 RESET TIMER: Give the user 30 full minutes to click the link/inspect results
                    self.last_interaction_time = time.time()
            else:
                # Fallback for when everything was already existing and no tasks were queued
                if self.phases["analyze"] == "done" and self.phases["purchase"] == "done":
                    self.phases["download"] = "done"
                    self.last_interaction_time = time.time()

            # Only request UI refresh when state changes, to avoid noisy idle queue loops.
            state_snapshot = (
                self.phases.get("analyze"),
                self.phases.get("purchase"),
                self.phases.get("download"),
                tuple(
                    (
                        t.chapter_str,
                        t.status.value,
                        bool(getattr(t, "share_link", None))
                    )
                    for t in self.active_tasks
                ),
                getattr(self, "sub_status", "")
            )
            if state_snapshot != last_state_snapshot:
                self.trigger_refresh()
                last_state_snapshot = state_snapshot

            if self.phases["download"] == "done":
                # 🟢 SEND NOTIFICATION: Always ping when done
                if self.interaction:
                    try:
                        # 🟢 ROBUST PING: Try multiple ways to reach the user/channel
                        channel = self.bot.get_channel(self.interaction.channel_id)
                        if not channel:
                            try:
                                channel = await self.bot.fetch_channel(self.interaction.channel_id)
                            except:
                                pass
                        
                        # Fallback: If we still don't have a channel, try to reach the user directly (DMs)
                        if not channel:
                            channel = self.interaction.user

                        if channel:
                            # 🟢 ROBUST PING: Use SettingsService for mentions
                            from app.services.settings_service import SettingsService
                            settings = SettingsService()
                            targets = await settings.get_notify_targets(int(self.user))
                            mentions = SettingsService.format_mentions(targets) or f"<@{self.user}>"
                            
                            ping_msg = await channel.send(content=mentions)
                            
                            async def delete_ping(msg):
                                await asyncio.sleep(15)
                                try: await msg.delete()
                                except: pass
                            
                            asyncio.create_task(delete_ping(ping_msg))
                    except Exception as e:
                        logger.error(f"Failed to send ping: {e}")
                break
            await asyncio.sleep(2)
