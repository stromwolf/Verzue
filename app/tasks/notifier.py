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
from app.services.settings_service import SettingsService
from app.bot.common.notification_builder import (
    build_notification_payload,
    build_new_series_notification_payload,
    build_hiatus_notification_payload,
)
from app.core.logger import req_id_context, group_name_context, log_category_context

logger = logging.getLogger("AutoPoller.Notifier")

class PollerNotifier:
    def __init__(self, bot):
        self.bot = bot
        self.settings = SettingsService(bot.redis_brain.client)

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
        
        # --- Feature Flag Guard ---
        platform = (sub.get("platform") or "").lower()
        if not self.bot.app_state.is_enabled(f"notifications.{platform}", group=group_name):
            logger.debug(f"[Notifier] notifications.{platform} disabled for {group_name}, skipping.")
            return
        
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

            # 🟢 S-GRADE: User-specific title override (Redis)
            if sub.get("added_by"):
                s_settings = await self.settings.get_subscription_settings(int(sub["added_by"]), sub["series_id"])
                if s_settings.get("custom_title"):
                    custom_title = s_settings["custom_title"]

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
                
                if files:
                    # 🟢 FIX: Multipart form-data required for V2 Components with attachment:// references
                    import json as json_lib
                    from aiohttp import FormData
                    
                    form = FormData()
                    form.add_field("payload_json", json_lib.dumps(payload), content_type="application/json")
                    
                    for idx, f in enumerate(files):
                        f.fp.seek(0)  # Reset buffer pointer
                        form.add_field(
                            f"files[{idx}]",
                            f.fp,
                            filename=f.filename,
                            content_type="image/png"
                        )
                    
                    # Use the underlying session to send raw multipart data
                    # (HTTPClient.request doesn't handle the payload_json field required for files + components)
                    url = f"https://discord.com/api/v10{route.url}"
                    headers = {"Authorization": f"Bot {self.bot.http.token}"}
                    
                    async with self.bot.http._HTTPClient__session.post(url, data=form, headers=headers) as resp:
                        if resp.status not in (200, 201, 204):
                            body = await resp.text()
                            logger.error(f"❌ Failed to send notification: {resp.status} {body}")
                        else:
                            logger.info(f"📨 [AutoPoller] Notification sent with attachment for {series_title} (N-ID: {notification_id})")
                else:
                    await self.bot.http.request(route, json=payload)
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

        # --- Feature Flag Guard ---
        if not self.bot.app_state.is_enabled(f"notifications.{platform.lower()}", group="Global"):
            return

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
    async def _notify_hiatus(
        self,
        *,
        group_name: str,
        sub: dict,
        series_title: str,
        series_id: str,
        image_url: str | None = None,
    ):
        """Notifies subscription channel that a series has gone on hiatus."""
        # --- Feature Flag Guard ---
        platform = (sub.get("platform") or "").lower()
        if not self.bot.app_state.is_enabled(f"notifications.{platform}", group=group_name):
            return

        try:
            channel = self.bot.get_channel(sub["channel_id"])
            if not channel:
                logger.warning(f"[AutoPoller] Hiatus notify: channel {sub['channel_id']} not found.")
                return

            admin = get_admin_settings(group_name)
            role_id = admin.get("role_id")
            custom_title = get_title_override(group_name, sub["series_url"])

            # 🟢 S-GRADE: User-specific title override (Redis)
            if sub.get("added_by"):
                s_settings = await self.settings.get_subscription_settings(int(sub["added_by"]), sub["series_id"])
                if s_settings.get("custom_title"):
                    custom_title = s_settings["custom_title"]

            notification_id = get_next_notification_id(group_name)

            # Attach poster if available
            files = []
            use_attachment_proxy = False
            if image_url:
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    res = await loop.run_in_executor(
                        None, lambda: curl_requests.get(image_url, timeout=10, impersonate="chrome")
                    )
                    if res.status_code == 200:
                        files.append(discord.File(io.BytesIO(res.content), filename="poster.png"))
                        use_attachment_proxy = True
                except Exception as e:
                    logger.warning(f"[AutoPoller] Hiatus poster fetch failed (non-fatal): {e}")

            payload = build_hiatus_notification_payload(
                platform=sub["platform"],
                role_id=role_id,
                series_title=series_title,
                custom_title=custom_title,
                poster_url=image_url,
                series_url=sub["series_url"],
                series_id=series_id,
                notification_id=notification_id,
                use_attachment_proxy=use_attachment_proxy,
            )

            route = discord.http.Route('POST', '/channels/{channel_id}/messages', channel_id=channel.id)

            if files:
                import json as json_lib
                from aiohttp import FormData
                form = FormData()
                form.add_field("payload_json", json_lib.dumps(payload), content_type="application/json")
                for idx, f in enumerate(files):
                    f.fp.seek(0)
                    form.add_field(f"files[{idx}]", f.fp, filename=f.filename, content_type="image/png")
                url_str = f"https://discord.com/api/v10/channels/{channel.id}/messages"
                headers = {"Authorization": f"Bot {self.bot.http.token}"}
                async with self.bot.http._HTTPClient__session.post(url_str, data=form, headers=headers) as resp:
                    if resp.status not in (200, 201, 204):
                        logger.error(f"❌ Hiatus notify failed: {resp.status} {await resp.text()}")
            else:
                await self.bot.http.request(route, json=payload)

            logger.info(f"💤 [AutoPoller] Hiatus notification sent for {series_title} in {group_name}")

        except Exception as e:
            logger.error(f"❌ [AutoPoller] _notify_hiatus failed for {series_title}: {e}")

