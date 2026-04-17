# ═══════════════════════════════════════════════════════════════════════════
# POLLER INTEGRATION PATCH
# File: app/tasks/poller.py  (inside _check_single_sub)
#
# Change: pass poll=True to get_series_info for Jumptoon subscriptions.
# Fallback: Mecha/Piccoma still use the existing call signature (no change
# needed — they accept **kwargs-safe via default param unused).
# ═══════════════════════════════════════════════════════════════════════════

async def _check_single_sub(self, group_name: str, sub: dict) -> bool:
    """Logic for analyzing a single series for updates."""
    try:
        logger.info(f"🔍 [AutoPoller] Checking {sub['series_title']} for {group_name}...")
        scraper = self.bot.task_queue.provider_manager.get_provider_for_url(sub["series_url"])

        # 🟢 OPTIMIZATION: Jumptoon supports poll-mode (page 1 + cache check).
        # Other providers ignore unknown kwargs via their signature defaults.
        is_jumptoon = "jumptoon.com" in sub["series_url"].lower()
        if is_jumptoon:
            data = await scraper.get_series_info(sub["series_url"], poll=True)
        else:
            data = await scraper.get_series_info(sub["series_url"])

        title, total_chapters, chapter_list, image_url, series_id, \
            release_day, release_time, status_label, genre_label = data

        # ... rest of the function is unchanged ...
        # (latest_chapter extraction, last_known comparison, notification dispatch)
