from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Path, Query
from fastapi.exceptions import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_201_CREATED, HTTP_204_NO_CONTENT, HTTP_404_NOT_FOUND

from src.core.routers.dependencies import get_db_session
from src.infrastructure.database.models import (
    Metric,
    Site,
)
from src.schemas import SiteCreate, SiteHealth, SiteOut, SiteStatsOut

router = APIRouter(prefix="sites/", tags=["sites"])


@router.post("", response_model=SiteOut, status_code=HTTP_201_CREATED)
async def create_site(
    payload: SiteCreate, session: AsyncSession = Depends(get_db_session)
):
    site = Site(
        id=uuid4(),
        url=str(payload.url),
        interval=payload.interval,
        is_active=True,
        regions=payload.regions,
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
        regions=site.regions,
        created_at=site.created_at,
    )


@router.get("", response_model=list[SiteOut])
async def list_sites(session: AsyncSession = Depends(get_db_session)):
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
            regions=s.regions,
            created_at=s.created_at,
        )
        for s in sites
    ]


@router.delete("{site_id}", status_code=HTTP_204_NO_CONTENT)
async def deactivate_site(
    site_id: UUID = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    site = (
        await session.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="site not found")

    site.is_active = False
    await session.commit()

    kv = app.state.kv
    try:
        await kv.delete(_site_key(site_id))
    except Exception:
        logger.exception("Failed to KV.DEL site_id=%s", site_id)

    return None


@router.get("{site_id}/stats", response_model=SiteStatsOut)
async def site_stats(
    site_id: UUID = Path(...),
    region: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
):
    exists = await session.execute(select(Site.id).where(Site.id == site_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="site not found")

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


@router.get("{site_id}/state", response_model=SiteHealth)
async def check_health(
    site_id: UUID = Path(...),
    session: AsyncSession = Depends(get_db_session),
):
    exists = await session.execute(select(Site.id).where(Site.id == site_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="site not found")

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
