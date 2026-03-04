import discord, asyncio, math, logging
from app.models.chapter import ChapterTask, TaskStatus

logger = logging.getLogger("Dashboard")
ICONS = {"load": "🔄", "tick": "✅", "wait": "⬜"}
COLORS = {"mecha": 0xe67e22, "smartoon": 0x2ecc71, "jumptoon": 0x9b59b6, "piccoma": 0xffd600, "kuaikan": 0xf1c40f}

class UniversalDashboard:
    active_views = {}  # Global router for raw V2 interactions

    def __init__(self, bot, ctx_data, service_type):
        self.bot = bot
        self.url, self.title, self.all_chapters = ctx_data['url'], ctx_data['title'], ctx_data['chapters']
        self.image_url, self.req_id, self.series_id, self.user = ctx_data['image_url'], ctx_data['req_id'], ctx_data['series_id'], ctx_data['user']
        self.service_type, self.color = service_type, COLORS.get(service_type, 0x2b2d31)
        
        if self.service_type == "mecha":
            self.bot.task_queue.scraper_registry.browser.inc_session()

        self.page, self.per_page = 1, 10
        self.max_page = math.ceil(len(self.all_chapters) / self.per_page) if self.all_chapters else 1
        self.selected_indices, self.active_tasks = set(), []
        
        self.phases = {"analyze": "waiting", "purchase": "waiting", "download": "waiting"}
        self.final_link, self.interaction, self.sub_status, self.processing_mode, self._last_hash = None, None, None, False, 0
        
        # 🟢 UI Toggle for Selection Mode Menu
        self.show_selection_menu = False
        
        # Register to global router
        UniversalDashboard.active_views[self.req_id] = self

    def build_v2_payload(self):
        """Constructs the pure Discord V2 Container Layout with Sub-Menu Logic"""
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

        # 1. HEADER SECTION
        header_text = f"## {self.title}\n**Total Chapters:** {len(self.all_chapters)} | **Service:** {self.service_type.upper()}"
        
        # 2. CONTENT DESCRIPTION (Processing Mode or Chapter List)
        desc = ""
        if self.processing_mode:
            # --- Status Tracking UI ---
            if self.phases["analyze"] == "done": desc += f"{ICONS['tick']} Analyzed.\n"
            else:
                icon = ICONS["load"] if self.phases["analyze"] == "loading" else ICONS["wait"]
                desc += f"{icon} Analyzing... {f'({self.sub_status})' if self.sub_status else ''}\n"
            
            if self.phases["analyze"] == "done":
                if self.phases["purchase"] == "done": desc += f"{ICONS['tick']} Purchased.\n"
                else:
                    icon = ICONS["load"] if self.phases["purchase"] == "loading" else ICONS["wait"]
                    desc += f"{icon} Auto-Purchasing...\n"
            
            if self.phases["purchase"] == "done":
                if self.phases["download"] == "loading":
                    desc += f"{ICONS['load']} Processing [{len(self.active_tasks)}] chapters...\n"
                elif self.phases["download"] == "done":
                    desc += f"{ICONS['tick']} Download Completed."

            if self.final_link: desc += f"\n\n📂 **Destination:** [Open Google Drive]({self.final_link})"
            desc += f"\n\n**Selected:** {sel_count} ({sel_text})"
        else:
            # --- Interactive Chapter List ---
            desc += "### **Chapter List**\n"
            start = (self.page - 1) * self.per_page
            display_slice = self.all_chapters[start : start + self.per_page]
            
            for i, ch in enumerate(display_slice):
                idx = start + i
                # Check for the NEW badge from scraper metadata
                new_tag = "✨ " if ch.get('is_new') else ""
                lock_icon = "🔒" if ch.get('is_locked') else "🔓"
                tick = "✅" if idx in self.selected_indices else "⬛"
                
                # Format: 01 | ✨ ✅ 🔓 第1話 - Title
                raw_t = ch.get('title', 'Ch').replace(' ', ' - ', 1)[:30]
                desc += f"`{idx+1:02d}` | {new_tag}{tick} {lock_icon} **{raw_t}**\n"
            
            desc += f"\n**Selected:** {sel_count} ({sel_text})"

        # 3. COMPONENT ASSEMBLY
        inner_components = []
        
        # Header Section (With Vertical Poster Accessory)
        # Note: Accessory field is ONLY added if image_url exists to prevent 400 errors.
        header_section = {
            "type": 9, # SECTION
            "components": [{"type": 10, "content": header_text}]
        }
        if self.image_url:
            header_section["accessory"] = {
                "type": 11, # MEDIA
                "media": {"url": self.image_url}
            }
        inner_components.append(header_section)
        
        inner_components.append({"type": 14, "spacing": 1}) # Separator
        inner_components.append({"type": 10, "content": desc}) # Main Body Text
        
        # 4. INTERACTIVE ELEMENTS (Selection Mode & Navigation)
        if not self.processing_mode:
            inner_components.append({"type": 14, "spacing": 1}) # Separator
            
            # --- ROW 1: SELECTION MENU (THE RADIO GROUP) ---
            # This only appears if show_selection_menu is toggled on
            if getattr(self, "show_selection_menu", False):
                # Look for the latest "UP" chapter in metadata
                new_ch = next((ch for ch in self.all_chapters if ch.get('is_new')), None)
                latest_label = f"[NEW] {new_ch['title']} released." if new_ch else "No New Chapter."

                inner_components.append({
                    "type": 1, # ACTION ROW
                    "components": [{
                        "type": 3, # STRING SELECT
                        "custom_id": f"mode_select_{self.req_id}",
                        "placeholder": "Choose Selection Mode",
                        "options": [
                            {"label": "SR", "description": "Select all available chapters.", "value": "all", "emoji": {"name": "📚"}},
                            {"label": "Select Chapter", "description": "Add custom range in the box", "value": "custom", "emoji": {"name": "🖊️"}},
                            {"label": "Latest Chapter", "description": latest_label, "value": "latest", "emoji": {"name": "✨"}, "disabled": not new_ch}
                        ]
                    }]
                })
            else:
                # Default State: Show the "Select Chapters" button to open the menu
                inner_components.append({
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 1, "label": "Select Chapters", "custom_id": f"btn_open_menu_{self.req_id}"},
                        {"type": 2, "style": 3, "label": "Start Extraction", "custom_id": f"btn_start_{self.req_id}", "disabled": sel_count == 0}
                    ]
                })

            # --- ROW 2: PAGE NAVIGATION ---
            p_options = []
            s_p = max(1, self.page - 12)
            e_p = min(self.max_page, s_p + 24)
            for p in range(s_p, e_p + 1):
                opt = {"label": f"Page {p}", "value": str(p), "emoji": {"name": "📄"}}
                if p == self.page:
                    opt["description"] = "(Current Page)"
                    opt["default"] = True
                p_options.append(opt)
            
            inner_components.append({
                "type": 1, 
                "components": [{
                    "type": 3, "custom_id": f"page_select_{self.req_id}", "options": p_options
                }]
            })

        # 5. FOOTER SECTION
        inner_components.append({"type": 14, "spacing": 1})
        footer_text = f"-# R-ID: {self.req_id} | S-ID: {self.series_id}"
        
        footer_section = {
            "type": 9,
            "components": [{"type": 10, "content": footer_text}]
        }
        # Add Cancel button as an accessory pinned to the right
        if not self.processing_mode:
            footer_section["accessory"] = {
                "type": 2, "style": 4, "emoji": {"name": "✖️"}, "custom_id": f"btn_cancel_{self.req_id}"
            }
        inner_components.append(footer_section)

        # Final V2 Container Wrapper
        return [{
            "type": 17, # CONTAINER
            "accent_color": self.color if self.phases.get("download") != "done" else 0x2ecc71,
            "components": inner_components
        }]

    async def update_view(self, interaction: discord.Interaction = None):
        """Pushes raw V2 JSON natively via HTTP"""
        payload_data = {"flags": 32768, "components": self.build_v2_payload(), "content": ""}
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
                        await self.bot.http.request(route, json=payload_data)
                    else:
                        raise e
        except Exception as e:
            logger.error(f"V2 UI Update Failed: {e}", exc_info=True)

    def trigger_refresh(self):
        from app.services.ui_manager import UIManager
        UIManager().request_update(self.req_id, self)

    async def monitor_tasks(self):
        while self.phases["download"] != "done":
            if self.active_tasks and all(t.status in [TaskStatus.COMPLETED, TaskStatus.FAILED] for t in self.active_tasks): 
                self.phases["download"] = "done"
            self.trigger_refresh()
            await asyncio.sleep(2)
