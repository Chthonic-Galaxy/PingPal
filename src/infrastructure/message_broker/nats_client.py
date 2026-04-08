import logging
from ssl import SSLContext

import nats.js.errors as js_errors
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext
from nats.js.api import KeyValueConfig, StorageType
from nats.js.kv import KeyValue

from src.config import Settings

logger = logging.getLogger(__name__)


class NATSManager:
    def __init__(self, nc: NATSClient, settings: Settings):
        self.nc = nc

        self.KV_BUCKET = settings.nats.kv_bucket

        self.settings = settings

        logger.debug("NATSManager has been initialyzed")

    async def get_jetstream(self) -> JetStreamContext:
        return self.nc.jetstream()

    async def get_kv(self) -> KeyValue:
        """Create (if needed) and return KV bucket."""
        logger.debug("Enter to get_kv method")
        js = self.nc.jetstream()
        logger.info("JetStream has been set up")

        try:
            return await js.key_value(self.KV_BUCKET)
        except js_errors.NotFoundError:
            logger.debug("JetStream Key Value Bucket hasn't been found")
            pass

        try:
            logger.debug("Trying to create JetStream Key Value Bucket")
            await js.create_key_value(
                KeyValueConfig(
                    bucket=self.KV_BUCKET,
                    description="PingPal site configuration (source of truth for Agents)",
                    storage=StorageType.FILE,
                    history=1,
                )
            )
        except js_errors.APIError as e:
            logger.error("Failed to create JetStream Key Value Bucket")
            if getattr(e, "err_code", None) != 10058:
                raise

        logger.debug("Return JetStream Key Value Bucket from get_kv method")
        return await js.key_value(self.KV_BUCKET)

    async def connect(
        self, servers: str | list[str], tls: None | SSLContext, name: str, **kwargs
    ):
        is_there_tls = tls or self.settings.ssl_context
        logger.info(
            "Connecting to the NATS. servers=%s, tls=%s, name=%s",
            servers,
            is_there_tls,
            name,
        )
        await self.nc.connect(
            servers=servers if servers else self.settings.nats.url,
            tls=tls if tls else self.settings.ssl_context,
            name=name,
            **kwargs,
        )

    async def disconnect(self):
        try:
            await self.nc.drain()
            logger.debug("NATS has been drained")
        except Exception:
            logger.exception("Failed to drain NATS")
            try:
                await self.nc.close()
                logger.debug("NATS has been closed")
            except Exception:
                logger.exception("Failed to close NATS")
