import discord
from discord import app_commands
from discord.ext import commands
import logging
import re
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
        # We bypass discord.py's UI limits by sending the raw API payload described in the V2 Docs
        payload = {
            "type": 4, # MESSAGE_WITH_SOURCE
            "data": {
                "flags": 32768, # 🟢 THIS IS THE MAGIC FLAG (1 << 15) THAT ENABLES V2 COMPONENTS
                "components": [
                    {
                        "type": 17, # 📦 V2 CONTAINER
                        "components": [
                            {
                                "type": 10, # 📝 V2 TEXT DISPLAY
                                "content": f"# Dashboard of {scan_name}"
                            },
                            {
                                "type": 14, # ➖ V2 SEPARATOR
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
                                "type": 1, # ➡️ ACTION ROW
                                "components": [
                                    {
                                        "type": 3, # 📜 STRING SELECT
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
            # 🟢 CRITICAL: Tell discord.py we've answered so it doesn't try to double-ack and crash!
            interaction.response._responded = True
            
        except discord.NotFound:
            # This triggers if you use the command too fast on bot startup and it times out (> 3 seconds)
            logger.warning("[Dashboard] Interaction timed out. This is normal on startup. Try the command again!")
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Failed to launch V2 Dashboard. Check bot logs.", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """🟢 EVENT LISTENER: Catch raw V2 interactions that bypass the standard View system."""

        # 3. Handle Select Menu Click -> Launch V2 Modal
        if interaction.type == discord.InteractionType.component:
            if interaction.data.get("custom_id") == "v2_platform_select":
                platform = interaction.data["values"][0]

                # V2 Modal with RadioGroup and Labels
                modal_payload = {
                    "type": 9, # MODAL
                    "data": {
                        "custom_id": f"v2_modal_{platform}",
                        "title": f"{platform} Extractor",
                        "components": [
                            {
                                "type": 18, # 🏷️ V2 LABEL COMPONENT
                                "label": "Choose Action",
                                "component": {
                                    "type": 21, # 🔘 V2 RADIO GROUP
                                    "custom_id": "action_radio",
                                    "options": [
                                        {"label": "Download Chapters", "value": "download", "default": True},
                                        {"label": "Add Subscription", "value": "subscribe"}
                                    ],
                                    "required": True
                                }
                            },
                            {
                                "type": 18, # 🏷️ V2 LABEL COMPONENT
                                "label": f"Add {platform} link here:",
                                "component": {
                                    "type": 4, # ⌨️ TEXT INPUT
                                    "custom_id": "url_input",
                                    "style": 1,
                                    "placeholder": f"Paste {platform} URL...",
                                    "required": True
                                }
                            }
                        ]
                    }
                }

                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=modal_payload
                )

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
                
                # 1. Acknowledge and Defer instantly so the modal closes
                await interaction.response.defer(ephemeral=False)
                
                # 2. Send loading state
                loading_msg = await interaction.followup.send(f"🔍 **Analyzing {platform} Link:**\n`{url}`\n*Fetching metadata, please wait...*", wait=True)

                # 3. Fetch Metadata and Launch Dashboard
                try:
                    import uuid
                    import asyncio
                    from app.core.logger import req_id_context
                    from app.bot.common.view import UniversalDashboard

                    req_id = str(uuid.uuid4())[:8].upper()
                    token = req_id_context.set(req_id)
                    
                    # Fetch from Scraper Registry
                    scraper = self.bot.task_queue.scraper_registry.get_scraper(url)
                    data = await asyncio.to_thread(scraper.get_series_info, url)
                    
                    title, total_chapters, chapter_list, image_url, series_id = data
                    
                    ctx_data = {
                        'url': url,
                        'title': title,
                        'chapters': chapter_list,
                        'image_url': image_url,
                        'series_id': series_id,
                        'req_id': req_id,
                        'user': interaction.user
                    }
                    
                    # Map Platform Name to Service Color/Type
                    service_type = platform.lower().replace(" ", "").replace(".jp", "").replace("comic", "")
                    
                    # 4. Mount Universal Dashboard
                    view = UniversalDashboard(self.bot, ctx_data, service_type)
                    view.interaction = interaction
                    
                    await interaction.edit_original_response(content=None, embed=view.build_live_embed(), view=view)
                    
                    # 5. Speculative Browser Warmup (For Mecha)
                    if service_type == "mecha":
                        browser = self.bot.task_queue.scraper_registry.browser
                        asyncio.create_task(asyncio.to_thread(browser.warmup))
                        logger.info("🔥 Browser Warmup Triggered (Background)")

                except Exception as e:
                    logger.error(f"Failed to fetch metadata: {e}", exc_info=True)
                    error_text = str(e).splitlines()[0] if str(e) else "Unknown Error"
                    await interaction.edit_original_response(content=f"❌ **Extraction Failed**\nCould not fetch metadata for `{url}`.\n**Reason:** `{error_text}`")
                finally:
                    try:
                        req_id_context.reset(token)
                    except:
                        pass

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
