import asyncio
import logging
import datetime
import traceback
import discord
from discord.ext import tasks

from app.services.group_manager import get_all_subscriptions, update_last_chapter
from app.models.chapter import ChapterTask

logger = logging.getLogger("AutoPoller")

class AutoDownloadPoller:
    def __init__(self, bot):
        self.bot = bot
        # Run daily at 15:00 UTC
        self.check_time = datetime.time(hour=15, minute=0, tzinfo=datetime.timezone.utc)
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()

    @tasks.loop(time=datetime.time(hour=15, minute=0, tzinfo=datetime.timezone.utc))
    async def poll_loop(self):
        """The main polling loop that fires at 15:00 UTC."""
        logger.info("🕒 [AutoPoller] Waking up for daily subscription check.")
        await self._check_subscriptions()

    @poll_loop.before_loop
    async def before_poll_loop(self):
        logger.info("⏳ [AutoPoller] Waiting for bot to be ready before starting loop...")
        await self.bot.wait_until_ready()
        logger.info("✅ [AutoPoller] Loop started and waiting for 15:00 UTC.")

    async def _check_subscriptions(self):
        """Iterates through all subscriptions and checks for new chapters."""
        all_subs = get_all_subscriptions()
        
        if not all_subs:
            logger.info("💤 [AutoPoller] No active subscriptions found. Going back to sleep.")
            return

        today_name = datetime.datetime.now(datetime.timezone.utc).strftime("%A")
        logger.info(f"📅 [AutoPoller] Today is {today_name}. Checking {len(all_subs)} total subs.")

        for group_name, sub in all_subs:
            try:
                # 1. Check if today is the designated release day
                release_day = sub.get("release_day")
                if not release_day or release_day.lower() != today_name.lower():
                    continue
                
                logger.info(f"🔍 [AutoPoller] Checking {sub['series_title']} for {group_name}...")
                
                # 2. Fetch the latest metadata using the appropriate scraper
                scraper = self.bot.task_queue.scraper_registry.get_scraper(
                    sub["series_url"], 
                    is_smartoon=("mecha" in sub["platform"].lower())
                )
                
                # Run the blocking network call in a thread to keep the bot responsive
                data = await asyncio.to_thread(scraper.get_series_info, sub["series_url"])
                title, total_chapters, chapter_list, image_url, series_id = data
                
                if not chapter_list:
                    continue
                
                # 3. Look for a brand new chapter
                # The chapter list is sorted historically. We examine the last chapter.
                latest_chapter = chapter_list[-1]
                last_known = str(sub.get("last_known_chapter_id", "0"))
                
                current_id = str(latest_chapter["id"])
                is_new_badge = latest_chapter.get("is_new", False)

                # The primary trigger: The ID is different from what we knew AND it has the UP badge
                # (Or if there's no UP badge logic for a certain site, just differing IDs)
                if current_id != last_known and (is_new_badge or sub["platform"].lower() != "jumptoon"):
                    logger.info(f"🚨 [AutoPoller] NEW CHAPTER DETECTED! {title} - {latest_chapter['notation']}")
                    
                    # 4. Update the stored known chapter right away so we don't trigger again
                    update_last_chapter(group_name, series_id, current_id)
                    
                    # 5. Notify the target channel with a Download button
                    await self._notify_channel(
                        sub["channel_id"], 
                        title, 
                        latest_chapter["notation"], 
                        series_id,
                        image_url
                    )

            except Exception as e:
                logger.error(f"❌ [AutoPoller] Error checking {sub.get('series_title')}: {e}")
                logger.debug(traceback.format_exc())

    async def _notify_channel(self, channel_id: int, title: str, chapter_str: str, series_id: str, image_url: str = None):
        """Sends a notification to the target channel with a Download button."""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
            
        embed = discord.Embed(
            title="🔔 New Chapter Detected!",
            description=f"**{title}** — {chapter_str}\n\nClick the button below to queue the download.",
            color=0xf1c40f # Yellow/Gold
        )
        if image_url:
            embed.set_thumbnail(url=image_url)
            
        view = discord.ui.View(timeout=None)
        button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="⬇️ Download Now",
            custom_id=f"poll_dl_{series_id}"
        )
        view.add_item(button)
            
        try:
            await channel.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Failed to send release notification to {channel_id}: {e}")

    # --- DEBUG COMMAND IMPL ---
    async def trigger_manual_poll(self, ctx):
        """Allows admins to force a poll immediately."""
        msg = await ctx.send("🔄 **Forcing Subscription Poll...** (Ignoring day/time checks)")
        
        all_subs = get_all_subscriptions()
        if not all_subs:
            return await msg.edit(content="❌ No subscriptions found globally.")
            
        found_new = 0
        for group_name, sub in all_subs:
            try:
                scraper = self.bot.task_queue.scraper_registry.get_scraper(
                    sub["series_url"], 
                    is_smartoon=("mecha" in sub["platform"].lower())
                )
                data = await asyncio.to_thread(scraper.get_series_info, sub["series_url"])
                title, _, chapter_list, _, series_id = data
                
                if not chapter_list: continue
                
                latest_chapter = chapter_list[-1]
                last_known = str(sub.get("last_known_chapter_id", "0"))
                current_id = str(latest_chapter["id"])
                
                if current_id != last_known:
                    update_last_chapter(group_name, series_id, current_id)
                    found_new += 1
                    await self._notify_channel(sub["channel_id"], title, latest_chapter["notation"], series_id, image_url)
                    
            except Exception as e:
                logger.error(f"Debug Poll Error: {e}")
                
        await msg.edit(content=f"✅ **Poll Complete!** Queued {found_new} new chapter(s).")
