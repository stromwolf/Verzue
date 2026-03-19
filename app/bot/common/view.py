import discord, asyncio, math, logging, time, re
from app.models.chapter import ChapterTask, TaskStatus

logger = logging.getLogger("Dashboard")
ICONS = {"load": "<a:waiting:1482424619746201601>", "tick": "[DONE]", "wait": "[   ]"}
COLORS = {"mecha": 0xe67e22, "smartoon": 0x2ecc71, "jumptoon": 0x9b59b6, "piccoma": 0xffd600, "kuaikan": 0xf1c40f}

class UniversalDashboard:
    active_views = {}  # Global router for raw V2 interactions

    def __init__(self, bot, ctx_data, service_type):
        self.bot = bot
        self.url, self.title, self.all_chapters = ctx_data['url'], ctx_data['title'], ctx_data['chapters']
        self.original_title = ctx_data.get('original_title', self.title)
        # Fallback to length of chapters if total_chapters isn't provided (e.g. for older scraper implementations)
        self.total_chapters = ctx_data.get('total_chapters', len(self.all_chapters))
        self.image_url, self.req_id, self.series_id, self.user = ctx_data['image_url'], ctx_data['req_id'], ctx_data['series_id'], ctx_data['user']
        self.service_type, self.color = service_type, COLORS.get(service_type, 0x2b2d31)
        
        if self.service_type == "mecha":
            self.bot.task_queue.browser_service.inc_session()

        self.page, self.per_page = 1, 10
        self.max_page = math.ceil(self.total_chapters / self.per_page) if self.total_chapters else 1
        self.selected_indices, self.active_tasks = set(), []
        
        self.phases = {"analyze": "waiting", "purchase": "waiting", "download": "waiting"}
        self.final_link, self.interaction, self.sub_status, self.processing_mode, self._last_hash = None, None, None, False, 0
        self.retry_active = False
        
        # 🟢 UI Toggle for Selection Mode Menu
        self.show_selection_menu = False
        
        # Session Timeout Tracking (15 mins)
        self.last_interaction_time = time.time()
        self.creation_time = time.time()
        self.timeout_task = asyncio.create_task(self._auto_timeout_loop())
        
        # Register to global router
        UniversalDashboard.active_views[self.req_id] = self

        # 🟢 BACKGROUND SCAN: Fetch remaining chapters for Mecha (Jumptoon now handles 100% upfront)
        self._full_scan_task = None
        if self.service_type in ["mecha"] and self.total_chapters > len(self.all_chapters):
            self._full_scan_task = asyncio.create_task(self._perform_full_scan())

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
                if self.service_type == "mecha":
                    try: self.bot.task_queue.browser_service.dec_session()
                    except: pass
                UniversalDashboard.active_views.pop(self.req_id, None)
                logger.info(f"[{self.req_id}] ⏳ Terminated inactive session (Auto-Cleanup).")
                # Attempt to edit the message to show it expired using the fallback route
                if getattr(self, 'interaction', None):
                    try:
                        exp_payload = {
                            "flags": 32768, 
                            "components": [{
                                "type": 17, "components": [{"type": 10, "content": "<a:error:1482426908699267174> **Session Expired:**\nThis dashboard has closed due to 15 minutes of inactivity."}]
                            }]
                        }
                        route = discord.http.Route('PATCH', f'/channels/{self.interaction.channel_id}/messages/{self.interaction.message.id}')
                        await self.bot.http.request(route, json=exp_payload)
                    except: pass
                break

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

        # 🟢 FINAL DESIGN FOR DONE STATE (V3)
        if self.phases.get("download") == "done":
            inner_components = [service_header, divider]
            if poster_component: inner_components.append(poster_component)
            
            # Consolidate title block
            inner_components.append({"type": 10, "content": f"# {self.title}\n-# **{self.original_title}**"})
            inner_components.append(divider)
            
            # 4. Chapters Section with Individual Drive Buttons
            if self.active_tasks:
                for task in self.active_tasks:
                    # Chapter notation and "Visit Drive" button
                    chapter_content = f"**{task.chapter_str}**"
                    
                    # Accessory Button
                    accessory = {
                        "type": 2, "style": 5, 
                        "label": "Visit Drive", 
                        "emoji": {"id": "1482676886680113172", "name": "drive"},
                        "url": task.share_link or self.final_link or "https://google.com"
                    }
                    
                    inner_components.append({
                        "type": 9, # Section
                        "components": [{"type": 10, "content": chapter_content}],
                        "accessory": accessory
                    })
            else:
                # Fallback for old sessions or when no active_tasks were created
                idxs = sorted(list(self.selected_indices))
                chapter_names = [self.all_chapters[i].get('notation', f"Ch.{i+1}") for i in idxs]
                chapter_names_str = ", ".join(chapter_names)
                
                inner_components.append({
                    "type": 9, # Section
                    "components": [{"type": 10, "content": f"**{chapter_names_str}**"}],
                    "accessory": {
                        "type": 2, "style": 5, 
                        "label": "Visit Drive", 
                        "emoji": {"id": "1482676886680113172", "name": "drive"},
                        "url": self.final_link or "https://google.com"
                    }
                })

            inner_components.append(divider)

            # 6. ID Footer Section with Error Button Accessory
            inner_components.append({
                "type": 9, # Section
                "components": [{"type": 10, "content": footer_text}],
                "accessory": {
                    "type": 2, "style": 2, # Secondary (Dull)
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
        if self.processing_mode:
            if self.phases["analyze"] == "done": desc += "Analyzed.\n"
            else:
                icon = ICONS["load"] if self.phases["analyze"] == "loading" else ICONS["wait"]
                desc += f"{icon} Analyzing...\n"
            
            if self.phases["analyze"] == "done":
                if self.phases["purchase"] == "done": desc += "Purchased.\n"
                else:
                    icon = ICONS["load"] if self.phases["purchase"] == "loading" else ICONS["wait"]
                    desc += f"{icon} Purchasing...\n"
                    unlocker = self.bot.task_queue.unlocker
                    active_info = [f"-> `Ch.{stats['task'].id:02d}`: {stats.get('progress', 0)}% | {stats['task'].status.value}" for stats in unlocker.worker_stats.values() if stats.get("view") == self and stats.get("task")]
                    if active_info: desc += "\n".join(active_info) + "\n"
            
            if self.phases["purchase"] == "done":
                if self.phases["download"] == "loading":
                    desc += f"{ICONS['load']} Downloading chapters...\n"
                    comp = sum(1 for t in self.active_tasks if t.status == TaskStatus.COMPLETED)
                    if comp: desc += f"-> **{comp}** chapters completed.\n"
                    for t in self.active_tasks:
                        if t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                            desc += f"-> `{t.chapter_str}`: {ICONS['load']} {t.status.value}...\n"
                            break
                elif self.phases["download"] == "done":
                    desc += "Download Completed.\n"
        else:
            desc += "### Chapter List\n"
            start_idx = (self.page - 1) * self.per_page
            display_chapters = self.all_chapters[start_idx : start_idx + self.per_page]
            
            for i, ch in enumerate(display_chapters):
                real_idx = start_idx + i
                if real_idx in self.selected_indices:
                    sel = "🗸"
                elif ch.get('is_new'):
                    sel = "<a:New:1482422261104382033>"
                else:
                    sel = str(real_idx + 1)
                    
                notation = ch.get('notation', f"第{real_idx+1}話")
                title_val = ch.get('title', '').strip()
                title_sep = f" - {title_val}" if title_val else ""
                clean_line = f"`[{sel}]` {notation}{title_sep}"
                if len(clean_line) > 100: clean_line = clean_line[:97] + "..."
                desc += clean_line + "\n"
            
            start_idx = (self.page - 1) * self.per_page

        footer_text = f"-# R-ID: {self.req_id} | S-ID: {self.series_id}"
        if self.processing_mode:
            inner_components = [service_header, divider]
            if poster_component: inner_components.append(poster_component)
            
            # Combine Titles and Metadata into a single block
            titles_text = f"# {self.title}\n-# **{self.original_title}**\n-# **Total Pages:** {self.max_page} | **Total Chapters:** {self.total_chapters}"
            inner_components.append({"type": 10, "content": titles_text})
            inner_components.append(divider)
            
            # --- Direct Per-Chapter Progress ---
            consolidated_lines = []
            
            # Determine global phase status if individual tasks haven't fully taken over
            global_phase = None
            if self.phases["analyze"] != "done": global_phase = "Analyzing"
            elif self.phases["purchase"] != "done": global_phase = "Purchasing"
            
            if global_phase and not self.active_tasks:
                # Early state: Scraper/Unlocker is working on the series as a whole
                inner_components.append({"type": 10, "content": f"{ICONS['load']} {global_phase} Series..."})
            else:
                # Active Chapter State
                for task in self.active_tasks:
                    if task.status == TaskStatus.FAILED: continue
                    
                    # Status text assembly
                    if global_phase and task.status == TaskStatus.QUEUED:
                        status_text = f"{global_phase}..."
                        icon = ICONS["load"]
                    else:
                        status_text = f"{task.status.value}"
                        if task.status != TaskStatus.COMPLETED: status_text += "..."
                        icon = ICONS["tick"] if task.status == TaskStatus.COMPLETED else ICONS["load"]
                    
                    status_line = f"{task.chapter_str}: {icon} {status_text}"
                    
                    if task.share_link:
                        # Flush consolidated lines first
                        if consolidated_lines:
                            inner_components.append({"type": 10, "content": "\n".join(consolidated_lines)})
                            consolidated_lines = []
                        
                        # Chapters with links get their own Section/Button (if budget allows)
                        if len(inner_components) < 18:
                            inner_components.append({
                                "type": 9, # Section
                                "components": [{"type": 10, "content": status_line}],
                                "accessory": {
                                    "type": 2, "style": 5,
                                    "label": "Drive",
                                    "emoji": {"id": "1482676886680113172", "name": "drive"},
                                    "url": task.share_link
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
            inner_components.append({"type": 10, "content": footer_text})
        else:
            header_text = f"## {self.title}"
            if self.original_title and self.original_title != self.title:
                header_text += f"\n{self.original_title}"
            header_text += f"\n**Total Pages:** {self.max_page} | **Total Chapters:** {self.total_chapters}"

            inner_components = []
            if self.image_url:
                inner_components.append({
                    "type": 9, # Section
                    "components": [{"type": 10, "content": header_text}],
                    "accessory": {"type": 11, "media": {"url": self.image_url}}
                })
            else:
                inner_components.append({"type": 10, "content": header_text})
            
            inner_components.append({"type": 14, "spacing": 1})
            inner_components.append({"type": 10, "content": desc})
            inner_components.append({"type": 10, "content": selection_text})
            inner_components.append({"type": 14, "spacing": 1})
            
            options = []
            s_page = max(1, self.page - 12)
            e_page = min(self.max_page, s_page + 24)
            for p in range(s_page, e_page + 1):
                opt = {"label": f"Page {p}", "value": str(p), "emoji": {"name": "📄"}}
                if p == self.page:
                    opt["description"] = "(Current Page)"
                    opt["emoji"] = {"name": "🐜"}
                    opt["default"] = True
                options.append(opt)
            
            inner_components.append({
                "type": 1, 
                "components": [{
                    "type": 3, "custom_id": f"page_select_{self.req_id}", "options": options
                }]
            })

            action_buttons = [{"type": 2, "style": 1, "label": "Select Chapters", "custom_id": f"btn_open_menu_{self.req_id}"}]
            if len(self.selected_indices) > 0:
                action_buttons.append({"type": 2, "style": 3, "label": "Start", "custom_id": f"btn_start_{self.req_id}"})

            inner_components.append({"type": 1, "components": action_buttons})
            inner_components.append({"type": 14, "spacing": 1})
            
            inner_components.append({
                "type": 9,
                "components": [{"type": 10, "content": footer_text}],
                "accessory": {
                    "type": 2, "style": 1, 
                    "emoji": {"id": "1482405757394751619", "name": "Home"},
                    "custom_id": f"btn_home_{self.req_id}"
                }
            })

        return [{
            "type": 17,
            "accent_color": self.color,
            "components": inner_components
        }]

    async def update_view(self, interaction: discord.Interaction = None):
        """Pushes raw V2 JSON natively via HTTP"""
        self.last_interaction_time = time.time()
        
        # 🟢 MANDATORY: For V2 Components (Flag 32768), the 'content' key MUST be omitted entirely.
        # Even 'content': "" will trigger a 400 Bad Request error from Discord.
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
                    payload = {"type": 7, "data": payload_data} # UPDATE_MESSAGE
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
                if not self.interaction: return
                try:
                    # Primary Route: Interaction Webhook (Fast, but expires in 15 mins)
                    route = discord.http.Route('PATCH', f'/webhooks/{self.bot.user.id}/{self.interaction.token}/messages/@original')
                    await self.bot.http.request(route, json=payload_data)
                except discord.HTTPException as e:
                    # 🟢 Error 50027: Invalid Webhook Token (Triggered after 15 minutes)
                    if e.code == 50027 and getattr(self.interaction, 'message', None):
                        # Fallback Route: Standard Channel Message Edit (Never expires!)
                        route = discord.http.Route(
                            'PATCH', 
                            f'/channels/{self.interaction.channel_id}/messages/{self.interaction.message.id}'
                        )
                        # Discord allows modifying Webhook messages with Bot token if you specify the channel/message
                        try:
                            await self.bot.http.request(route, json=payload_data)
                        except: pass
                    else:
                        raise e
        except Exception as e:
            logger.error(f"V2 UI Update Failed: {e}", exc_info=True)

    def trigger_refresh(self):
        from app.services.ui_manager import UIManager
        UIManager().request_update(self.req_id, self)

    async def _perform_full_scan(self):
        """Fetches all missing chapter metadata in the background."""
        try:
            logger.info(f"[{self.req_id}] 📡 Starting background full scan for {self.service_type}...")
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(self.url)
            
            pg_size = 30 if self.service_type == "jumptoon" else 10
            total_jt_pages = math.ceil(self.total_chapters / pg_size)
            
            # Skip the last page as it was already pre-fetched in get_series_info
            skip_pages = [total_jt_pages] if total_jt_pages > 1 else []
            
            seen_ids = {ch['id'] for ch in self.all_chapters}
            
            # 🟢 S-Grade Async
            new_chaps = await scraper.fetch_more_chapters(self.url, total_jt_pages, seen_ids, skip_pages)
            
            if new_chaps:
                # Add and re-sort
                self.all_chapters.extend(new_chaps)
                
                # Sort numerically by notation/id
                def extract_num(ch):
                    m = re.search(r'\d+', ch.get('notation', ''))
                    return int(m.group()) if m else 0
                
                self.all_chapters.sort(key=lambda x: extract_num(x))
                logger.info(f"[{self.req_id}] ✅ Background scan complete. Total mapped: {len(self.all_chapters)}")
                
                # Update UI to reflect new chapters if user is on a later page
                self.trigger_refresh()
                
        except Exception as e:
            logger.error(f"[{self.req_id}] ❌ Background full scan failed: {e}")

    async def monitor_tasks(self):
        while self.phases["download"] != "done":
            if self.active_tasks:
                # 🟢 DYNAMIC LINK UPDATE: Update final_link for single-chapter tasks as soon as folder is ready
                if len(self.selected_indices) == 1 and not self.final_link:
                    task = self.active_tasks[0]
                    if task.pre_created_folder_id:
                        try:
                            uploader = self.bot.task_queue.uploader
                            self.final_link = await asyncio.to_thread(uploader.get_share_link, task.pre_created_folder_id)
                        except: pass

                if all(t.status in [TaskStatus.COMPLETED, TaskStatus.FAILED] for t in self.active_tasks): 
                    self.phases["download"] = "done"
                    # 🟢 RESET TIMER: Give the user 30 full minutes to click the link/inspect results
                    self.last_interaction_time = time.time()
            else:
                # Fallback for when everything was already existing and no tasks were queued
                if self.phases["analyze"] == "done" and self.phases["purchase"] == "done":
                    self.phases["download"] = "done"
                    self.last_interaction_time = time.time()
            
            self.trigger_refresh()
            if self.phases["download"] == "done":
                # 🟢 SEND NOTIFICATION: Always ping when done
                if self.final_link and self.interaction:
                    try:
                        channel = self.bot.get_channel(self.interaction.channel_id)
                        if channel:
                            # 🟢 SIMPLE PING: Only the user mention (deletes in 30s)
                            ping_msg = await channel.send(content=f"<@{self.interaction.user.id}>")
                            
                            async def delete_ping(msg):
                                await asyncio.sleep(15)
                                try: await msg.delete()
                                except: pass
                            
                            asyncio.create_task(delete_ping(ping_msg))
                    except: pass
                break
            await asyncio.sleep(2)
