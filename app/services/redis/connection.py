import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError
import logging
import time
import socket
from config.settings import Settings
from app.core.events import EventBus
from app.core.lua_scripts import TOKEN_BUCKET_SCRIPT

logger = logging.getLogger("RedisManager.Connection")

class RedisConnection:
    """
    Handles the physical Redis connection, pooling, and resilience logic.
    """
    def __init__(self, manager):
        self.manager = manager
        self.client = None
        self.pool = None
        self._lua_limiter = None
        self._is_connected = True
        self._setup()

    def _setup(self):
        try:
            logger.info(f"🔌 Connecting to Redis: {Settings.REDIS_URL}")
            retry_strategy = Retry(ExponentialBackoff(cap=10, base=1), 5)
            
            self.pool = redis.ConnectionPool.from_url(
                Settings.REDIS_URL, 
                decode_responses=True, 
                max_connections=50,
                retry=retry_strategy,
                retry_on_timeout=True,
                retry_on_error=[ConnectionError, TimeoutError],
                socket_keepalive=True,
                socket_keepalive_options={
                    socket.TCP_KEEPIDLE: 60,
                    socket.TCP_KEEPINTVL: 10,
                    socket.TCP_KEEPCNT: 3,
                }
            )
            self.client = redis.Redis(connection_pool=self.pool)
        except Exception as e:
            logger.critical(f"Redis Setup Failed: {e}")
            self.client = None

    async def _handle_connection_status(self, is_success: bool):
        if is_success:
            if not self._is_connected:
                logger.info("📡 [Redis] RECONNECTED: Connection restored.")
                self._is_connected = True
                await EventBus.emit("redis_connected", {})
        else:
            if self._is_connected:
                logger.error("🚨 [Redis] DISCONNECTED: Connection lost. Hibernating services...")
                self._is_connected = False
                await EventBus.emit("redis_lost", {})

    async def check_connection(self):
        if not self.client: return False
        try: 
            status = await self.client.ping()
            await self._handle_connection_status(True)
            return status
        except (ConnectionError, TimeoutError):
            await self._handle_connection_status(False)
            return False
        except: return False

    async def get_token(self, bucket_name: str, rate: int = 40, capacity: int = 50):
        if not self.client: return True, 0
        try:
            if not self._lua_limiter:
                self._lua_limiter = self.client.register_script(TOKEN_BUCKET_SCRIPT)
            bucket_key = "global_ui_limit" if "discord_ui" in bucket_name else bucket_name
            result = await self._lua_limiter(keys=[f"limiter:{bucket_key}"], args=[capacity, rate, time.time()])
            await self._handle_connection_status(True)
            return result[0] == 1, result[1]
        except (ConnectionError, TimeoutError):
            await self._handle_connection_status(False)
            return True, 0
        except Exception as e:
            logger.error(f"Redis Limiter Error: {e}")
            return True, 0
