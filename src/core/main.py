from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import nats.js.errors as js_errors
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from nats.aio.client import Client as NATS
from nats.js.api import KeyValueConfig, StorageType
from pydantic import AnyHttpUrl, BaseModel, Field
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.database import (
    Metric,
    Site,
    build_db_settings,
    create_engine,
    create_sessionmaker,
    wait_for_db,
)

logger = logging.getLogger("pingpal-core")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

KV_BUCKET = "pingpal_config"
KV_KEY_PREFIX = "site."
NATS_METRICS_SUBJECT = "pingpal.metrics.ingest"


class SiteHealth(BaseModel):
    site_id: UUID
    status: str  # "UP", "DOWN", "PARTIAL_OUTAGE", "UNKNOWN"
    failing_regions: list[str]


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
    region: str | None
    avg_latency_ms: float | None
    last_status_code: int | None
    last_check_at: datetime | None


class MetricIngest(BaseModel):
    site_id: UUID
    region: str
    status_code: int
    latency_ms: float
    timestamp: datetime
    error_message: str | None = None


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
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


def _site_key(site_id: UUID) -> str:
    return f"{KV_KEY_PREFIX}{site_id}"


def _site_value(site: Site) -> bytes:
    payload = {
        "site_id": str(site.id),
        "url": site.url,
        "interval": site.interval,
        "is_active": site.is_active,
    }
    return json.dumps(payload).encode("utf-8")


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
    nats_url = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
    nc = NATS()
    await nc.connect(
        servers=[nats_url],
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
            data = json.loads(msg.data.decode("utf-8"))
            ingest = MetricIngest(
                site_id=UUID(str(data["site_id"])),
                region=data["region"],
                status_code=int(data["status_code"]),
                latency_ms=float(data["latency_ms"]),
                timestamp=_parse_dt(data["timestamp"]),
                error_message=(data.get("error_message") or None),
            )
            row = {
                "time": ingest.timestamp,
                "site_id": ingest.site_id,
                "region": ingest.region,
                "status_code": ingest.status_code,
                "latency_ms": ingest.latency_ms,
                "error_message": ingest.error_message,
            }
            metrics_queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("metrics_queue is full; dropping metric")
        except Exception:
            logger.exception("Failed to parse/queue metric message")

    sub = await nc.subscribe(NATS_METRICS_SUBJECT, cb=on_metric_msg)
    worker_task = asyncio.create_task(_metrics_db_worker(sessionmaker, metrics_queue))

    try:
        yield
    finally:
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
    site = Site(
        id=uuid4(), url=str(payload.url), interval=payload.interval, is_active=True
    )
    session.add(site)
    await session.commit()
    await session.refresh(site)

    kv = app.state.kv
    try:
        await kv.put(_site_key(site.id), _site_value(site))
    except Exception:
        logger.exception("Failed to KV.PUT config for site_id=%s", site.id)

    return SiteOut(
        id=site.id,
        url=site.url,
        interval=site.interval,
        is_active=site.is_active,
        created_at=site.created_at,  # type: ignore[arg-type]
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
            created_at=s.created_at,  # type: ignore[arg-type]
        )
        for s in sites
    ]


@app.delete("/sites/{site_id}", status_code=204)
async def deactivate_site(
    site_id: UUID = Path(...),
    session: AsyncSession = Depends(get_session),
):
    site = (
        await session.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=404, detail="site not found")

    site.is_active = False
    await session.commit()

    kv = app.state.kv
    try:
        await kv.delete(_site_key(site_id))
    except Exception:
        logger.exception("Failed to KV.DEL site_id=%s", site_id)

    return None


@app.get("/sites/{site_id}/stats", response_model=SiteStatsOut)
async def site_stats(
    site_id: UUID = Path(...),
    region: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    exists = await session.execute(select(Site.id).where(Site.id == site_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="site not found")

    avg_latency_stmt = select(func.avg(Metric.latency_ms)).where(
        Metric.site_id == site_id,
        Metric.status_code > 0,
    )
    if region:
        avg_latency_stmt = avg_latency_stmt.where(Metric.region == region)

    avg_latency = (await session.execute(avg_latency_stmt)).scalar_one_or_none()
    avg_latency_f = float(avg_latency) if avg_latency is not None else None

    last_stmt = select(Metric.status_code, Metric.time).where(Metric.site_id == site_id)
    if region:
        last_stmt = last_stmt.where(Metric.region == region)
    last_stmt = last_stmt.order_by(Metric.time.desc()).limit(1)

    last = (await session.execute(last_stmt)).first()

    if last is None:
        return SiteStatsOut(
            site_id=site_id,
            region=region,
            avg_latency_ms=avg_latency_f,
            last_status_code=None,
            last_check_at=None,
        )

    last_status_code, last_time = int(last[0]), last[1]
    return SiteStatsOut(
        site_id=site_id,
        region=region,
        avg_latency_ms=avg_latency_f,
        last_status_code=last_status_code,
        last_check_at=last_time,
    )


@app.get("/sites/{site_id}/state", response_model=SiteHealth)
async def check_health(
    site_id: UUID = Path(...),
    session: AsyncSession = Depends(get_session),
):
    exists = await session.execute(select(Site.id).where(Site.id == site_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="site not found")

    stmt = (
        select(Metric.region, Metric.status_code)
        .distinct(Metric.region)
        .where(
            Metric.site_id == site_id,
            Metric.time >= datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        .order_by(Metric.region, Metric.time.desc())
    )

    result = await session.execute(stmt)
    latest_metrics = result.all()

    failing_regions = []
    for region, status_code in latest_metrics:
        if status_code >= 400 or status_code == 0:
            failing_regions.append(region)

    status = "UP"
    unique_errors = len(failing_regions)

    match unique_errors:
        case 0:
            status = "UP"
        case 1:
            status = "PARTIAL_OUTAGE"
        case _:
            status = "DOWN"

    return SiteHealth(
        site_id=site_id, status=status, failing_regions=list(failing_regions)
    )
