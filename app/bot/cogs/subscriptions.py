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
                
                # Defer the interaction since scraping might take a few seconds
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
                    title, _, chapter_list, _, fetched_series_id = data
                    
                    if not chapter_list:
                        return await interaction.followup.send("❌ No chapters found for this series.", ephemeral=True)
                        
                    latest_chapter = chapter_list[-1]
                    
                    # 3. Create the download task
                    task = ChapterTask(
                        id=len(chapter_list),
                        title=latest_chapter["title"],
                        chapter_str=latest_chapter["notation"],
                        url=target_sub["series_url"],
                        series_title=title,
                        req_id=f"MANUAL_POLL_{series_id}",
                        series_id_key=series_id,
                        episode_id=str(latest_chapter["id"]),
                        requester_id=interaction.user.id,
                        channel_id=target_sub["channel_id"],
                        guild_id=0,
                        guild_name=target_group,
                        scan_group=target_group,
                        is_smartoon=("mecha" in target_sub["platform"].lower())
                    )
                    
                    await self.bot.task_queue.add_task(task)
                    
                    # 4. Edit the original V2 message to show Queued status
                    #    The notification uses V2 Components (flag 32768), not embeds.
                    queued_payload = {
                        "flags": 32768,
                        "components": [{
                            "type": 17,  # Container
                            "accent_color": 0x2ecc71,  # Green = success
                            "components": [
                                {"type": 10, "content": f"### <a:done_subscription:1482425914456281108> Download Queued"},
                                {"type": 14, "divider": True, "spacing": 1},
                                {"type": 10, "content": f"**{title}** — {latest_chapter['notation']}"},
                                {"type": 14, "divider": True, "spacing": 1},
                                {"type": 10, "content": f"-# Queued by <@{interaction.user.id}> | S-ID: {series_id}"},
                            ]
                        }]
                    }
                    
                    route = discord.http.Route(
                        'PATCH',
                        '/channels/{channel_id}/messages/{message_id}',
                        channel_id=interaction.channel_id,
                        message_id=interaction.message.id,
                    )
                    await self.bot.http.request(route, json=queued_payload)
                    
                except Exception as e:
                    logger.error(f"Failed to process manual poll download for {series_id}: {e}")
                    await interaction.followup.send(f"❌ Failed to queue download: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(SubscriptionsCog(bot))
