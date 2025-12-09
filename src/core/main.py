from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from nats.aio.client import Client as NATS
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.database import (
    Metric,
    Site,
    build_db_settings,
    create_engine,
    create_sessionmaker,
    init_db,
)

logger = logging.getLogger("pingpal-core")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


NATS_CONFIG_SUBJECT = "pingpal.config.updates"
NATS_METRICS_SUBJECT = "pingpal.metrics.ingest"


class SiteCreate(BaseModel):
    url: AnyHttpUrl
    interval: int = Field(default=60, ge=5, le=24 * 60 * 60)


class SiteOut(BaseModel):
    id: UUID
    url: str
    interval: int
    is_active: bool
    created_at: datetime


class SiteStatsOut(BaseModel):
    site_id: UUID
    avg_latency_ms: float | None
    last_status_code: int | None
    last_check_at: datetime | None


class MetricIngest(BaseModel):
    site_id: UUID
    status_code: int
    latency_ms: float
    timestamp: datetime
    error_message: str | None = None


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        # Accept ISO-8601 from agent
        dt = datetime.fromisoformat(value)
    else:
        raise ValueError("invalid timestamp")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_session(request: Request) -> AsyncSession:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


async def _publish_active_sites(app: FastAPI) -> None:
    """
    MVP convenience: on Core startup, re-emit ADD events so an Agent
    starting later can still learn current config.
    """
    nc: NATS = app.state.nats
    sessionmaker: async_sessionmaker[AsyncSession] = app.state.sessionmaker

    async with sessionmaker() as session:
        rows = (
            (await session.execute(select(Site).where(Site.is_active.is_(True))))
            .scalars()
            .all()
        )

    for s in rows:
        payload = {
            "type": "ADD",
            "site_id": str(s.id),
            "url": s.url,
            "interval": s.interval,
        }
        try:
            await nc.publish(NATS_CONFIG_SUBJECT, json.dumps(payload).encode("utf-8"))
        except Exception:
            logger.exception("Failed to publish startup ADD for site_id=%s", s.id)


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

    await init_db(engine)

    # NATS
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    nc = NATS()
    await nc.connect(servers=[nats_url], name="pingpal-core")
    app.state.nats = nc

    # Metrics ingestion pipeline
    metrics_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50_000)
    app.state.metrics_queue = metrics_queue

    async def on_metric_msg(msg):
        try:
            data = json.loads(msg.data.decode("utf-8"))
            ingest = MetricIngest(
                site_id=UUID(str(data["site_id"])),
                status_code=int(data["status_code"]),
                latency_ms=float(data["latency_ms"]),
                timestamp=_parse_dt(data["timestamp"]),
                error_message=(data.get("error_message") or None),
            )
            row = {
                "time": ingest.timestamp,
                "site_id": ingest.site_id,
                "status_code": ingest.status_code,
                "latency_ms": ingest.latency_ms,
                "error_message": ingest.error_message,
            }
            metrics_queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("metrics_queue is full; dropping metric message")
        except Exception:
            logger.exception("Failed to parse/queue metric message")

    sub = await nc.subscribe(NATS_METRICS_SUBJECT, cb=on_metric_msg)

    worker_task = asyncio.create_task(_metrics_db_worker(sessionmaker, metrics_queue))

    # Emit current active sites as ADD events (MVP quality-of-life)
    await _publish_active_sites(app)

    try:
        yield
    finally:
        # Shutdown
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)

        try:
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


app = FastAPI(title="PingPal Core", lifespan=lifespan)


@app.post("/sites", response_model=SiteOut, status_code=201)
async def create_site(
    payload: SiteCreate, session: AsyncSession = Depends(get_session)
):
    site = Site(url=str(payload.url), interval=payload.interval, is_active=True)
    session.add(site)
    await session.commit()
    await session.refresh(site)

    # Publish config update (best-effort)
    nc: NATS = app.state.nats
    msg = {
        "type": "ADD",
        "site_id": str(site.id),
        "url": site.url,
        "interval": site.interval,
    }
    try:
        await nc.publish(NATS_CONFIG_SUBJECT, json.dumps(msg).encode("utf-8"))
    except Exception:
        logger.exception("Failed to publish ADD config update for site_id=%s", site.id)

    return SiteOut(
        id=site.id,
        url=site.url,
        interval=site.interval,
        is_active=site.is_active,
        created_at=site.created_at,
    )


@app.get("/sites", response_model=list[SiteOut])
async def list_sites(session: AsyncSession = Depends(get_session)):
    sites = (
        (await session.execute(select(Site).order_by(Site.created_at.desc())))
        .scalars()
        .all()
    )
    return [
        SiteOut(
            id=s.id,
            url=s.url,
            interval=s.interval,
            is_active=s.is_active,
            created_at=s.created_at,
        )
        for s in sites
    ]


@app.get("/sites/{site_id}/stats", response_model=SiteStatsOut)
async def site_stats(
    site_id: UUID = Path(...),
    session: AsyncSession = Depends(get_session),
):
    exists = await session.execute(select(Site.id).where(Site.id == site_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="site not found")

    avg_latency_stmt = select(func.avg(Metric.latency_ms)).where(
        Metric.site_id == site_id,
        Metric.status_code > 0,
    )
    avg_latency = (await session.execute(avg_latency_stmt)).scalar_one_or_none()
    avg_latency_f = float(avg_latency) if avg_latency is not None else None

    last_stmt = (
        select(Metric.status_code, Metric.time)
        .where(Metric.site_id == site_id)
        .order_by(Metric.time.desc())
        .limit(1)
    )
    last = (await session.execute(last_stmt)).first()

    if last is None:
        return SiteStatsOut(
            site_id=site_id,
            avg_latency_ms=avg_latency_f,
            last_status_code=None,
            last_check_at=None,
        )

    last_status_code, last_time = int(last[0]), last[1]
    return SiteStatsOut(
        site_id=site_id,
        avg_latency_ms=avg_latency_f,
        last_status_code=last_status_code,
        last_check_at=last_time,
    )
