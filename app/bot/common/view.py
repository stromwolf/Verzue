import discord, asyncio, math, logging
from discord.ui import View, Button, Modal, TextInput
from app.models.chapter import ChapterTask, TaskStatus

logger = logging.getLogger("Dashboard")
ICONS = {"load": "🔄", "tick": "✅", "wait": "⬜"}
COLORS = {"mecha": 0xe67e22, "smartoon": 0x2ecc71, "jumptoon": 0x9b59b6, "piccoma": 0xffd600, "kuaikan": 0xf1c40f}

class UniversalDashboard(View):
    def __init__(self, bot, ctx_data, service_type):
        super().__init__(timeout=120)
        self.bot, self.url, self.title, self.all_chapters = bot, ctx_data['url'], ctx_data['title'], ctx_data['chapters']
        self.image_url, self.req_id, self.series_id, self.user = ctx_data['image_url'], ctx_data['req_id'], ctx_data['series_id'], ctx_data['user']
        self.service_type, self.color = service_type, COLORS.get(service_type, 0x2b2d31)
        
        if self.service_type == "mecha":
            browser = self.bot.task_queue.scraper_registry.browser
            browser.inc_session()

        self.page, self.per_page = 1, 10
        self.max_page = math.ceil(len(self.all_chapters) / self.per_page) if self.all_chapters else 1
        self.selected_indices, self.active_tasks = set(), []
        
        self.phases = {"analyze": "waiting", "purchase": "waiting", "download": "waiting"}
        self.final_link, self.interaction, self.sub_status, self.processing_mode, self._last_hash = None, None, None, False, 0

    async def on_timeout(self):
        logger.info(f"⏳ Dashboard timed out for R-ID: {self.req_id}")
        if self.service_type == "mecha":
            browser = self.bot.task_queue.scraper_registry.browser
            browser.dec_session()
        if self.interaction:
            try:
                await self.interaction.edit_original_response(content="❌ **Session Expired** (Inactive for 120s)", view=None)
            except: pass

    async def on_error(self, interaction, error, item):
        logger.error(f"Dashboard Error: {error}", exc_info=True)
        if self.service_type == "mecha":
            browser = self.bot.task_queue.scraper_registry.browser
            browser.dec_session()
        try: await interaction.followup.send("❌ A critical error occurred.", ephemeral=True)
        except: pass

    def build_live_embed(self):
        # 1. Dynamic Range Formatter for Selected Chapters
        sel_text = "None"
        if self.selected_indices:
            if len(self.selected_indices) == len(self.all_chapters):
                sel_text = f"1-{len(self.all_chapters)} (SR)"
            else:
                idxs = sorted(list(self.selected_indices))
                ranges, s, p = [], idxs[0], idxs[0]
                for i in idxs[1:]:
                    if i == p + 1: p = i
                    else:
                        ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                        s = p = i
                ranges.append(f"{s+1}-{p+1}" if s != p else f"{s+1}")
                sel_text = ", ".join(ranges)
                if len(sel_text) > 40: sel_text = sel_text[:37] + "..."

        # 2. Setup Base Embed
        color = 0x2ecc71 if self.phases["download"] == "done" else self.color
        embed = discord.Embed(color=color)
        
        # 3. Top Header Section (V2 Markdown)
        desc = f"## {self.title}\n**Total:** {len(self.all_chapters)}\n──────────────────────────\n"
        
        if self.processing_mode:
            # 1. ANALYZE
            if self.phases["analyze"] == "done":
                desc += f"{ICONS['tick']} Analyzed.\n"
            else:
                icon = ICONS["load"] if self.phases["analyze"] == "loading" else ICONS["wait"]
                stat = f"Analyzing... ({self.sub_status})" if self.sub_status else "Analyzing..."
                desc += f"{icon} {stat}\n"
            
            # 2. PURCHASE
            if self.phases["analyze"] == "done":
                if self.phases["purchase"] == "done":
                    desc += f"{ICONS['tick']} Purchased.\n"
                else:
                    icon = ICONS["load"] if self.phases["purchase"] == "loading" else ICONS["wait"]
                    count_str = f" [{getattr(self, 'purchase_count', 0)}]" if getattr(self, 'purchase_count', 0) > 0 else ""
                    desc += f"{icon} Auto-Purchasing{count_str}...\n"
                    
                    unlocker = self.bot.task_queue.scraper_registry.unlocker
                    active_info = [
                        f"-> `Ch.{stats['task'].id:02d}`: {stats.get('progress', 0)}% | {stats['task'].purchase_status}"
                        for stats in unlocker.worker_stats.values()
                        if stats.get("view") == self and stats.get("task")
                    ]
                    if active_info: desc += "\n".join(active_info) + "\n"
            
            # 3. DOWNLOAD
            if self.phases["purchase"] == "done":
                if self.phases["download"] == "loading":
                    desc += f"{ICONS['load']} Processing [{len(self.active_tasks)}] chapters...\n"
                    comp = sum(1 for t in self.active_tasks if t.status == TaskStatus.COMPLETED)
                    if comp: desc += f"-> **{comp}** chapters completed.\n"
                    
                    for t in self.active_tasks:
                        if t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                            desc += f"-> `Ch.{t.id:02d}`: {ICONS['load']} {t.status.value}...\n"
                            break
                elif self.phases["download"] == "done":
                    desc += f"{ICONS['tick']} Download Completed."

            if self.final_link: desc += f"\n\n📂 **Destination:** [Open Google Drive]({self.final_link})"
            
            # Processing View Footer
            desc += f"\n──────────────────────────\n-# R-ID: {self.req_id} | S-ID: {self.series_id}"
        else:
            # 4. Standard Chapter List View
            desc += "### **Chapter List**\n"
            start = (self.page-1)*self.per_page
            for i, ch in enumerate(self.all_chapters[start:start+self.per_page]):
                idx, raw_t = start + i, ch.get('title','Ch')
                clean_t = raw_t.replace(' ', ' - ', 1)[:35]
                line = f"`{idx+1:02d}` | {clean_t}"
                desc += f"**{line}**\n" if idx in self.selected_indices else f"{line}\n"
            
            # Chapter List View Footer
            desc += f"\n**Page:** {self.page}/{self.max_page} | **Selected Chapter:** {sel_text}\n──────────────────────────\n-# R-ID: {self.req_id} | S-ID: {self.series_id}"

        # Apply description and add Thumbnail (Poster)
        embed.description = desc
        if self.image_url: embed.set_thumbnail(url=self.image_url)
        return embed

    async def update_view(self, interaction: discord.Interaction = None):
        if interaction:
            await interaction.response.edit_message(embed=self.build_live_embed(), view=self)
        else:
            if not self.interaction: return
            await self.interaction.edit_original_response(embed=self.build_live_embed(), view=self)

    @discord.ui.button(label="Start Extraction", style=discord.ButtonStyle.success, row=1)
    async def start(self, interaction: discord.Interaction, button: Button):
        from app.core.logger import req_id_context
        req_id_context.set(self.req_id)
        if not self.selected_indices: return await interaction.response.send_message("❌ Select chapters.", ephemeral=True)
        self.processing_mode, self.interaction = True, interaction
        for child in self.children: child.disabled = True
        self.phases["analyze"] = "loading"
        self.sub_status = "Identifying Client"
        self.purchase_count = 0 
        await self.update_view(interaction)
        
        asyncio.create_task(self.monitor_tasks())
        from app.services.batch_controller import BatchController
        controller = BatchController(self.bot)
        tasks = await controller.prepare_batch(interaction, sorted(list(self.selected_indices)), self.all_chapters, self.title, self.url, view_ref=self, series_id=self.series_id)
        
        if not tasks:
            self.phases.update({"analyze": "done", "purchase": "done", "download": "done"})
            self.trigger_refresh()
            return
            
        self.phases.update({"analyze":"done","purchase":"done","download":"loading"})
        self.active_tasks, self._last_hash = [], 0
        self.trigger_refresh()
        
        actual_tasks = []
        for t in tasks:
            t.is_smartoon = True
            t.series_id_key = self.series_id
            actual_task = await self.bot.task_queue.add_task(t)
            actual_tasks.append(actual_task)
        self.active_tasks = actual_tasks

    def trigger_refresh(self):
        from app.services.ui_manager import UIManager
        UIManager().request_update(self.req_id, self)

    async def monitor_tasks(self):
        while self.phases["download"] != "done":
            if self.active_tasks and all(t.status in [TaskStatus.COMPLETED, TaskStatus.FAILED] for t in self.active_tasks): 
                self.phases["download"] = "done"
            self.trigger_refresh()
            await asyncio.sleep(2)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, i, b): 
        if self.page > 1: self.page -= 1; await self.update_view(i)
    
    @discord.ui.button(label="Page", style=discord.ButtonStyle.secondary, row=0)
    async def jump(self, i, b): await i.response.send_modal(UniversalJumpModal(self))

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, i, b): 
        if self.page < self.max_page: self.page += 1; await self.update_view(i)

    @discord.ui.button(label="Select Range", style=discord.ButtonStyle.primary, row=1)
    async def range_select(self, i, b): await i.response.send_modal(UniversalRangeModal(self))

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger, row=1)
    async def clear(self, i, b): self.selected_indices.clear(); await self.update_view(i)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        logger.info(f"🚫 User cancelled dashboard for R-ID: {self.req_id}")
        if self.service_type == "mecha":
            browser = self.bot.task_queue.scraper_registry.browser
            browser.dec_session()
        self.stop()
        await interaction.response.edit_message(content="❌ **Dashboard Closed** (Manual Cancel)", embed=None, view=None)

class UniversalJumpModal(Modal, title="Jump to Page"):
    pg = TextInput(label="Page Number", placeholder="e.g. 5")
    def __init__(self, view): super().__init__(); self.view = view
    async def on_submit(self, i: discord.Interaction):
        try:
            p = int(self.pg.value)
            if 1 <= p <= self.view.max_page: 
                self.view.page = p
                await self.view.update_view(i)
            else: 
                await i.response.defer()
        except: 
            await i.response.defer()

class UniversalRangeModal(Modal, title="Select Range"):
    rng = TextInput(label="Range (e.g. 1-10, 15)", placeholder="1-5")
    def __init__(self, view): super().__init__(); self.view = view
    async def on_submit(self, i: discord.Interaction):
        try:
            parts = self.rng.value.replace(" ", "").split(",")
            for p in parts:
                if "-" in p:
                    s, e = map(int, p.split("-"))
                    for k in range(s, e+1):
                        if 1 <= k <= len(self.view.all_chapters): self.view.selected_indices.add(k-1)
                elif p.isdigit():
                    k = int(p); 
                    if 1 <= k <= len(self.view.all_chapters): self.view.selected_indices.add(k-1)
            await self.view.update_view(i)
        except: 
            await i.response.defer()
