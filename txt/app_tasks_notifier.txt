import logging
import time
import discord
import io
from curl_cffi import requests as curl_requests

from app.services.group_manager import (
    get_admin_settings,
    get_title_override,
    get_next_notification_id,
)
from app.bot.common.notification_builder import (
    build_notification_payload,
    build_new_series_notification_payload,
)
from app.core.logger import req_id_context, group_name_context, log_category_context

logger = logging.getLogger("AutoPoller.Notifier")

class PollerNotifier:
    def __init__(self, bot):
        self.bot = bot

    async def _notify_channel(
        self,
        *,
        group_name: str,
        sub: dict,
        series_title: str,
        series_id: str,
        image_url: str | None = None,
        chapter_id: str | None = None,
        chapter_number: str | None = None,
        files: list | None = None,
        use_attachment_proxy: bool = False,
    ):
        """Sends a V2 Component notification to the target channel."""
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

            # Get custom title override (Vault)
            custom_title = get_title_override(group_name, sub["series_url"])
            
            # 🟢 S-GRADE: Default to Chapter Number if no override
            if not custom_title:
                custom_title = chapter_number

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
                chapter_number=chapter_number,
                use_attachment_proxy=use_attachment_proxy,
            )

            try:
                route = discord.http.Route(
                    'POST',
                    '/channels/{channel_id}/messages',
                    channel_id=channel.id,
                )
                await self.bot.http.request(route, json=payload, files=files)
                logger.info(f"📨 [AutoPoller] Notification sent for {series_title} (N-ID: {notification_id})")
            except Exception as e:
                logger.error(f"Failed to send release notification to {channel.id}: {e}")
        finally:
            req_id_context.reset(t1)
            group_name_context.reset(t2)
            log_category_context.reset(t3)

    async def _notify_new_series(self, series: dict, platform: str, channel_id: int):
        """Sends a 'New Series premiere' notification to the target channel."""
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
