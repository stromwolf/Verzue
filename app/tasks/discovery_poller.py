import logging
import datetime
import time
import discord
from discord.ext import tasks

logger = logging.getLogger("AutoPoller.Discovery")

# Jumptoon New Series Detection Target
JUMPTOON_NEW_SERIES_CHANNEL_ID = 1436068941310197822

class DiscoveryPoller:
    def __init__(self, bot, notifier):
        self.bot = bot
        self.notifier = notifier
        self.new_series_poll_loop.start()

    @tasks.loop(seconds=10)
    async def new_series_poll_loop(self):
        """
        🟢 PREMIERE DETECTION (Mar 25 Request)
        Restricted to 15:00:01 - 15:03:00 UTC window.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        start = now.replace(hour=15, minute=0, second=1, microsecond=0)
        end = now.replace(hour=15, minute=3, second=0, microsecond=0)
        
        if not (start <= now <= end):
            return

        logger.info("🕒 [AutoPoller] Premiere Window active. Checking for new series.")
        try:
            platforms = [
                ("jumptoon", JUMPTOON_NEW_SERIES_CHANNEL_ID),
                ("piccoma", JUMPTOON_NEW_SERIES_CHANNEL_ID),
                ("mecha", JUMPTOON_NEW_SERIES_CHANNEL_ID)
            ]
            for p_name, ch_id in platforms:
                await self._run_discovery_for_platform(p_name, ch_id)
        except Exception as e:
            logger.error(f"❌ [AutoPoller] Premiere Discovery failed: {e}")

    async def _run_discovery_for_platform(self, platform_name: str, channel_id: int):
        """Generic discovery engine for any supported platform (Jumptoon, Piccoma, Mecha)."""
        # --- Feature Flag Guard ---
        if not self.bot.app_state.is_enabled(f"notifications.{platform_name}"):
            return

        try:
            # Map platform name to a representative URL to get the provider
            base_urls = {
                "jumptoon": "https://jumptoon.com",
                "piccoma": "https://piccoma.com",
                "mecha": "https://mechacomic.jp"
            }
            target_url = base_urls.get(platform_name)
            if not target_url: return

            provider = self.bot.task_queue.provider_manager.get_provider_for_url(target_url)
            if not provider: return

            new_series = await provider.get_new_series_list()
            if not new_series:
                return

            # Use Redis set to track seen series IDs
            redis_key = f"verzue:seen:{platform_name}_new_series"

            # S-GRADE: Determine if this is the first time we've ever run discovery
            is_first_scan = not await self.bot.redis_brain.client.exists(redis_key)
            if is_first_scan:
                logger.info(f"🧠 [AutoPoller] First-time {platform_name.capitalize()} Discovery: Silently seeding the brain.")

            for series in new_series:
                s_id = series["series_id"]
                if is_first_scan:
                    await self.bot.redis_brain.client.sadd(redis_key, s_id)
                    continue

                # Check if already seen
                is_seen = await self.bot.redis_brain.client.sismember(redis_key, s_id)
                if is_seen:
                    logger.debug(f"🤫 [AutoPoller] {platform_name.capitalize()} - {series['title']} is already in the brain. Skipping.")
                    continue
                    
                logger.info(f"🆕 [AutoPoller] NEW {platform_name.upper()} SERIES DETECTED: {series['title']} ({s_id})")
                
                # Store in brain immediately
                await self.bot.redis_brain.client.sadd(redis_key, s_id)
                
                # Notify via the extracted notifier
                await self.notifier._notify_new_series(series, platform_name, channel_id)

        except Exception as e:
            logger.error(f"❌ [AutoPoller] Discovery Error for {platform_name}: {e}")
