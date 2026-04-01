import discord
import psutil
import logging
import asyncio
from discord.ext import commands, tasks
from datetime import datetime

logger = logging.getLogger("SystemMonitor")

class SystemMonitorCog(commands.Cog):
    """Cog for monitoring system resources (CPU, RAM, Disk) every 30 seconds."""
    def __init__(self, bot):
        self.bot = bot
        self.MONITOR_CHANNEL_ID = 1488728756054134946
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    @tasks.loop(seconds=30)
    async def monitor_loop(self):
        """Periodic task to check system resources and send a message."""
        try:
            # 1. Gather Metrics
            cpu_usage = psutil.cpu_percent(interval=1)
            
            ram = psutil.virtual_memory()
            ram_used = ram.used / (1024 ** 3)  # GB
            ram_total = ram.total / (1024 ** 3)  # GB
            ram_percent = ram.percent
            
            disk = psutil.disk_usage('/')
            disk_used = disk.used / (1024 ** 3)  # GB
            disk_total = disk.total / (1024 ** 3)  # GB
            disk_percent = disk.percent

            # 2. Build Embed
            embed = discord.Embed(
                title="🖥️ System Resource Monitor",
                color=self._get_color(cpu_usage, ram_percent, disk_percent),
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="🧠 CPU Usage",
                value=f"```\n{cpu_usage}% borrowed logic\n```",
                inline=True
            )
            
            embed.add_field(
                name="📟 RAM Usage",
                value=f"```\n{ram_percent}% ({ram_used:.1f}GB / {ram_total:.1f}GB)\n```",
                inline=True
            )
            
            embed.add_field(
                name="💽 Disk Space",
                value=f"```\n{disk_percent}% ({disk_used:.1f}GB / {disk_total:.1f}GB)\n```",
                inline=False
            )
            
            embed.set_footer(text="Updates every 30 seconds")

            # 3. Send Message
            channel = self.bot.get_channel(self.MONITOR_CHANNEL_ID)
            if not channel:
                channel = await self.bot.fetch_channel(self.MONITOR_CHANNEL_ID)
            
            if channel:
                await channel.send(embed=embed)
            else:
                logger.error(f"❌ [SystemMonitor] Could not find monitor channel: {self.MONITOR_CHANNEL_ID}")

        except Exception as e:
            logger.error(f"❌ [SystemMonitor] Failed to gather or send metrics: {e}")

    @monitor_loop.before_loop
    async def before_monitor_loop(self):
        """Wait for the bot to be ready before starting the loop."""
        await self.bot.wait_until_ready()

    def _get_color(self, cpu, ram, disk):
        """Determine embed color based on resource pressure."""
        if cpu > 90 or ram > 90 or disk > 95:
            return 0xe74c3c  # Red
        if cpu > 70 or ram > 70 or disk > 85:
            return 0xf1c40f  # Yellow
        return 0x2ecc71  # Green

async def setup(bot):
    await bot.add_cog(SystemMonitorCog(bot))
