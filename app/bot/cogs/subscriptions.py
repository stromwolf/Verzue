import discord
from discord.ext import commands
import logging
import asyncio
from typing import Optional

from config.settings import Settings
from app.services.group_manager import (
    load_group, 
    remove_subscription, 
    set_release_day,
    get_all_subscriptions,
    _group_filename
)
from app.models.chapter import ChapterTask

logger = logging.getLogger("SubscriptionsCog")

class SubscriptionsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        """Standard Security Gatekeeper"""
        if not Settings.ALLOWED_IDS:
            return True
        return ctx.author.id in Settings.ALLOWED_IDS or ctx.author.id == 1216284053049704600

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Catch button clicks for new chapter downloads."""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            
            if custom_id.startswith("poll_dl_"):
                series_id = custom_id.replace("poll_dl_", "")
                
                # 🟢 S-GRADE: Defer to transform the original message directly
                await interaction.response.defer()
                
                # 1. Find the subscription globally by series_id
                all_subs = get_all_subscriptions()
                target_sub = None
                target_group = None
                for group_name, sub in all_subs:
                    if sub.get("series_id") == series_id:
                        target_sub = sub
                        target_group = group_name
                        break
                        
                if not target_sub:
                    return await interaction.followup.send("❌ Could not find the subscription details for this series. It might have been deleted.", ephemeral=True)
                    
                try:
                    # 2. Re-fetch metadata
                    scraper = self.bot.task_queue.provider_manager.get_provider_for_url(target_sub["series_url"])
                    data = await scraper.get_series_info(target_sub["series_url"])
                    title, _, chapter_list, image_url, fetched_series_id, _, _, status_label, genre_label = data
                    
                    if not chapter_list:
                        return await interaction.followup.send("❌ No chapters found for this series.", ephemeral=True)
                        
                    latest_chapter = chapter_list[-1]
                    
                    # 3. Queue for Download via BatchController
                    from app.services.batch_controller import BatchController
                    from app.bot.common.view import UniversalDashboard
                    controller = BatchController(self.bot)
                    
                    req_id = f"SUB-{series_id[:4]}"
                    ctx_data = {
                        'url': target_sub["series_url"], 'title': title, 'chapters': chapter_list,
                        'image_url': image_url, 'req_id': req_id,
                        'series_id': series_id, 'user': interaction.user.id,
                        'status_label': status_label, 'genre_label': genre_label
                    }
                    view = UniversalDashboard(self.bot, ctx_data, target_sub["platform"])
                    view.interaction = interaction
                    view.processing_mode = True
                    view.phases = {"analyze": "loading", "purchase": "waiting", "download": "waiting"}
                    await view.update_view(interaction)

                    # 🟢 Folder Resolution & Task Preparation
                    # We want to download the LATEST chapter found
                    latest_idx = len(chapter_list) - 1
                    tasks = await controller.prepare_batch(
                        interaction=interaction,
                        selected_indices=[latest_idx],
                        all_chapters=chapter_list,
                        title=title,
                        url=target_sub["series_url"],
                        view_ref=view,
                        series_id=series_id
                    )
                    
                    if not tasks: return

                    for t in tasks:
                         t.source = "subscription"
                         await self.bot.task_queue.add_task(t)
                    
                    asyncio.create_task(view.monitor_tasks())
                    
                except Exception as e:
                    logger.error(f"Failed to process manual poll download for {series_id}")
                    await self.bot.dispatch_error(e, interaction=interaction)
                    if not interaction.response.is_done():
                        await interaction.response.send_message(f"Come <@1216284053049704600>. New Error", ephemeral=True)
                    else:
                        await interaction.followup.send(f"Come <@1216284053049704600>. New Error", ephemeral=True)
                    
    @commands.command(name="check_chapters", aliases=["check_subs", "force_poll"])
    async def force_subscription_poll(self, ctx, series_url: str = None):
        """
        Forces a manual chapter check.
        - No URL: checks all subscriptions.
        - With URL (jumptoon/piccoma/mecha): checks only that specific series.
        """
        # 🟢 FIX: Resolve poller through main_bot if running on HelperBot
        resolved_bot = self.bot if hasattr(self.bot, 'auto_poller') else getattr(self.bot, 'main_bot', self.bot)
        poller = getattr(resolved_bot, "auto_poller", None)
        if not poller:
            return await ctx.send("❌ `AutoPoller` not initialized.")

        # --- MODE A: No URL → Full sweep (original behavior) ---
        if not series_url:
            return await poller.trigger_manual_poll(ctx)

        # --- MODE B: URL provided → Single series check ---
        series_url = series_url.strip("<>")  # Strip Discord's auto-embed brackets if present

        msg = await ctx.send(f"🔍 **Checking chapters for:** `{series_url}`...")

        try:
            # 1. Find the matching subscription across all groups
            all_subs = get_all_subscriptions()
            target_sub = None
            target_group = None

            for group_name, sub in all_subs:
                sub_url = sub.get("series_url", "")
                # Normalize comparison: strip trailing slashes
                if sub_url.rstrip("/") == series_url.rstrip("/"):
                    target_sub = sub
                    target_group = group_name
                    break

            if not target_sub:
                return await msg.edit(content=f"❌ **No subscription found** for that URL.\nMake sure it's an exact match to a subscribed series.")

            # 2. Run the single sub check (same logic as the poller)
            title = target_sub.get("series_title", series_url)
            await msg.edit(content=f"🔄 **Forcing check for:** `{title}`...")

            found_new = await poller._check_single_sub(target_group, target_sub)

            if found_new:
                await msg.edit(content=f"✅ **New chapter found and notification sent for:** `{title}`")
            else:
                await msg.edit(content=f"📭 **No new chapter detected for:** `{title}`\n-# Already up to date.")

        except Exception as e:
            logger.error(f"[check_chapters] Error checking {series_url}: {e}", exc_info=True)
            await msg.edit(content=f"❌ **Error while checking:** `{e}`")

async def setup(bot):
    await bot.add_cog(SubscriptionsCog(bot))
