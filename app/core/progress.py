import logging
import sys
from .logger import req_id_context

class ProgressBar:
    """S-Grade Progress Bar for log-compatible console output."""
    def __init__(self, req_id: str, label: str, service: str, total: int, bar_length: int = 20, episode_id: str | None = None):
        self.req_id = req_id
        self.label = label
        self.service = service
        self.total = total
        self.bar_length = bar_length
        self.completed = 0
        self.episode_id = episode_id
        self._last_percent = -1
        
        # 🔧 FIX: Immediately push the status to the view on construction
        # This guarantees Discord sees "Downloading..." before gather() runs
        self._push_status_immediately()

    def _push_status_immediately(self):
        """Force-sets the task status and triggers a refresh BEFORE the concurrent work begins."""
        try:
            from app.bot.common.view import UniversalDashboard
            from app.models.chapter import TaskStatus
            view = UniversalDashboard.active_views.get(self.req_id)
            if not view:
                return
                
            new_status = TaskStatus.DOWNLOADING if self.label == "Downloading" else TaskStatus.UPLOADING
            updated = False
            for t in view.active_tasks:
                if self.episode_id and str(t.episode_id) != str(self.episode_id):
                    continue
                    
                if t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                    if t.status != new_status:
                        t.status = new_status
                        updated = True
                    break
                    
            if updated:
                view.trigger_refresh()
        except:
            pass

    def update(self, current: int = None):
        if current is not None:
            self.completed = current
        
        percent = int((self.completed / self.total) * 100) if self.total > 0 else 100
        filled_length = int(self.bar_length * self.completed // self.total) if self.total > 0 else self.bar_length
        bar = '▰' * filled_length + '▱' * (self.bar_length - filled_length)
        msg = f"{self.label}: [{self.service}] {bar} {self.completed}/{self.total} ({percent}%)"

        # 1. Console Real-time Update (Visual only, no new line)
        if sys.stdout.isatty():
            # Apply same formatting for consistency
            from app.core.logger import CustomFormatter
            fmt_msg = f"[INFO ] [{self.req_id}] - {msg}"
            sys.stdout.write(f"\r{fmt_msg}")
            sys.stdout.flush()

        # 2. Logger Throttle: Log to permanent files at certain milestones (25, 50, 75, 100%)
        # This keeps logs visible during background uploads without flooding them.
        milestones = [25, 50, 75, 100]
        current_milestone = next((m for m in milestones if percent >= m and not hasattr(self, f"_logged_{m}")), None)
        
        if current_milestone:
            setattr(self, f"_logged_{current_milestone}", True)
            token = req_id_context.set(self.req_id)
            try:
                if percent == 100 and sys.stdout.isatty(): sys.stdout.write("\n")
                logging.getLogger("ProgressBar").info(msg)
            finally:
                req_id_context.reset(token)

        # 3. Dashboard Sync
        try:
            from app.bot.common.view import UniversalDashboard
            view = UniversalDashboard.active_views.get(self.req_id)
            if view:
                from app.models.chapter import TaskStatus
                new_status = TaskStatus.DOWNLOADING if self.label == "Downloading" else TaskStatus.UPLOADING
                
                updated = False
                for t in view.active_tasks:
                    # If we have a specific episode_id, only update THAT task
                    if self.episode_id and str(t.episode_id) != str(self.episode_id):
                        continue
                        
                    if t.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                        if t.status != new_status:
                            t.status = new_status
                            updated = True
                        
                        # If we updated the target task (or the first available one if no ID provided), we can stop
                        if self.episode_id or updated:
                            break
                
                if updated and percent > 0:
                    view.trigger_refresh()
        except: pass

    def finish(self):
        # Ensure 100% is logged at least once if it wasn't already
        if not getattr(self, "_logged_final", False):
            self.update(self.total)
