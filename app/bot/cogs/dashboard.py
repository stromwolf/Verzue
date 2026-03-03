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
                                "content": "## Platform Lists\n* **Available Platforms**\n * KakaoPage\n - Mecha Comic\n - Jumptoon\n - Kuaikan Manhua\n - Piccoma\n - AC.QQ\n* **Coming Soon Platforms**\n * Naver Webtoon\n - Line Manga"
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
                                            {"label": "KakaoPage", "value": "KakaoPage", "emoji": {"name": "🇰🇷"}},
                                            {"label": "Mecha Comic", "value": "Mecha Comic", "emoji": {"name": "🇯🇵"}},
                                            {"label": "Jumptoon", "value": "Jumptoon", "emoji": {"name": "🇰🇷"}},
                                            {"label": "Kuaikan", "value": "Kuaikan", "emoji": {"name": "🇨🇳"}},
                                            {"label": "Piccoma", "value": "Piccoma", "emoji": {"name": "🇯🇵"}},
                                            {"label": "AC.QQ", "value": "AC.QQ", "emoji": {"name": "🇨🇳"}}
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
            await self.bot.http.request(
                discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                json=payload
            )
        except Exception as e:
            logger.error(f"Failed to send V2 Dashboard: {e}")
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
                # Data structure: components: [ { type: 18, components: [ { type: 21, value: ... } ] }, ... ]
                # Actually, V2 interaction data for modals might differ. 
                # According to the pattern provided by the user:
                for row in interaction.data.get("components", []):
                    # In V2, the Label wraps the component
                    inner = row.get("components", [{}])[0]
                    cid = inner.get("custom_id")
                    if cid == "action_radio":
                        action = inner.get("value", "download") 
                    elif cid == "url_input":
                        url = inner.get("value", "")

                # Send an ephemeral confirmation
                msg_payload = {
                    "type": 4, # MESSAGE_WITH_SOURCE
                    "data": {
                        "flags": 64, # EPHEMERAL
                        "content": f"✅ **{platform} Request Received!**\n🔹 **Action:** `{action.title()}`\n🔗 **URL:** `{url}`"
                    }
                }
                
                await self.bot.http.request(
                    discord.http.Route('POST', f'/interactions/{interaction.id}/{interaction.token}/callback'),
                    json=msg_payload
                )

async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
