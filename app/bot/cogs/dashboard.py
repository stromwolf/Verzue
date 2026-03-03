import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
import uuid
import asyncio
from config.settings import Settings

logger = logging.getLogger("Dashboard")

class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="dashboard", description="Open the central extraction menu")
    async def dashboard(self, interaction: discord.Interaction):
        """Phase 1: Launch the V2 Dashboard using raw API payloads."""
        # 1. Gather Context
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        scan_name = Settings.SERVER_MAP.get(channel_id) or Settings.SERVER_MAP.get(guild_id) or Settings.DEFAULT_CLIENT_NAME

        # 2. Construct Raw V2 JSON Payload
        payload = {
            "type": 4, # MESSAGE_WITH_SOURCE
            "data": {
                "flags": 32768, # 🟢 MAGIC FLAG FOR V2 COMPONENTS
                "components": [
                    {
                        "type": 17, # CONTAINER
                        "components": [
                            {
                                "type": 10, # TEXT DISPLAY
                                "content": f"# Dashboard of {scan_name}"
                            },
                            {
                                "type": 14, # SEPARATOR
                                "divider": True,
                                "spacing": 1
                            },
                            {
                                "type": 10,
                                "content": "## Platform Lists\n**Available Platforms**\n> * <:Mechacomic:1478369141957333083> Mecha Comic\n> * <:Jumptoon:1478367963928068168> Jumptoon\n\n**Coming Soon Platforms**\n> * <:KakaoPage:1478366505640001566> KakaoPage\n> * <:KuaikanManhua:1478368412609679380> Kuaikan Manhua\n> * <:Piccoma:1478368704164134912> Piccoma\n> * <:acqq:1478369616660140082> AC.QQ"
                            },
                            {
                                "type": 10,
                                "content": "## Your Commands"
                            },
                            {
                                "type": 1, # ACTION ROW
                                "components": [
                                    {
                                        "type": 3, # SELECT
                                        "custom_id": "v2_platform_select",
                                        "placeholder": "Select Platform",
                                        "options": [
                                            {"label": "Mecha Comic", "value": "Mecha Comic", "emoji": {"id": "1478369141957333083", "name": "Mechacomic"}},
                                            {"label": "Jumptoon", "value": "Jumptoon", "emoji": {"id": "1478367963928068168", "name": "Jumptoon"}}
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        }

        # Send raw HTTP request to Discord
        try:
            route = discord.http.Route(
                'POST', '/interactions/{interaction_id}/{interaction_token}/callback',
                interaction_id=interaction.id,
                interaction_token=interaction.token
            )
            await self.bot.http.request(route, json=payload)
            
        except discord.NotFound:
            logger.warning("[Dashboard] Interaction timed out. This is normal on startup.")
        except discord.HTTPException as e:
            if e.code == 40060:
                pass # Already acknowledged (Double-click), ignore silently
            else:
                logger.error(f"Failed to send V2 Dashboard: {e}")
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions."""

        # 3. Handle Select Menu Click -> Launch V2 Modal
        if interaction.type == discord.InteractionType.component:
            if interaction.data.get("custom_id") == "v2_platform_select":
                platform = interaction.data["values"][0]

                modal_payload = {
                    "type": 9, # MODAL
                    "data": {
                        "custom_id": f"v2_modal_{platform}",
                        "title": f"{platform} Extractor",
                        "components": [
                            {
                                "type": 18, # LABEL
                                "label": "Choose Action",
                                "component": {
                                    "type": 21, # RADIO GROUP
                                    "custom_id": "action_radio",
                                    "options": [
                                        {"label": "Download Chapters", "value": "download", "default": True},
                                        {"label": "Add Subscription", "value": "subscribe"}
                                    ],
                                    "required": True
                                }
                            },
                            {
                                "type": 18, # LABEL
                                "label": f"Add {platform} link here:",
                                "component": {
                                    "type": 4, # TEXT INPUT
                                    "custom_id": "url_input",
                                    "style": 1,
                                    "placeholder": f"Paste {platform} URL...",
                                    "required": True
                                }
                            }
                        ]
                    }
                }

                try:
                    await self.bot.http.request(
                        discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                        json=modal_payload
                    )
                except discord.NotFound:
                    logger.warning("[Dashboard] Modal launch timed out.")
                except discord.HTTPException as e:
                    if e.code != 40060: logger.error(f"Modal launch error: {e}")

        # 4. Handle V2 Modal Submission
        elif interaction.type == discord.InteractionType.modal_submit:
            custom_id = interaction.data.get("custom_id", "")
            if custom_id.startswith("v2_modal_"):
                platform = custom_id.replace("v2_modal_", "")
                
                action = "download"
                url = ""

                # Extract values from the V2 Label > Component nesting
                for row in interaction.data.get("components", []):
                    inner = row.get("component", {})
                    cid = inner.get("custom_id")
                    if cid == "action_radio":
                        action = inner.get("value", "download") 
                    elif cid == "url_input":
                        url = inner.get("value", "")

                # 🛑 NEW: STRICT URL VALIDATION
                platform_domains = {
                    "Mecha Comic": "mechacomic.jp",
                    "Jumptoon": "jumptoon.com",
                    "KakaoPage": "kakao.com",
                    "Kuaikan Manhua": "kuaikanmanhua.com",
                    "Piccoma": "piccoma.com",
                    "AC.QQ": "ac.qq.com"
                }
                
                expected_domain = platform_domains.get(platform)
                if expected_domain and expected_domain not in url.lower():
                    error_payload = {
                        "type": 4, # MESSAGE_WITH_SOURCE
                        "data": {
                            "flags": 64, # EPHEMERAL (Invisible)
                            "content": f"⛔ **Protocol Violation**\nYou selected **{platform}**, but provided a link for a different site.\n\nPlease provide a valid `{expected_domain}` link."
                        }
                    }
                    try:
                        route = discord.http.Route('POST', '/interactions/{interaction_id}/{interaction_token}/callback', interaction_id=interaction.id, interaction_token=interaction.token)
                        await self.bot.http.request(route, json=error_payload)
                    except discord.NotFound:
                        pass
                    return # Stop processing

                # 🛑 PATH B: Subscription (Coming Soon)
                if action == "subscribe":
                    msg_payload = {
                        "type": 4, # MESSAGE_WITH_SOURCE
                        "data": {
                            "flags": 64, # EPHEMERAL
                            "content": f"🚧 **Subscription Feature Coming Soon!**\nWe are currently building the tracking database for **{platform}**."
                        }
                    }
                    try:
                        route = discord.http.Route('POST', '/interactions/{interaction_id}/{interaction_token}/callback', interaction_id=interaction.id, interaction_token=interaction.token)
                        await self.bot.http.request(route, json=msg_payload)
                    except discord.NotFound:
                        pass
                    return

                # 🟢 PATH A: Download (Bridge to Universal Dashboard)
                
                # 1. Create a NEW standard V1 response
                msg_target = interaction
                try:
                    await interaction.response.send_message(f"🔍 **Analyzing {platform} Link:**\n`{url}`\n*Fetching metadata, please wait...*")
                except discord.NotFound:
                    # 10062 Error: Fallback to channel message
                    if interaction.channel:
                        msg_target = await interaction.channel.send(f"🔍 **Analyzing {platform} Link for {interaction.user.mention}:**\n`{url}`\n*Fetching metadata...*")
                    else:
                        return 
                except discord.HTTPException as e:
                    if e.code == 40060: pass

                # 3. Fetch Metadata and Launch Dashboard
                try:
                    from app.core.logger import req_id_context
                    from app.bot.common.view import UniversalDashboard

                    req_id = str(uuid.uuid4())[:8].upper()
                    token = req_id_context.set(req_id)
                    
                    # 🟢 CRITICAL FIX: Force the API Scraper for Mecha!
                    # Prevents 'NoneType' Selenium crash.
                    is_smartoon = "mecha" in platform.lower()
                    scraper = self.bot.task_queue.scraper_registry.get_scraper(url, is_smartoon=is_smartoon)
                    
                    data = await asyncio.to_thread(scraper.get_series_info, url)
                    
                    title, total_chapters, chapter_list, image_url, series_id = data
                    
                    ctx_data = {
                        'url': url, 'title': title, 'chapters': chapter_list,
                        'image_url': image_url, 'series_id': series_id,
                        'req_id': req_id, 'user': interaction.user
                    }
                    
                    service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
                    
                    # 4. Mount Universal Dashboard in V2 Container Format
                    view = UniversalDashboard(self.bot, ctx_data, service_type)
                    view.interaction = interaction
                    
                    # Push the custom Container payload immediately using the webhook
                    await view.update_view()
                    
                    # 5. Speculative Browser Warmup (For Mecha)
                    if service_type == "mecha":
                        browser = self.bot.task_queue.scraper_registry.browser
                        asyncio.create_task(asyncio.to_thread(browser.warmup))
                        logger.info("🔥 Browser Warmup Triggered (Background)")

                except Exception as e:
                    logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
                    error_text = str(e).splitlines()[0] if str(e) else "Unknown Error"
                    
                    if hasattr(msg_target, 'edit_original_response'):
                        await msg_target.edit_original_response(content=f"❌ **Extraction Failed**\nCould not fetch metadata for `{url}`.\n**Reason:** `{error_text}`")
                    else:
                        await msg_target.edit(content=f"❌ **Extraction Failed for {interaction.user.mention}**\nCould not fetch metadata for `{url}`.\n**Reason:** `{error_text}`")
                finally:
                    try:
                        req_id_context.reset(token)
                    except:
                        pass

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
