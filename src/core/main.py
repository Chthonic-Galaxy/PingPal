from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import nats.errors
from fastapi import Depends, FastAPI
from nats.aio.client import Client as NATS
from nats.aio.subscription import Subscription as NATSSubscription
from nats.js import JetStreamContext
from nats.js.kv import KeyValue
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.dependencies import active_agents
from src.api.v1.router import router as router_v1
from src.config import settings
from src.core.security import get_api_key
from src.core.utils import _site_key, _site_value
from src.infrastructure.database.connection import async_session_factory, engine
from src.infrastructure.database.models import (
    Metric,
    Site,
)
from src.infrastructure.database.utils import wait_for_db
from src.infrastructure.logger import setup_logging
from src.infrastructure.message_broker.nats_client import NATSManager
from src.schemas import AgentHeartbeat, MetricPayload

setup_logging(settings.log_level)
logger = logging.getLogger("pingpal-core")


async def on_heartbeat_msg(msg):
    try:
        data = AgentHeartbeat.model_validate_json(msg.data)
        active_agents[data.agent_id] = data
    except Exception:
        logger.exception("Bad heartbeat")


async def _sync_kv_from_db(
    sessionmaker: async_sessionmaker[AsyncSession],
    kv: KeyValue,
) -> None:
    async with sessionmaker.begin() as session:
        sites = (await session.execute(select(Site))).scalars().all()

    db_ids = {s.id for s in sites}
    active_ids = {s.id for s in sites if s.is_active}

    for s in sites:
        key = _site_key(s.id)
        try:
            if s.is_active:
                await kv.put(key, _site_value(s))
            else:
                try:
                    await kv.delete(key)
                except Exception:
                    pass
        except Exception:
            logger.exception("KV sync failed for site_id=%s", s.id)

    try:
        keys = await kv.keys()
    except Exception:
        keys = None

    if keys:
        for key in keys:
            if not key.startswith(settings.nats.kv_key_prefix):
                continue
            raw_id = key[len(settings.nats.kv_key_prefix) :]
            try:
                site_id = UUID(raw_id)
            except Exception:
                continue

            if site_id not in db_ids or site_id not in active_ids:
                try:
                    await kv.delete(key)
                except Exception:
                    pass


async def _metrics_db_worker(
    sessionmaker: async_sessionmaker[AsyncSession],
    psub: JetStreamContext.PullSubscription,
    *,
    batch_size: int = 500,
    flush_interval_s: float = 1.0,
) -> None:
    async def _flush(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        async with sessionmaker() as session:
            try:
                await session.execute(insert(Metric), items)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Failed to insert metrics batch (size=%d)", len(items))
                raise

    while True:
        try:
            msgs = await psub.fetch(batch=batch_size, timeout=flush_interval_s)
            batch: list[dict[str, Any]] = []

            for msg in msgs:
                try:
                    payload = MetricPayload.model_validate_json(msg.data)
                    batch.append(
                        {
                            "time": payload.timestamp,
                            "site_id": payload.site_id,
                            "region": payload.region,
                            "status_code": payload.status_code,
                            "latency_ms": payload.latency_ms,
                            "error_message": payload.error_message,
                        }
                    )
                except Exception:
                    logger.exception("Failed to parse/queue metric message")

            try:
                await _flush(batch)
            except Exception:
                continue

            await asyncio.gather(*[msg.ack() for msg in msgs])
        except nats.errors.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB
    db_sessionmaker = async_session_factory

    await wait_for_db(engine)

    # NATS + KV
    nc = NATS()
    nats_mgr = NATSManager(nc, settings)
    await nats_mgr.connect(
        servers=[settings.nats.url],
        tls=settings.ssl_context,
        name="pingpal-core",
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,
    )

    js = await nats_mgr.get_jetstream()
    await js.add_stream(name="METRICS", subjects=[settings.nats.metrics_subject])

    nats_kv = await nats_mgr.get_kv()
    app.state.kv = nats_kv

    await _sync_kv_from_db(db_sessionmaker, nats_kv)

    # Metrics ingestion
    subscriptions: dict[str, NATSSubscription | JetStreamContext.PullSubscription] = {}
    # subscriptions["metrics"] = await nc.subscribe(
    #     settings.nats.metrics_subject, cb=on_metric_msg
    # )
    subscriptions["metrics"] = await js.pull_subscribe(
        settings.nats.metrics_subject, durable="metrics_db_writer"
    )
    subscriptions["agent_heartbeat"] = await nc.subscribe(
        settings.nats.agents_heartbeat_subject, cb=on_heartbeat_msg
    )
    worker_task = asyncio.create_task(
        _metrics_db_worker(db_sessionmaker, subscriptions["metrics"])
    )

    try:
        yield
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

        try:
            for sub in subscriptions.values():
                await sub.unsubscribe()
        except Exception:
            pass

        await nats_mgr.disconnect()

        await engine.dispose()


app = FastAPI(
    title="PingPal Core",
    lifespan=lifespan,
    dependencies=[Depends(get_api_key)],
)

app.include_router(router_v1)
