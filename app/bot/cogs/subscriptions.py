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

    def _get_current_group(self, ctx) -> Optional[str]:
        """Gets the group name mapped to this server."""
        guild_id = ctx.guild.id if ctx.guild else 0
        channel_id = ctx.channel.id
        group_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id)
        return group_name

    @commands.command(name="sub-day")
    async def sub_day(self, ctx, *, args: str = None):
        """Usage: $sub-day <Series URL>, <Day>"""
        if not args or ',' not in args:
            embed = discord.Embed(
                title="ℹ️ How to use `$sub-day`",
                description=(
                    "Sets the weekly release day for an auto-download subscription.\n"
                    "The bot will only poll for new chapters on this day.\n\n"
                    "**Format:**\n"
                    "`$sub-day <Series URL>, <Day>`\n\n"
                    "**Example:**\n"
                    "`$sub-day https://jumptoon.com/..., Tuesday`"
                ),
                color=0x3498db
            )
            return await ctx.send(embed=embed)

        try:
            series_url, day = [x.strip() for x in args.rsplit(',', 1)]
            
            # Clean Discord's <> auto-formatting if they pasted a raw link
            if series_url.startswith("<") and series_url.endswith(">"):
                series_url = series_url[1:-1]
                
            valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            day_cap = day.capitalize()
            
            if day_cap not in valid_days:
                return await ctx.send(f"❌ Invalid day: `{day}`. Please use a full weekday name (e.g., Monday).")

            group_name = self._get_current_group(ctx)
            if not group_name:
                return await ctx.send("❌ This server is not mapped to any Group Profile. Use `$cdn-menu` first.")

            updated = set_release_day(group_name, series_url, day_cap)
            if updated:
                embed = discord.Embed(
                    title="✅ Release Day Updated",
                    description=f"Subscription for `{series_url}` will now be checked every **{day_cap}**.",
                    color=0x2ecc71
                )
                await ctx.send(embed=embed)
            else:
                await ctx.send(f"❌ Could not find a subscription matching that URL in the {group_name} profile.")

        except Exception as e:
            logger.error(f"Error in sub-day: {e}")
            await ctx.send("❌ **Format error!** Please use: `$sub-day <URL>, Day`")

    @commands.command(name="sub-list")
    async def sub_list(self, ctx):
        """Lists all active subscriptions for the current group."""
        group_name = self._get_current_group(ctx)
        if not group_name:
            return await ctx.send("❌ This server is not mapped to any Group Profile. Use `$cdn-menu` first.")

        data = load_group(group_name)
        subs = data.get("subscriptions", [])

        if not subs:
            return await ctx.send(f"ℹ️ No active subscriptions for **{group_name}**.")

        desc = f"Active Subscriptions for **{group_name}**:\n\n"
        for sub in subs:
            day_str = sub.get('release_day') or '*Not Set*'
            desc += f"📚 **{sub['series_title']}** ({sub['platform']})\n"
            desc += f"├─ 🔗 **URL:** <{sub['series_url']}>\n"
            desc += f"├─ 📅 **Release Day:** {day_str}\n"
            desc += f"├─ 🔔 **Channel:** <#{sub['channel_id']}>\n"
            desc += f"└─ 🔖 **Tracking from:** Ch. {sub.get('last_known_chapter_id', 'Unknown')}\n\n"

        embed = discord.Embed(
            title="📡 Auto-Download Subscriptions",
            description=desc,
            color=0x3498db
        )
        await ctx.send(embed=embed)

    @commands.command(name="sub-remove", aliases=["sub-del", "unsub"])
    async def sub_remove(self, ctx, *, target_url: str = None):
        """Usage: $sub-remove <Series URL>"""
        if not target_url:
            return await ctx.send("❌ Please provide the series URL: `$sub-remove <Series URL>`")

        # Clean Discord's <> auto-formatting
        if target_url.startswith("<") and target_url.endswith(">"):
            target_url = target_url[1:-1]

        group_name = self._get_current_group(ctx)
        if not group_name:
            return await ctx.send("❌ This server is not mapped to any Group Profile.")

        removed = remove_subscription(group_name, target_url.strip())
        if removed:
            embed = discord.Embed(
                title="🗑️ Subscription Removed",
                description=f"Stopped tracking URL for {group_name}.",
                color=0xe74c3c
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Could not find an active subscription for that URL.")

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
                    # 2. Re-fetch metadata via scraper to get the latest chapter info
                    scraper = self.bot.task_queue.scraper_registry.get_scraper(
                        target_sub["series_url"], 
                        is_smartoon=("mecha" in target_sub["platform"].lower())
                    )
                    data = await asyncio.to_thread(scraper.get_series_info, target_sub["series_url"])
                    title, _, chapter_list, _, fetched_series_id = data
                    
                    if not chapter_list:
                        return await interaction.followup.send("❌ No chapters found for this series.", ephemeral=True)
                        
                    latest_chapter = chapter_list[-1]
                    
                    # 3. Create the downward task
                    task = ChapterTask(
                        id=len(chapter_list), # Rough sequence
                        title=latest_chapter["title"],
                        chapter_str=latest_chapter["notation"],
                        url=target_sub["series_url"],
                        series_title=title,
                        req_id=f"MANUAL_POLL_{series_id}",
                        series_id_key=series_id,
                        episode_id=str(latest_chapter["id"]),
                        requester_id=interaction.user.id, # Record who clicked it!
                        channel_id=target_sub["channel_id"],
                        guild_id=0,
                        guild_name=target_group,
                        scan_group=target_group,
                        is_smartoon=("mecha" in target_sub["platform"].lower())
                    )
                    
                    await self.bot.task_queue.add_task(task)
                    
                    # 4. Edit the original message to remove the button and show Queued status
                    embed = interaction.message.embeds[0]
                    embed.description = embed.description.replace("Click the button below to queue the download.", f"✅ **Download Queued by <@{interaction.user.id}>!**")
                    embed.color = 0x2ecc71 # Green
                    
                    await interaction.message.edit(embed=embed, view=None)
                    
                except Exception as e:
                    logger.error(f"Failed to process manual poll download for {series_id}: {e}")
                    await interaction.followup.send(f"❌ Failed to queue download: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(SubscriptionsCog(bot))
