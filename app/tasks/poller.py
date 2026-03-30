import asyncio
import logging
import datetime
import traceback
import time
import discord
from discord.ext import tasks

from app.services.group_manager import (
    get_all_subscriptions,
    update_last_chapter,
    get_admin_settings,
    get_title_override,
    get_next_notification_id,
    load_group,
    save_group,
)
from app.bot.common.notification_builder import (
    build_notification_payload,
    build_new_series_notification_payload,
)

logger = logging.getLogger("AutoPoller")

# Max concurrent scraper checks to avoid rate-limiting
CONCURRENCY_LIMIT = 10

# Jumptoon New Series Detection Target
JUMPTOON_NEW_SERIES_CHANNEL_ID = 1436068941310197822


class AutoDownloadPoller:
    def __init__(self, bot):
        self.bot = bot
        self.is_checking = False  # 🟢 [CONCURRENCY LOCK]
        # Run daily at 15:05 UTC (5 min buffer after platform releases at 15:00)
        self.check_time = datetime.time(hour=15, minute=5, tzinfo=datetime.timezone.utc)
        self.poll_loop.start()
        # High-frequency window (15:00 - 15:05 UTC)
        self.high_freq_poll_loop.start()
        # New Series Detection (Every 30 minutes)
        self.new_series_poll_loop.start()
        # 🟢 Smart Priority Background Poller (Every 5 minutes)

    @tasks.loop(time=datetime.time(hour=15, minute=5, tzinfo=datetime.timezone.utc))
    async def poll_loop(self):
        """The main polling loop that fires at 15:05 UTC."""
        logger.info("🕒 [AutoPoller] Waking up for daily subscription check.")
        await self._check_subscriptions()

    @tasks.loop(seconds=10)
    async def high_freq_poll_loop(self):
        """High-frequency polling for specific platforms between 15:00 and 15:05 UTC."""
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Define the window: 15:00:01 - 15:05:00
        start_wait = now.replace(hour=15, minute=0, second=1, microsecond=0)
        end_wait = now.replace(hour=15, minute=5, second=0, microsecond=0)

        if not (start_wait <= now <= end_wait):
            # Outside of window, we don't need to check 10s. 
            # We can skip most of the work if we're not close.
            # But tasks.loop(seconds=10) will still fire.
            return

        logger.info(f"⚡ [AutoPoller] High-frequency window active ({now.strftime('%H:%M:%S')} UTC). Checking targets...")
        await self._check_high_freq_targets()

    async def _check_high_freq_targets(self):
        """Checks subscriptions for piccoma, jumptoon, and mecha specifically."""
        all_subs = get_all_subscriptions()
        if not all_subs:
            return

        today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
        targets = ["piccoma", "jumptoon", "mecha", "kakao"] # Added kakao to targets
        
        # Filter to target platforms + today's release day
        todays_targets = [
            (group_name, sub) for group_name, sub in all_subs
            if sub and (sub.get("platform") or "").lower() in targets and
               (sub.get("release_day") or "").lower() == today_name.lower()
        ]

        if not todays_targets:
            return

        logger.debug(f"🚀 [AutoPoller] Checking {len(todays_targets)} high-frequency targets...")
        
        # We process these sequentially but quickly to avoid overlapping loops
        # and because we only expect a few titles per platform.
        for group_name, sub in todays_targets:
            try:
                # Check Redis to see if we already found a new chapter for this series today
                # to avoid hitting the scraper every 10 seconds if it's already done.
                date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                redis_key = f"verzue:poller:found:{sub['series_id']}:{date_str}"
                
                if await self.bot.redis_brain.client.get(redis_key):
                    continue

                found = await self._check_single_sub(group_name, sub)
                if found:
                    logger.info(f"✅ [AutoPoller] Found new chapter for {sub.get('series_title')} via high-freq poller.")
                    # Mark as found for the rest of today
                    await self.bot.redis_brain.client.set(redis_key, "1", ex=86400)
            except Exception as e:
                logger.error(f"❌ [AutoPoller] High-freq error for {sub.get('series_title')}: {e}")

    @poll_loop.before_loop
    @high_freq_poll_loop.before_loop
    async def before_poll_loop(self):
        logger.debug("⏳ [AutoPoller] Waiting for bot to be ready before starting loop...")
        await self.bot.wait_until_ready()
        logger.debug("✅ [AutoPoller] Loop started.")

    async def _check_subscriptions(self):
        """Checks all subscriptions concurrently using asyncio.gather + semaphore."""
        if self.is_checking:
            logger.warning("🕒 [AutoPoller] Attempted to start poll while one is already in progress. Skipping.")
            return
        
        self.is_checking = True
        try:
            all_subs = get_all_subscriptions()

            if not all_subs:
                logger.info("💤 [AutoPoller] No active subscriptions found. Going back to sleep.")
                return

            today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
            logger.info(f"📅 [AutoPoller] Today is {today_name}. Checking {len(all_subs)} total subs.")

            # 🟢 S-GRADE: Include series with NO release_day (Cold Start/New Subscriptions)
            todays_subs = [
                (group_name, sub) for group_name, sub in all_subs
                if sub and (not sub.get("release_day") or (sub.get("release_day") or "").lower() == today_name.lower())
            ]

            if not todays_subs:
                logger.info(f"💤 [AutoPoller] No subscriptions scheduled for {today_name}. Going back to sleep.")
                return

            logger.info(f"🚀 [AutoPoller] {len(todays_subs)} subs scheduled for today. Starting concurrent check...")

            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

            async def check_with_semaphore(group_name, sub):
                async with semaphore:
                    return await self._check_single_sub(group_name, sub)

            tasks_list = [
                check_with_semaphore(group_name, sub)
                for group_name, sub in todays_subs
            ]

            results = await asyncio.gather(*tasks_list, return_exceptions=True)

            found_new = sum(1 for r in results if r is True)
            errors = sum(1 for r in results if isinstance(r, Exception))
            logger.info(f"✅ [AutoPoller] Poll complete. New chapters: {found_new}, Errors: {errors}")

        finally:
            self.is_checking = False

    async def _check_single_sub(self, group_name: str, sub: dict) -> bool:
        """
        Checks a single subscription for new chapters.
        Returns True if a new chapter was found and notified.
        """
        try:
            logger.info(f"🔍 [AutoPoller] Checking {sub['series_title']} for {group_name}...")

            # Lightweight check: get series info (Approach 4)
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(sub["series_url"])
            data = await scraper.get_series_info(sub["series_url"])
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
            
            # --- GRANULAR DIAGNOSIS ---
            latest_chapter = chapter_list[-1] if chapter_list else None
            last_known = str(sub.get("last_known_chapter_id", "0"))
            current_id = str(latest_chapter["id"]) if latest_chapter else "0"
            is_new = latest_chapter.get("is_new", False) if latest_chapter else False
            
            logger.info(f"📊 [AutoPoller] Check {title}: current={current_id}, last_known={last_known}, UP_tag={is_new}")

            # S-GRADE: Update release day if detected and missing (Phase 10 Automation)
            if release_day and not sub.get("release_day"):
                from app.services.group_manager import set_release_day
                if set_release_day(group_name, sub["series_url"], release_day):
                    logger.info(f"📅 [AutoPoller] Auto-detected release day for {title}: {release_day}")
                    sub["release_day"] = release_day # Update local dict too
            
            # 🟢 S-GRADE: Status Auto-Detection (Hiatus/Completed)
            current_status = sub.get("status", "Weekly")
            new_status = status_label if status_label else "Weekly"
            
            if new_status != current_status:
                data = load_group(group_name)
                for s in data["subscriptions"]:
                    if s["series_id"] == series_id:
                        s["status"] = new_status
                        save_group(group_name, data)
                        logger.info(f"🏷️ [AutoPoller] Status updated for {title}: {current_status} → {new_status}")
                        break

            if not latest_chapter:
                return False

            # Comparison Logic
            if current_id != last_known:
                logger.info(f"🚨 [AutoPoller] NEW CHAPTER DETECTED! {title} - {latest_chapter['notation']}")

                # Update stored chapter right away
                update_last_chapter(group_name, series_id, current_id)

                # Send V2 Component notification
                await self._notify_channel(
                    group_name=group_name,
                    sub=sub,
                    series_title=title,
                    series_id=series_id,
                    image_url=image_url,
                    chapter_id=current_id,
                )
                return True

            return False

        except Exception as e:
            logger.error(f"❌ [AutoPoller] Error checking {sub.get('series_title')}: {e}")
            logger.debug(traceback.format_exc())
            raise

    async def _notify_channel(
        self,
        *,
        group_name: str,
        sub: dict,
        series_title: str,
        series_id: str,
        image_url: str | None = None,
        chapter_id: str | None = None,
    ):
        """Sends a V2 Component notification to the target channel."""
        from app.core.logger import req_id_context, group_name_context, log_category_context
        
        # 🟢 S-GRADE: Inject Notification Context
        notif_id = f"notif_{int(time.time())}"
        t1 = req_id_context.set(notif_id)
        t2 = group_name_context.set(group_name)
        t3 = log_category_context.set("Notification")
        
        try:
            channel = self.bot.get_channel(sub["channel_id"])
            if not channel:
                logger.warning(f"[AutoPoller] Channel {sub['channel_id']} not found, skipping notification.")
                return

            # Get admin settings for role ping
            admin = get_admin_settings(group_name)
            role_id = admin.get("role_id")

            # Get custom title override
            custom_title = get_title_override(group_name, sub["series_url"])

            # Get next N-ID
            notification_id = get_next_notification_id(group_name)

            # Build the V2 Component payload
            payload = build_notification_payload(
                platform=sub["platform"],
                role_id=role_id,
                series_title=series_title,
                custom_title=custom_title,
                poster_url=image_url,
                series_url=sub["series_url"],
                series_id=series_id,
                notification_id=notification_id,
                chapter_id=chapter_id,
            )

            try:
                route = discord.http.Route(
                    'POST',
                    '/channels/{channel_id}/messages',
                    channel_id=channel.id,
                )
                await self.bot.http.request(route, json=payload)
                logger.info(f"📨 [AutoPoller] Notification sent for {series_title} (N-ID: {notification_id})")
            except Exception as e:
                logger.error(f"Failed to send release notification to {channel.id}: {e}")
        finally:
            req_id_context.reset(t1)
            group_name_context.reset(t2)
            log_category_context.reset(t3)

    # --- DEBUG COMMAND IMPL ---
    async def trigger_manual_poll(self, ctx):
        """Allows admins to force a poll immediately (ignores day/time checks)."""
        msg = await ctx.send("🔄 **Forcing Subscription Poll...** (Ignoring day/time checks)")

        if self.is_checking:
            return await msg.edit(content="⚠️ **Poll already in progress.** Please wait for the current check to complete.")

        self.is_checking = True
        try:
            all_subs = get_all_subscriptions()
            if not all_subs:
                return await msg.edit(content="❌ No subscriptions found globally.")

            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            found_new = 0

            async def check_with_semaphore(group_name, sub):
                async with semaphore:
                    return await self._force_check_single(group_name, sub)

            tasks_list = [
                check_with_semaphore(group_name, sub)
                for group_name, sub in all_subs
            ]

            results = await asyncio.gather(*tasks_list, return_exceptions=True)
            found_new = sum(1 for r in results if r is True)

            await msg.edit(content=f"✅ **Poll Complete!** Queued {found_new} new chapter(s).")
        finally:
            self.is_checking = False

    async def _force_check_single(self, group_name: str, sub: dict) -> bool:
        """Force-check a single sub (ignores release_day filter)."""
        try:
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(sub["series_url"])
            data = await scraper.get_series_info(sub["series_url"])
            title, _, chapter_list, image_url, series_id, _, _, _, _ = data

            if not chapter_list:
                return False

            latest_chapter = chapter_list[-1]
            last_known = str(sub.get("last_known_chapter_id", "0"))
            current_id = str(latest_chapter["id"])
            is_new = latest_chapter.get("is_new", False)

            # 📊 Diagnostic log synced with auto-poller
            logger.info(f"📊 [AutoPoller] (Force) Check {title}: current={current_id}, last_known={last_known}, UP_tag={is_new}")

            if current_id != last_known:
                update_last_chapter(group_name, series_id, current_id)

                await self._notify_channel(
                    group_name=group_name,
                    sub=sub,
                    series_title=title,
                    series_id=series_id,
                    image_url=image_url,
                    chapter_id=current_id,
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Debug Poll Error for {sub.get('series_title')}: {e}")
            return False

    @tasks.loop(minutes=30)
    async def smart_batch_poll_loop(self):
        """
        🟢 STRICT PRIORITY SCHEDULING (Mar 25 Final Request)
        - Weeklies: Checked ONLY via high_freq_poll_loop (15:00-15:05 UTC).
        - Hiatus: Checked once every 24 hours.
        """
        all_subs = get_all_subscriptions()
        if not all_subs: return

        now_ts = time.time()
        
        # We only prioritize Hiatus series here. 
        # Weeklies are handled by the high_freq_poll_loop and the daily sweep.
        hiatus_subs = [
            (g, s) for g, s in all_subs 
            if s and (s.get("status") or "").lower() == "hiatus"
        ]
        
        if not hiatus_subs: return

        for g_name, sub in hiatus_subs:
            s_id = sub["series_id"]
            last_check_key = f"verzue:poller:last_check:{s_id}"
            last_check_raw = await self.bot.redis_brain.client.get(last_check_key)
            last_check = float(last_check_raw) if last_check_raw else 0
            
            # Hiatus Interval: 24 hours (86400 seconds)
            if (now_ts - last_check) >= 86400:
                try:
                    logger.info(f"💤 [SmartPoller] Checking Hiatus series: {sub.get('series_title')}")
                    await self.bot.redis_brain.client.set(last_check_key, str(now_ts), ex=172800)
                    await self._check_single_sub(g_name, sub)
                    # Small delay between hiatus checks
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"❌ [SmartPoller] Hiatus check failed for {sub.get('series_title')}: {e}")

    # --- NEW SERIES DETECTION ---
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
                
                # Notify
                await self._notify_new_series(series, platform_name, channel_id)

        except Exception as e:
            logger.error(f"❌ [AutoPoller] Discovery Error for {platform_name}: {e}")

    async def _notify_new_series(self, series: dict, platform: str, channel_id: int):
        """Sends a 'New Series premiere' notification to the target channel."""
        from app.core.logger import req_id_context, group_name_context, log_category_context
        
        # 🟢 S-GRADE: Inject Discovery Context
        notif_id = f"discovery_{int(time.time())}"
        t1 = req_id_context.set(notif_id)
        t2 = group_name_context.set("Global") # Discovery is bot-wide
        t3 = log_category_context.set("Notification")

        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"[AutoPoller] Notification Channel {channel_id} not found.")
                return

            payload = build_new_series_notification_payload(
                platform=platform,
                series_title=series["title"],
                poster_url=series.get("poster_url") or series.get("poster"),
                series_url=series["url"],
                series_id=series["series_id"]
            )

            try:
                route = discord.http.Route(
                    'POST',
                    '/channels/{channel_id}/messages',
                    channel_id=channel.id,
                )
                await self.bot.http.request(route, json=payload)
                logger.info(f"📨 [AutoPoller] {platform.capitalize()} alert sent for {series['title']}")
            except Exception as e:
                logger.error(f"❌ Failed to send Discord alert for {series['title']}: {e}")
        finally:
            req_id_context.reset(t1)
            group_name_context.reset(t2)
            log_category_context.reset(t3)
