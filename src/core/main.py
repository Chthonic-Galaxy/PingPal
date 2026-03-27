from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

import nats.js.errors as js_errors
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from nats.aio.client import Client as NATS
from nats.aio.subscription import Subscription as NATSSubscription
from nats.js.api import KeyValueConfig, StorageType
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.config import settings
from src.core.security import get_api_key
from src.database import (
    Metric,
    Site,
    build_db_settings,
    create_engine,
    create_sessionmaker,
    wait_for_db,
)
from src.schemas import AgentHeartbeat, AgentStatusOut, MetricPayload, SiteConfig

logger = logging.getLogger("pingpal-core")
logging.basicConfig(level=settings.log_level)

KV_BUCKET = "pingpal_config"
KV_KEY_PREFIX = "site."
NATS_METRICS_SUBJECT = "pingpal.metrics.ingest"

active_agents: dict[str, AgentHeartbeat] = {}  # TODO: replace by Redis


async def on_heartbeat_msg(msg):
    try:
        data = AgentHeartbeat.model_validate_json(msg.data)
        active_agents[data.agent_id] = data
    except Exception:
        logger.exception("Bad heartbeat")


def _site_key(site_id: UUID) -> str:
    return f"{KV_KEY_PREFIX}{site_id}"


def _site_value(site: Site) -> bytes:
    payload = SiteConfig(
        site_id=str(site.id),  # pyright: ignore[reportArgumentType]
        url=site.url,  # pyright: ignore[reportArgumentType]
        interval=site.interval,
        is_active=site.is_active,
        regions=site.regions,
    )
    return payload.model_dump_json().encode("utf-8")


async def _ensure_kv_bucket(nc: NATS):
    """
    Create (if needed) and return KV bucket.
    No BucketAlreadyExistsError in nats-py: use NotFoundError + APIError(err_code).
    """
    js = nc.jetstream()

    try:
        return await js.key_value(KV_BUCKET)
    except js_errors.NotFoundError:
        pass

    try:
        await js.create_key_value(
            KeyValueConfig(
                bucket=KV_BUCKET,
                description="PingPal site configuration (source of truth for Agents)",
                storage=StorageType.FILE,
                history=1,
            )
        )
    except js_errors.APIError as e:
        if getattr(e, "err_code", None) != 10058:
            raise

    return await js.key_value(KV_BUCKET)


async def _sync_kv_from_db(
    sessionmaker: async_sessionmaker[AsyncSession],
    kv,
) -> None:
    async with sessionmaker() as session:
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
            if not key.startswith(KV_KEY_PREFIX):
                continue
            raw_id = key[len(KV_KEY_PREFIX) :]
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
    queue: asyncio.Queue[dict[str, Any]],
    *,
    batch_size: int = 500,
    flush_interval_s: float = 1.0,
) -> None:
    batch: list[dict[str, Any]] = []

    async def flush(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        async with sessionmaker() as session:
            try:
                await session.execute(insert(Metric), items)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Failed to insert metrics batch (size=%d)", len(items))

    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=flush_interval_s)
            batch.append(item)
            queue.task_done()

            if len(batch) >= batch_size:
                await flush(batch)
                batch.clear()
        except asyncio.TimeoutError:
            if batch:
                await flush(batch)
                batch.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB
    db_settings = build_db_settings()
    engine: AsyncEngine = create_engine(db_settings)
    sessionmaker = create_sessionmaker(engine)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker

    await wait_for_db(engine)

    # NATS + KV
    nats_url = settings.nats_url
    nc = NATS()
    await nc.connect(
        servers=[nats_url],
        tls=settings.ssl_context,
        name="pingpal-core",
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,
    )
    app.state.nats = nc

    kv = await _ensure_kv_bucket(nc)
    app.state.kv = kv

    await _sync_kv_from_db(sessionmaker, kv)

    # Metrics ingestion
    metrics_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50_000)
    app.state.metrics_queue = metrics_queue

    async def on_metric_msg(msg):
        try:
            payload = MetricPayload.model_validate_json(msg.data)
            row = {
                "time": payload.timestamp,
                "site_id": payload.site_id,
                "region": payload.region,
                "status_code": payload.status_code,
                "latency_ms": payload.latency_ms,
                "error_message": payload.error_message,
            }
            metrics_queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("metrics_queue is full; dropping metric")
        except Exception:
            logger.exception("Failed to parse/queue metric message")

    subscriptions: dict[str, NATSSubscription] = {}
    subscriptions["metrics"] = await nc.subscribe(
        NATS_METRICS_SUBJECT, cb=on_metric_msg
    )
    subscriptions["agent_heartbeat"] = await nc.subscribe(
        "pingpal.agents.heartbeat", cb=on_heartbeat_msg
    )
    worker_task = asyncio.create_task(_metrics_db_worker(sessionmaker, metrics_queue))

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

        try:
            await nc.drain()
        except Exception:
            try:
                await nc.close()
            except Exception:
                pass

        await engine.dispose()


app = FastAPI(
    title="PingPal Core", lifespan=lifespan, dependencies=[Depends(get_api_key)]
)
