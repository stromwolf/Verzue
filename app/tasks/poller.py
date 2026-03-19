import asyncio
import logging
import datetime
import traceback
import discord
from discord.ext import tasks

from app.services.group_manager import (
    get_all_subscriptions,
    update_last_chapter,
    get_admin_settings,
    get_title_override,
    get_next_notification_id,
)
from app.bot.common.notification_builder import build_notification_payload

logger = logging.getLogger("AutoPoller")

# Max concurrent scraper checks to avoid rate-limiting
CONCURRENCY_LIMIT = 10


class AutoDownloadPoller:
    def __init__(self, bot):
        self.bot = bot
        # Run daily at 15:05 UTC (5 min buffer after platform releases at 15:00)
        self.check_time = datetime.time(hour=15, minute=5, tzinfo=datetime.timezone.utc)
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()

    @tasks.loop(time=datetime.time(hour=15, minute=5, tzinfo=datetime.timezone.utc))
    async def poll_loop(self):
        """The main polling loop that fires at 15:05 UTC."""
        logger.info("🕒 [AutoPoller] Waking up for daily subscription check.")
        await self._check_subscriptions()

    @poll_loop.before_loop
    async def before_poll_loop(self):
        logger.info("⏳ [AutoPoller] Waiting for bot to be ready before starting loop...")
        await self.bot.wait_until_ready()
        logger.info("✅ [AutoPoller] Loop started and waiting for 15:05 UTC.")

    async def _check_subscriptions(self):
        """Checks all subscriptions concurrently using asyncio.gather + semaphore."""
        all_subs = get_all_subscriptions()

        if not all_subs:
            logger.info("💤 [AutoPoller] No active subscriptions found. Going back to sleep.")
            return

        today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
        logger.info(f"📅 [AutoPoller] Today is {today_name}. Checking {len(all_subs)} total subs.")

        # Filter to only today's release day subs
        todays_subs = [
            (group_name, sub) for group_name, sub in all_subs
            if sub.get("release_day", "").lower() == today_name.lower()
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
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time = data

            # S-GRADE: Update release day if detected and missing (Phase 10 Automation)
            if release_day and not sub.get("release_day"):
                from app.services.group_manager import set_release_day
                if set_release_day(group_name, sub["series_url"], release_day):
                    logger.info(f"📅 [AutoPoller] Auto-detected release day for {title}: {release_day}")
                    sub["release_day"] = release_day # Update local dict too

            if not chapter_list:
                return False

            # Compare latest chapter ID
            latest_chapter = chapter_list[-1]
            last_known = str(sub.get("last_known_chapter_id", "0"))
            current_id = str(latest_chapter["id"])
            is_new_badge = latest_chapter.get("is_new", False)

            if current_id != last_known and (is_new_badge or sub["platform"].lower() != "jumptoon"):
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
        image_url: str = None,
    ):
        """Sends a V2 Component notification to the target channel."""
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

    # --- DEBUG COMMAND IMPL ---
    async def trigger_manual_poll(self, ctx):
        """Allows admins to force a poll immediately (ignores day/time checks)."""
        msg = await ctx.send("🔄 **Forcing Subscription Poll...** (Ignoring day/time checks)")

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

    async def _force_check_single(self, group_name: str, sub: dict) -> bool:
        """Force-check a single sub (ignores release_day filter)."""
        try:
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(sub["series_url"])
            data = await scraper.get_series_info(sub["series_url"])
            title, _, chapter_list, image_url, series_id = data

            if not chapter_list:
                return False

            latest_chapter = chapter_list[-1]
            last_known = str(sub.get("last_known_chapter_id", "0"))
            current_id = str(latest_chapter["id"])

            if current_id != last_known:
                update_last_chapter(group_name, series_id, current_id)

                await self._notify_channel(
                    group_name=group_name,
                    sub=sub,
                    series_title=title,
                    series_id=series_id,
                    image_url=image_url,
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Debug Poll Error for {sub.get('series_title')}: {e}")
            return False
