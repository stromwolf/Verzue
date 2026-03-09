import discord, asyncio, math, logging, time
from app.models.chapter import ChapterTask, TaskStatus

logger = logging.getLogger("Dashboard")
ICONS = {"load": "🔄", "tick": "✅", "wait": "⬜"}
COLORS = {"mecha": 0xe67e22, "smartoon": 0x2ecc71, "jumptoon": 0x9b59b6, "piccoma": 0xffd600, "kuaikan": 0xf1c40f}

class UniversalDashboard:
    active_views = {}  # Global router for raw V2 interactions

    def __init__(self, bot, ctx_data, service_type):
        self.bot = bot
        self.url, self.title, self.all_chapters = ctx_data['url'], ctx_data['title'], ctx_data['chapters']
        # Fallback to length of chapters if total_chapters isn't provided (e.g. for older scraper implementations)
        self.total_chapters = ctx_data.get('total_chapters', len(self.all_chapters))
        self.image_url, self.req_id, self.series_id, self.user = ctx_data['image_url'], ctx_data['req_id'], ctx_data['series_id'], ctx_data['user']
        self.service_type, self.color = service_type, COLORS.get(service_type, 0x2b2d31)
        
        if self.service_type == "mecha":
            self.bot.task_queue.scraper_registry.browser.inc_session()

        self.page, self.per_page = 1, 10
        self.max_page = math.ceil(self.total_chapters / self.per_page) if self.total_chapters else 1
        self.selected_indices, self.active_tasks = set(), []
        
        self.phases = {"analyze": "waiting", "purchase": "waiting", "download": "waiting"}
        self.final_link, self.interaction, self.sub_status, self.processing_mode, self._last_hash = None, None, None, False, 0
        
        # 🟢 UI Toggle for Selection Mode Menu
        self.show_selection_menu = False
        
        # Session Timeout Tracking (15 mins)
        self.last_interaction_time = time.time()
        self.creation_time = time.time()
        self.timeout_task = asyncio.create_task(self._auto_timeout_loop())
        
        # Register to global router
        UniversalDashboard.active_views[self.req_id] = self

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
                    try: self.bot.task_queue.scraper_registry.browser.dec_session()
                    except: pass
                UniversalDashboard.active_views.pop(self.req_id, None)
                logger.info(f"[{self.req_id}] ⏳ Terminated inactive session (Auto-Cleanup).")
                # Attempt to edit the message to show it expired using the fallback route
                if getattr(self, 'interaction', None):
                    try:
                        exp_payload = {
                            "flags": 32768, 
                            "components": [{
                                "type": 17, "components": [{"type": 10, "content": "❌ **Session Expired:**\nThis dashboard has closed due to 15 minutes of inactivity."}]
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

        header_text = f"## {self.title}\n**Total Pages:** {self.max_page} | **Total Chapters:** {self.total_chapters}"
        
        desc = ""
        if self.processing_mode:
            if self.phases["analyze"] == "done": desc += f"{ICONS['tick']} Analyzed.\n"
            else:
                icon = ICONS["load"] if self.phases["analyze"] == "loading" else ICONS["wait"]
                stat = f"Analyzing... ({self.sub_status})" if self.sub_status else "Analyzing..."
                desc += f"{icon} {stat}\n"
            
            if self.phases["analyze"] == "done":
                if self.phases["purchase"] == "done": desc += f"{ICONS['tick']} Purchased.\n"
                else:
                    icon = ICONS["load"] if self.phases["purchase"] == "loading" else ICONS["wait"]
                    count_str = f" [{getattr(self, 'purchase_count', 0)}]" if getattr(self, 'purchase_count', 0) > 0 else ""
                    desc += f"{icon} Auto-Purchasing{count_str}...\n"
                    unlocker = self.bot.task_queue.scraper_registry.unlocker
                    active_info = [f"-> `Ch.{stats['task'].id:02d}`: {stats.get('progress', 0)}% | {stats['task'].purchase_status}" for stats in unlocker.worker_stats.values() if stats.get("view") == self and stats.get("task")]
                    if active_info: desc += "\n".join(active_info) + "\n"
            
            if self.phases["purchase"] == "done":
                if self.phases["download"] == "loading":
                    desc += f"{ICONS['load']} Processing [{len(self.active_tasks)}] chapters...\n"
                    comp = sum(1 for t in self.active_tasks if t.status == TaskStatus.COMPLETED)
                    if comp: desc += f"-> **{comp}** chapters completed.\n"
                    for t in self.active_tasks:
                        if t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                            desc += f"-> `{t.chapter_str}`: {ICONS['load']} {t.status.value}...\n"
                            break
                elif self.phases["download"] == "done":
                    desc += f"{ICONS['tick']} Download Completed."

            if self.final_link: desc += f"\n\n📂 **Destination:** [Open Google Drive]({self.final_link})"
            desc += f"\n\n**Selected:** {sel_count} ({sel_text})"
        else:
            desc += "### **Chapter List**\n"
            start_idx = (self.page - 1) * self.per_page
            display_chapters = self.all_chapters[start_idx : start_idx + self.per_page]
            
            for i, ch in enumerate(display_chapters):
                real_idx = start_idx + i
                new_tag = "✨ " if ch.get('is_new') else ""
                sel = "✅" if real_idx in self.selected_indices else str(real_idx + 1)
                notation = ch.get('notation', f"第{real_idx+1}話")
                title_val = ch.get('title', '').strip()
                
                # Format as user requested: `index` | notation - title
                title_sep = f" - {title_val}" if title_val else ""
                clean_line = f"{new_tag}`{sel}` | {notation}{title_sep}"
                
                # Truncate line safely if needed (unlikely but good for stability)
                if len(clean_line) > 100: clean_line = clean_line[:97] + "..."
                
                desc += clean_line + "\n"
            
            desc += f"\n**Selected:** {sel_count} ({sel_text})"

        footer_text = f"-# R-ID: {self.req_id} | S-ID: {self.series_id}"

        # 🟢 FIX: Initialize the component list BEFORE we start appending to it!
        inner_components = []
        
        # 1. Header Section
        if self.image_url:
            inner_components.append({
                "type": 9,
                "components": [{"type": 10, "content": header_text}],
                "accessory": {"type": 11, "media": {"url": self.image_url}}
            })
        else:
            inner_components.append({"type": 10, "content": header_text})
        
        inner_components.append({"type": 14, "spacing": 1})
        inner_components.append({"type": 10, "content": desc})
        
        # 2. Interactive Elements
        if not self.processing_mode:
            inner_components.append({"type": 14, "spacing": 1})
            
            # Row 1: String Select (Pages)
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

            # Row 2: Always display action buttons
            inner_components.append({
                "type": 1,
                "components": [
                    {"type": 2, "style": 1, "label": "Select Chapters", "custom_id": f"btn_open_menu_{self.req_id}"},
                    {"type": 2, "style": 3, "label": "Start", "custom_id": f"btn_start_{self.req_id}", "disabled": len(self.selected_indices) == 0}
                ]
            })

        # 3. Footer Section
        inner_components.append({"type": 14, "spacing": 1})
        
        if not self.processing_mode:
            inner_components.append({
                "type": 9,
                "components": [{"type": 10, "content": footer_text}],
                "accessory": {
                    "type": 2, "style": 4, "emoji": {"name": "✖️"}, "custom_id": f"btn_cancel_{self.req_id}"
                }
            })
        else:
            inner_components.append({"type": 10, "content": footer_text})

        return [{
            "type": 17,
            "accent_color": self.color if self.phases.get("download") != "done" else 0x2ecc71,
            "components": inner_components
        }]

    async def update_view(self, interaction: discord.Interaction = None):
        """Pushes raw V2 JSON natively via HTTP"""
        self.last_interaction_time = time.time()
        # 🟢 Double check there is no 'content' key at the end of this line:
        payload_data = {"flags": 32768, "components": self.build_v2_payload()} 
        try:
            if interaction:
                payload = {"type": 7, "data": payload_data} # UPDATE_MESSAGE
                route = discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback')
                await self.bot.http.request(route, json=payload)
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
                # Only post fallback if we likely hit the 15m expiration
                if time.time() - getattr(self, "creation_time", time.time()) > 850 and self.final_link and self.interaction:
                    try:
                        channel = self.bot.get_channel(self.interaction.channel_id)
                        if channel:
                            embed = discord.Embed(
                                title="✅ Download Completed",
                                description=f"The download session for **{self.title}** has finished.\n\n📂 **[Open Google Drive]({self.final_link})**",
                                color=0x2ecc71
                            )
                            await channel.send(content=f"<@{self.interaction.user.id}>", embed=embed)
                    except: pass
                break
            await asyncio.sleep(2)
