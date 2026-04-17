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
    update_last_up_chapter,
    load_group,
    save_group,
)
from .notifier import PollerNotifier
from .discovery_poller import DiscoveryPoller

logger = logging.getLogger("AutoPoller")

# Max concurrent scraper checks to avoid rate-limiting
CONCURRENCY_LIMIT = 10

class AutoDownloadPoller:
    def __init__(self, bot):
        self.bot = bot
        self.is_checking = False  
        
        # 🟢 Initialize Specialized Sub-Pollers
        self.notifier = PollerNotifier(bot)
        self.discovery = DiscoveryPoller(bot, self.notifier)
        
        # 🟢 Main Daily Polling Loop (15:05 UTC)
        self.poll_loop.start()
        # 🟢 High-frequency window (15:00 - 15:05 UTC)
        self.high_freq_poll_loop.start()
        # 🟢 Hiatus Sweep (Every 30 minutes)
        self.smart_batch_poll_loop.start()

    @tasks.loop(time=datetime.time(hour=15, minute=5, tzinfo=datetime.timezone.utc))
    async def poll_loop(self):
        """The main polling loop that fires at 15:05 UTC."""
        logger.info("🕒 [AutoPoller] Waking up for daily subscription check.")
        await self._check_subscriptions()

    @tasks.loop(seconds=10)
    async def high_freq_poll_loop(self):
        """High-frequency polling for specific platforms between 15:00 and 15:05 UTC."""
        now = datetime.datetime.now(datetime.timezone.utc)
        start_wait = now.replace(hour=15, minute=0, second=1, microsecond=0)
        end_wait = now.replace(hour=15, minute=5, second=0, microsecond=0)

        if not (start_wait <= now <= end_wait):
            return

        logger.info(f"⚡ [AutoPoller] High-frequency window active ({now.strftime('%H:%M:%S')} UTC). Checking targets...")
        await self._check_high_freq_targets()

    @tasks.loop(minutes=30)
    async def smart_batch_poll_loop(self):
        """🟢 Hiatus Series Sweep (Checked once every 24 hours)."""
        all_subs = get_all_subscriptions()
        if not all_subs: return

        now_ts = time.time()
        hiatus_subs = [(g, s) for g, s in all_subs if s and (s.get("status") or "").lower() == "hiatus"]
        
        if not hiatus_subs: return

        for g_name, sub in hiatus_subs:
            s_id = sub["series_id"]
            last_check_key = f"verzue:poller:last_check:{s_id}"
            last_check_raw = await self.bot.redis_brain.client.get(last_check_key)
            last_check = float(last_check_raw) if last_check_raw else 0
            
            if (now_ts - last_check) >= 86400:
                try:
                    logger.info(f"💤 [SmartPoller] Checking Hiatus series: {sub.get('series_title')}")
                    await self.bot.redis_brain.client.set(last_check_key, str(now_ts), ex=172800)
                    await self._check_single_sub(g_name, sub)
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"❌ [SmartPoller] Hiatus check failed for {sub.get('series_title')}: {e}")

    @poll_loop.before_loop
    @high_freq_poll_loop.before_loop
    async def before_poll_loop(self):
        logger.debug("⏳ [AutoPoller] Waiting for bot to be ready before starting loop...")
        await self.bot.wait_until_ready()
        logger.debug("✅ [AutoPoller] Loop started.")

    async def _check_high_freq_targets(self):
        """Checks subscriptions for piccoma, jumptoon, and mecha specifically."""
        all_subs = get_all_subscriptions()
        if not all_subs: return

        today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
        targets = ["piccoma", "jumptoon", "mecha", "kakao"]
        
        todays_targets = []
        for group_name, sub in all_subs:
            if not sub: continue
            p_name = (sub.get("platform") or "").lower()
            # Normalize platform name
            if "mecha" in p_name: p_name = "mecha"
            elif "kakao" in p_name: p_name = "kakao"
            elif "jumptoon" in p_name: p_name = "jumptoon"
            elif "piccoma" in p_name: p_name = "piccoma"
            
            if p_name in targets and (sub.get("release_day") or "").lower() == today_name.lower():
                todays_targets.append((group_name, sub))

        if not todays_targets: return

        # Split Mecha batch from others
        mecha_targets = [t for t in todays_targets if "mecha" in t[1].get("platform", "").lower()]
        other_targets = [t for t in todays_targets if "mecha" not in t[1].get("platform", "").lower()]

        for group_name, sub in other_targets:
            try:
                date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                redis_key = f"verzue:poller:found:{sub['series_id']}:{date_str}"
                if await self.bot.redis_brain.client.get(redis_key): continue

                if await self._check_single_sub(group_name, sub):
                    await self.bot.redis_brain.client.set(redis_key, "1", ex=86400)
            except Exception as e:
                logger.error(f"❌ [AutoPoller] High-freq error for {sub.get('series_title')}: {e}")

        if mecha_targets:
            await self._check_mecha_batch(mecha_targets)

    async def _check_subscriptions(self):
        """Analyzes all subscriptions with concurrency limits."""
        if self.is_checking:
            logger.warning("🕒 [AutoPoller] Attempted to start poll while one is already in progress. Skipping.")
            return
        
        self.is_checking = True
        try:
            all_subs = get_all_subscriptions()
            if not all_subs: return

            today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
            todays_subs = []
            for group_name, sub in all_subs:
                if not sub or (sub.get("status") or "").lower() == "completed":
                    continue
                
                rel_day = (sub.get("release_day") or "").lower()
                if not rel_day or rel_day == today_name.lower():
                    todays_subs.append((group_name, sub))

            if not todays_subs: return

            mecha_targets = [t for t in todays_subs if "mecha" in t[1].get("platform", "").lower()]
            other_targets = [t for t in todays_subs if "mecha" not in t[1].get("platform", "").lower()]

            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            async def check_with_semaphore(group_name, sub):
                async with semaphore:
                    return await self._check_single_sub(group_name, sub)

            tasks_list = [check_with_semaphore(gn, sub) for gn, sub in other_targets]
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
            
            if mecha_targets:
                await self._check_mecha_batch(mecha_targets)

            found_new = sum(1 for r in results if r is True)
            logger.info(f"✅ [AutoPoller] Poll complete. New chapters found: {found_new}")
        finally:
            self.is_checking = False

    async def _check_mecha_batch(self, targets: list):
        """🟢 Specialized Mecha Batch Optimization using Alerts page."""
        try:
            sub_map = {sub["series_id"]: (gn, sub) for gn, sub in targets}
            provider = self.bot.task_queue.provider_manager.get_provider_for_url("https://mechacomic.jp")
            alerts = await provider.get_alerts_list()
            
            if not alerts: return
            updated_ids = {a["series_id"] for a in alerts}
            
            for sid, (gn, sub) in sub_map.items():
                if sid in updated_ids:
                    date_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                    redis_key = f"verzue:poller:found:{sid}:{date_str}"
                    if await self.bot.redis_brain.client.get(redis_key): continue

                    if await self._check_single_sub(gn, sub):
                        await self.bot.redis_brain.client.set(redis_key, "1", ex=86400)
        except Exception as e:
            logger.error(f"❌ [AutoPoller] Mecha Batch Error: {e}")

    async def _check_single_sub(self, group_name: str, sub: dict) -> bool:
        """Logic for analyzing a single series for updates."""
        try:
            logger.info(f"🔍 [AutoPoller] Checking {sub['series_title']} for {group_name}...")
            scraper = self.bot.task_queue.provider_manager.get_provider_for_url(sub["series_url"])
            data = await scraper.get_series_info(sub["series_url"])
            title, total_chapters, chapter_list, image_url, series_id, release_day, release_time, status_label, genre_label = data
            
            latest_chapter = chapter_list[-1] if chapter_list else None
            last_known = str(sub.get("last_known_chapter_id", "0"))
            current_id = str(latest_chapter["id"]) if latest_chapter else "0"
            
            # S-GRADE: Update release day if detected and missing
            if release_day and not sub.get("release_day"):
                from app.services.group_manager import set_release_day
                if set_release_day(group_name, sub["series_url"], release_day):
                    logger.info(f"📅 [AutoPoller] Auto-detected release day for {title}: {release_day}")
                    sub["release_day"] = release_day
            
            # S-GRADE: Status Auto-Detection
            new_status = status_label if status_label else "Weekly"
            if new_status != sub.get("status", "Weekly"):
                data_json = load_group(group_name)
                for s in data_json["subscriptions"]:
                    if s["series_id"] == series_id:
                        s["status"] = new_status
                        save_group(group_name, data_json)
                        logger.info(f"🏷️ [AutoPoller] Status updated: {title} → {new_status}")
                        break

            # ✅ Piccoma UP flag integration
            latest_is_up = latest_chapter.get('is_up', False) if latest_chapter else False
            up_not_yet_notified = latest_is_up and str(sub.get("last_up_chapter_id")) != current_id
            
            if latest_chapter and (current_id != last_known or up_not_yet_notified):
                trigger_reason = "ID change" if current_id != last_known else "UP flag"
                logger.info(f"🚨 [AutoPoller] NEW CHAPTER DETECTED ({trigger_reason})! {title} - {latest_chapter['notation']}")
                
                if current_id != last_known:
                    update_last_chapter(group_name, series_id, current_id)
                
                if up_not_yet_notified:
                    update_last_up_chapter(group_name, series_id, current_id)

                # Poster attachment
                files = []
                use_attachment_proxy = False
                if image_url:
                    from curl_cffi import requests as curl_requests
                    import io
                    try:
                        res = curl_requests.get(image_url, timeout=10, impersonate="chrome")
                        if res.status_code == 200:
                            files.append(discord.File(io.BytesIO(res.content), filename="poster.png"))
                            use_attachment_proxy = True
                    except Exception as e:
                        logger.error(f"Failed to attach image: {e}")

                await self.notifier._notify_channel(
                    group_name=group_name,
                    sub=sub,
                    series_title=title,
                    series_id=series_id,
                    image_url=image_url,
                    chapter_id=current_id,
                    chapter_number=latest_chapter.get('notation'),
                    files=files,
                    use_attachment_proxy=use_attachment_proxy
                )
                return True
            return False
        except Exception as e:
            logger.error(f"❌ [AutoPoller] Error checking {sub.get('series_title')}: {e}")
            return False

    async def trigger_manual_poll(self, ctx):
        """Allows admins to force a poll immediately."""
        msg = await ctx.send("🔄 **Forcing Subscription Poll...** (Ignoring day/time checks)")
        if self.is_checking:
            return await msg.edit(content="⚠️ **Poll already in progress.**")

        self.is_checking = True
        try:
            all_subs = get_all_subscriptions()
            if not all_subs: return await msg.edit(content="❌ No subscriptions found.")

            semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
            async def check_with_semaphore(group_name, sub):
                async with semaphore:
                    try:
                        # Re-use logic but without the today_name filter
                        return await self._check_single_sub(group_name, sub)
                    except: return False

            tasks_list = [check_with_semaphore(gn, sub) for gn, sub in all_subs]
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
            found_new = sum(1 for r in results if r is True)
            await msg.edit(content=f"✅ **Poll Complete!** Found {found_new} new chapter(s).")
        finally:
            self.is_checking = False
