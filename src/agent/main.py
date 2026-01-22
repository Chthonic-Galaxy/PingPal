from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import httpx
import nats.errors
import nats.js.errors as js_errors
from nats.aio.client import Client as NATS
from nats.js.api import KeyValueConfig, StorageType

from src.config import settings
from src.schemas import MetricPayload, SiteConfig

logger = logging.getLogger("pingpal-agent")
logging.basicConfig(level=settings.log_level)

KV_BUCKET = "pingpal_config"
KV_WATCH_PATTERN = "site.*"
NATS_METRICS_SUBJECT = "pingpal.metrics.ingest"

KV_DEL = "DEL"
KV_PURGE = "PURGE"

AGENT_REGION = settings.pingpal_region


@dataclass
class RunningTask:
    stop: asyncio.Event
    task: asyncio.Task[None]
    url: str
    interval: int
    is_sleeping: bool = False


def utc_now_iso() -> datetime:
    return datetime.now(timezone.utc)


async def ping_loop(
    *,
    site_id: UUID,
    url: str,
    interval: int,
    nc: NATS,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
    rt_state: RunningTask,
) -> None:
    interval = max(1, int(interval))

    try:
        while not stop.is_set():
            rt_state.is_sleeping = False

            started = time.perf_counter()
            status_code = 0
            error_message: str | None = None

            try:
                resp = await client.get(url, follow_redirects=True)
                status_code = int(resp.status_code)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                error_message = f"{type(e).__name__}: {e}"
            except Exception as e:
                error_message = f"{type(e).__name__}: {e}"

            latency_ms = (time.perf_counter() - started) * 1000.0

            metric = MetricPayload(
                site_id=site_id,
                region=AGENT_REGION,
                status_code=status_code,
                latency_ms=latency_ms,
                timestamp=utc_now_iso(),
                error_message=error_message,
            )

            try:
                await nc.publish(
                    NATS_METRICS_SUBJECT, metric.model_dump_json().encode("utf-8")
                )
            except Exception:
                logger.exception("Failed to publish metric for site_id=%s", site_id)

            rt_state.is_sleeping = True
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ping_loop crashed for site_id=%s", site_id)


async def ensure_kv_bucket(nc: NATS):
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


async def main() -> None:
    nats_url = settings.nats_url

    nc = NATS()
    await nc.connect(
        servers=[nats_url],
        tls=settings.ssl_context,
        name="pingpal-agent",
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,
    )

    kv = await ensure_kv_bucket(nc)

    tasks: dict[UUID, RunningTask] = {}

    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:

        def on_task_done(t: asyncio.Task, site_id: UUID) -> None:
            try:
                exc = t.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    logger.error(f"Task for site {site_id} crashed: {exc}")
                else:
                    logger.info(f"Task for site {site_id} finished gracefully")
            except asyncio.CancelledError:
                pass

            if site_id in tasks:
                if tasks[site_id].task == t:
                    del tasks[site_id]
                    logger.info(f"Removed dead task for site {site_id} from registry")

        async def stop_task(site_id: UUID) -> None:
            rt = tasks.pop(site_id, None)
            if not rt:
                return
            rt.stop.set()
            if rt.is_sleeping:
                rt.task.cancel()
                logger.info("Cancelling sleeping task site_id=%s", site_id)
            else:
                logger.info("Waiting for busy task to finish site_id=%s", site_id)
            await asyncio.gather(rt.task, return_exceptions=True)
            logger.info("Stopped task site_id=%s", site_id)

        async def start_or_restart_task(cfg: SiteConfig) -> None:
            should_run = False
            if "global" in cfg.regions:
                should_run = True
            if AGENT_REGION in cfg.regions:
                should_run = True
            
            if not should_run or not cfg.is_active:
                await stop_task(cfg.site_id)
                if not should_run and cfg.is_active:
                    logger.debug(f"Skipping site {cfg.site_id} (Target: {cfg.regions}, Me: {AGENT_REGION})")
                return

            existing = tasks.get(cfg.site_id)
            if (
                existing
                and existing.url == str(cfg.url)
                and existing.interval == int(cfg.interval)
            ):
                return

            if existing:
                await stop_task(cfg.site_id)

            stop_evt = asyncio.Event()
            # task config pre-creating
            rt = RunningTask(
                stop=stop_evt,
                task=None,  # pyright: ignore[reportArgumentType]
                url=str(cfg.url),
                interval=int(cfg.interval),
                is_sleeping=False,
            )

            t = asyncio.create_task(
                ping_loop(
                    site_id=cfg.site_id,
                    url=str(cfg.url),
                    interval=int(cfg.interval),
                    nc=nc,
                    client=client,
                    stop=stop_evt,
                    rt_state=rt,  # forward running task there
                )
            )
            t.add_done_callback(lambda task: on_task_done(task, cfg.site_id))

            rt.task = t
            tasks[cfg.site_id] = rt

            logger.info(
                "Started task site_id=%s interval=%s url=%s",
                cfg.site_id,
                cfg.interval,
                cfg.url,
            )

        shutdown_evt = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_evt.set)
            except NotImplementedError:
                pass

        async def kv_watch_loop() -> None:
            """
            ВАЖНО: KeyWatcher кладёт `None` как маркер "initial catch-up finished".
            Но `async for entry in watcher` завершится на первом None (см. __anext__),
            поэтому используем бесконечный while + watcher.updates().
            """
            watcher = await kv.watch(KV_WATCH_PATTERN, include_history=True)

            try:
                while not shutdown_evt.is_set():
                    try:
                        entry = await watcher.updates(timeout=1.0)
                    except nats.errors.TimeoutError:
                        continue

                    if entry is None:
                        continue

                    op = entry.operation
                    key = entry.key

                    if op in (KV_DEL, KV_PURGE):
                        if key.startswith("site."):
                            raw_id = key.split("site.", 1)[1]
                            try:
                                await stop_task(UUID(raw_id))
                            except Exception:
                                logger.warning("DEL/PURGE for unparseable key=%s", key)
                        continue

                    if not entry.value:
                        continue

                    try:
                        cfg = SiteConfig.model_validate_json(entry.value)
                        await start_or_restart_task(cfg)
                    except Exception:
                        logger.exception("Failed to handle KV PUT key=%s", key)

            finally:
                try:
                    await watcher.stop()
                except Exception:
                    pass

        watch_task = asyncio.create_task(kv_watch_loop())

        await shutdown_evt.wait()

        watch_task.cancel()
        await asyncio.gather(watch_task, return_exceptions=True)

        await asyncio.gather(
            *(stop_task(site_id) for site_id in list(tasks.keys())),
            return_exceptions=True,
        )

    try:
        await nc.drain()
    except Exception:
        try:
            await nc.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
