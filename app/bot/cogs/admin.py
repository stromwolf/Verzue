import discord
from discord import app_commands
from discord.ext import commands
import sys
import os
import signal
import asyncio
import logging
from pathlib import Path
from config.settings import Settings

logger = logging.getLogger("AdminCog")
PID_FILE = Path("bot.pid")

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        """
        Security Gatekeeper: Only allows users in ALLOWED_IDS 
        to run these commands.
        """
        if not Settings.ALLOWED_IDS:
            return True # Open access if no IDs are set (Dev Mode)
            
        return ctx.author.id in Settings.ALLOWED_IDS or ctx.author.id == 1216284053049704600

    @commands.command(name="cdn-menu")
    async def cdn_menu(self, ctx, *, args: str = None):
        """Usage: $cdn-menu [Group Name], [Server/Channel ID]"""
        
        # 1. Admin & Allowed Users Security Check
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        
        if not (is_owner or is_allowed):
            return await ctx.send("\u274c You are not authorized to use this command.", delete_after=60.0)

        # Automatically delete the user's $cdn-menu message after 60 seconds
        try:
            await ctx.message.delete(delay=60.0)
        except Exception:
            pass

        # 2. Show Guide if no arguments are provided
        if not args:
            profiles = sorted(Settings.GROUP_PROFILES)
            prof_list = "\n".join(f"\u2022 `{p}`" for p in profiles) if profiles else "\u2022 *No profiles created yet. Use `$group-add` first.*"
            guide_embed = discord.Embed(
                title="\u2139\ufe0f How to use `$cdn-menu`",
                description=(
                    "Links a Server or Channel ID to an existing Group Profile.\n"
                    "The `/dashboard` title for that server will update to match.\n\n"
                    "**Format:**\n"
                    "`$cdn-menu <Group Name>, <Server/Channel ID>`\n\n"
                    f"**Available Groups:**\n{prof_list}\n\n"
                    "**Tips:**\n"
                    "\u2022 Create a new group first with `$group-add <Name>`.\n"
                    "\u2022 View all mappings with `$group-list`.\n\n"
                    "*(This message will self-destruct in 60 seconds)*"
                ),
                color=0x3498db
            )
            return await ctx.send(embed=guide_embed, delete_after=60.0)

        # 3. Process the setup if arguments are provided
        try:
            if ',' in args:
                group_name, target_id_str = args.rsplit(',', 1)
            else:
                group_name, target_id_str = args.rsplit(' ', 1)
                
            group_name = group_name.strip()
            target_id = int(target_id_str.strip())

            # 4. \ud83d\udfe2 VALIDATE: Only allow group names that were pre-registered via $group-add
            if group_name not in Settings.GROUP_PROFILES:
                known = ", ".join(f"`{p}`" for p in sorted(Settings.GROUP_PROFILES)) or "*None yet. Use `$group-add` to create one.*"
                err_embed = discord.Embed(
                    title="\u274c Unknown Group Profile",
                    description=(
                        f"**`{group_name}`** is not a registered group profile.\n\n"
                        f"**Available Groups:**\n{known}\n\n"
                        "Use `$group-add <Name>` to create a new profile first."
                    ),
                    color=0xe74c3c
                )
                return await ctx.send(embed=err_embed, delete_after=60.0)
            
            # Update memory and save to file
            Settings.SERVER_MAP[target_id] = group_name
            Settings.save_server_map()
            
            success_embed = discord.Embed(
                title="\u2705 Server Linked",
                description=f"ID `{target_id}` is now linked to **{group_name}**.\nThe `/dashboard` will say *Dashboard of {group_name}* here.\n\n*(This message will self-destruct in 60 seconds)*",
                color=0x2ecc71
            )
            await ctx.send(embed=success_embed, delete_after=60.0)
            logger.info(f"Dashboard mapping updated: {target_id} -> {group_name} by {ctx.author}")
            
        except ValueError:
            await ctx.send("\u274c **Format error!** Please use: `$cdn-menu Group Name, ServerID`", delete_after=60.0)

    @commands.command(name="group-add")
    async def group_add(self, ctx, *, name: str = None):
        """Usage: $group-add [Group Name]. Registers a new Group Profile."""
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        
        if not (is_owner or is_allowed):
            return await ctx.send("\u274c You are not authorized to use this command.", delete_after=60.0)

        try:
            await ctx.message.delete(delay=60.0)
        except Exception:
            pass

        if not name:
            guide_embed = discord.Embed(
                title="\u2139\ufe0f How to use `$group-add`",
                description=(
                    "Creates a new Group Profile that can then be assigned to servers via `$cdn-menu`.\n\n"
                    "**Format:**\n"
                    "`$group-add <Group Name>`\n\n"
                    "**Example:**\n"
                    "`$group-add Thunder Scan`\n\n"
                    "*(This message will self-destruct in 60 seconds)*"
                ),
                color=0x3498db
            )
            return await ctx.send(embed=guide_embed, delete_after=60.0)

        name = name.strip()
        if name in Settings.GROUP_PROFILES:
            return await ctx.send(
                embed=discord.Embed(
                    title="\u2139\ufe0f Already Exists",
                    description=f"A profile named **{name}** already exists.",
                    color=0xf39c12
                ),
                delete_after=60.0
            )

        Settings.GROUP_PROFILES.add(name)
        Settings.save_group_profiles()

        # 🟢 Create the subscription profile JSON for this group
        from app.services.group_manager import ensure_group_file
        ensure_group_file(name)

        embed = discord.Embed(
            title="\u2705 Group Profile Created",
            description=(
                f"Profile **{name}** has been registered.\n\n"
                "You can now link it to a server using:\n"
                f"`$cdn-menu {name}, <Server/Channel ID>`"
            ),
            color=0x2ecc71
        )
        await ctx.send(embed=embed, delete_after=60.0)
        logger.info(f"Group Profile created: '{name}' by {ctx.author}")

    @commands.command(name="group-list")
    async def group_list(self, ctx):
        """Displays all registered Group Profiles and their IDs."""
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        
        if not (is_owner or is_allowed):
            return await ctx.send("❌ You are not authorized to use this command.", delete_after=60.0)

        if not Settings.SERVER_MAP:
            return await ctx.send("ℹ️ No group profiles registered yet.")

        desc = "## Registered Group Profiles\n"
        for target_id, name in Settings.SERVER_MAP.items():
            desc += f"> • **{name}**: `{target_id}`\n"
        
        embed = discord.Embed(
            title="📋 Group Profile Registry",
            description=desc,
            color=0x3498db
        )
        embed.set_footer(text=f"Default: {Settings.DEFAULT_CLIENT_NAME}")
        await ctx.send(embed=embed)

    @commands.command(name="set-admin")
    async def set_admin(self, ctx, *, args: str = None):
        """Usage: $set-admin <Channel ID>, [Role ID]"""
        is_owner = ctx.author.id == 1216284053049704600
        is_allowed = ctx.author.id in Settings.CDN_ALLOWED_USERS
        
        if not (is_owner or is_allowed):
            return await ctx.send("❌ You are not authorized to use this command.", delete_after=60.0)

        if not args:
            embed = discord.Embed(
                title="ℹ️ How to use `$set-admin`",
                description=(
                    "Sets the admin notification channel for the group profile linked to this server.\n"
                    "Alerts will be sent here when new subscriptions are added.\n\n"
                    "**Format:**\n"
                    "`$set-admin <Channel ID>, [Role ID]`\n\n"
                    "**Example:**\n"
                    "`$set-admin 123456789, 987654321` (Role ID is optional)"
                ),
                color=0x3498db
            )
            return await ctx.send(embed=embed)

        try:
            guild_id = ctx.guild.id if ctx.guild else 0
            channel_id_origin = ctx.channel.id
            group_name = Settings.SERVER_MAP.get(channel_id_origin) or Settings.SERVER_MAP.get(guild_id)

            if not group_name:
                return await ctx.send("❌ This server is not mapped to any Group Profile. Use `$cdn-menu` first.")

            role_id = None
            if ',' in args:
                chan_id_str, role_id_str = args.split(',', 1)
                admin_channel_id = int(chan_id_str.strip())
                role_id = int(role_id_str.strip())
            else:
                admin_channel_id = int(args.strip())

            from app.services.group_manager import set_admin_settings
            set_admin_settings(group_name, admin_channel_id, role_id)

            embed = discord.Embed(
                title="✅ Admin Channel Set",
                description=(
                    f"New subscription alerts for **{group_name}** will be sent to <#{admin_channel_id}>.\n"
                    + (f"Pinging role: <@&{role_id}>" if role_id else "No role ping set.")
                ),
                color=0x2ecc71
            )
            await ctx.send(embed=embed)

        except ValueError:
            await ctx.send("❌ **Format error!** Please use: `$set-admin <Channel ID>, [Role ID]`")
        except Exception as e:
            logger.error(f"Error in set-admin: {e}")
            await ctx.send(f"❌ **Error:** {e}")

    @commands.command(name="allow-cdn")
    async def allow_cdn(self, ctx, user_id: int):
        """Usage: $allow-cdn [User ID]. Grants/Revokes access to the $cdn-menu command."""
        # Only the main owner can run this
        if ctx.author.id != 1216284053049704600:
            return await ctx.send("❌ You are not authorized to use this command.", delete_after=60.0)

        if user_id in Settings.CDN_ALLOWED_USERS:
            Settings.CDN_ALLOWED_USERS.remove(user_id)
            Settings.save_cdn_users()
            action = "revoked from"
        else:
            Settings.CDN_ALLOWED_USERS.add(user_id)
            Settings.save_cdn_users()
            action = "granted to"

        embed = discord.Embed(
            title="🔐 CDN Access Updated",
            description=f"Access to `$cdn-menu` has been **{action}** user ID `{user_id}`.",
            color=0x3498db
        )
        await ctx.send(embed=embed)

    @commands.command(name="sync")
    async def sync_commands(self, ctx):
        """Forces a global sync of slash commands."""
        msg = await ctx.send("🔄 **Syncing slash commands...**")
        try:
            # Syncs all app_commands (Slash Commands) to Discord
            synced = await self.bot.tree.sync()
            await msg.edit(content=f"✅ **Success!** Synced {len(synced)} commands globally.")
            logger.info(f"Manual Sync: {len(synced)} commands synced by {ctx.author}")
        except Exception as e:
            await msg.edit(content=f"❌ **Sync failed:** `{e}`")

    @commands.command(name="restart", aliases=["reboot", "reset"])
    async def restart_bot(self, ctx):
        """Clean shutdown of services and process reboot."""
        msg = await ctx.send("🔄 **Initiating System Reboot...**")
        
        try:
            # 1. SHUT DOWN BROWSER & POLLER
            logger.info("Reboot: Terminating Browser Engine (if any)...")
            browser = self.bot.task_queue.scraper_registry.browser
            if browser and hasattr(browser, 'stop'):
                if asyncio.iscoroutinefunction(browser.stop):
                    await browser.stop()
                else:
                    browser.stop()
                    
            if hasattr(self.bot, 'auto_poller'):
                logger.info("Reboot: Canceling Auto-Download Poller...")
                try:
                    self.bot.auto_poller.poll_loop.cancel()
                except Exception as e:
                    logger.debug(f"Reboot: Poller cancel note: {e}")
            
            # 2. KILL ANY PREVIOUSLY SAVED PID (stale instances)
            if PID_FILE.exists():
                try:
                    old_pid = int(PID_FILE.read_text().strip())
                    if old_pid != os.getpid():  # Don't kill ourselves yet
                        if os.name == 'nt':
                            # Windows alternative to SIGTERM
                            os.system(f"taskkill /F /PID {old_pid}")
                        else:
                            os.kill(old_pid, signal.SIGTERM)
                        logger.info(f"Reboot: Killed stale instance PID {old_pid}")
                except (ProcessLookupError, ValueError, Exception) as e:
                    logger.debug(f"Reboot: Stale instance PID cleanup note: {e}")
                PID_FILE.unlink(missing_ok=True)

            # 3. UPDATE UI
            await msg.edit(content="👋 **Services stopped. Rebooting now...**")
            await asyncio.sleep(1)

            # 4. EXECUTE RESTART IN SAME CONSOLE
            logger.info(f"Reboot: Process re-executing by {ctx.author}")
            
            import subprocess
            # Spawn the new bot in the exact same terminal window
            subprocess.Popen([sys.executable] + sys.argv)
            
            # Cleanly disconnect this old bot from Discord
            await self.bot.close()
            sys.exit(0)
            
        except Exception as e:
            logger.error(f"Restart Failed: {e}")
            await msg.edit(content=f"❌ **Restart failed:** `{e}`")

    @commands.command(name="ping")
    async def ping(self, ctx):
        """Basic health check for the bot and event loop."""
        latency = round(self.bot.latency * 1000)
        
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"**Latency:** `{latency}ms`\n**Status:** `Operational`",
            color=0x2ecc71
        )
        # Check Redis connection via the task queue
        from app.services.redis_manager import RedisManager
        is_redis = await RedisManager().check_connection()
        
        embed.add_field(name="Global Brain", value="✅ Connected" if is_redis else "❌ Disconnected", inline=True)
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AdminCog(bot))