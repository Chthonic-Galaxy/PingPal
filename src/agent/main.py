from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from nats.aio.client import Client as NATS
from pydantic import AnyHttpUrl, BaseModel, Field

logger = logging.getLogger("pingpal-agent")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

NATS_CONFIG_SUBJECT = "pingpal.config.updates"
NATS_METRICS_SUBJECT = "pingpal.metrics.ingest"


class ConfigUpdate(BaseModel):
    type: str = Field(..., pattern="^(ADD|REMOVE)$")
    site_id: UUID
    url: AnyHttpUrl | None = None
    interval: int | None = Field(default=None, ge=5, le=24 * 60 * 60)


@dataclass
class RunningTask:
    stop: asyncio.Event
    task: asyncio.Task[None]
    url: str
    interval: int


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ping_loop(
    *,
    site_id: UUID,
    url: str,
    interval: int,
    nc: NATS,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    # Small guardrail
    interval = max(1, int(interval))

    try:
        while not stop.is_set():
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

            payload: dict[str, Any] = {
                "site_id": str(site_id),
                "status_code": status_code,
                "latency_ms": latency_ms,
                "timestamp": utc_now_iso(),
            }
            if error_message:
                payload["error_message"] = error_message

            try:
                await nc.publish(
                    NATS_METRICS_SUBJECT,
                    json.dumps(payload).encode("utf-8"),
                )
            except Exception:
                logger.exception("Failed to publish metric for site_id=%s", site_id)

            # Sleep until next interval or stop
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ping_loop crashed for site_id=%s", site_id)


async def main() -> None:
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")

    nc = NATS()
    await nc.connect(servers=[nats_url], name="pingpal-agent")

    tasks: dict[UUID, RunningTask] = {}

    timeout = httpx.Timeout(
        connect=5.0,
        read=10.0,
        write=10.0,
        pool=5.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:

        async def stop_task(site_id: UUID) -> None:
            rt = tasks.pop(site_id, None)
            if not rt:
                return
            rt.stop.set()
            rt.task.cancel()
            await asyncio.gather(rt.task, return_exceptions=True)
            logger.info("Stopped task site_id=%s", site_id)

        async def start_or_restart_task(site_id: UUID, url: str, interval: int) -> None:
            existing = tasks.get(site_id)
            if existing and existing.url == url and existing.interval == interval:
                return

            if existing:
                await stop_task(site_id)

            stop_evt = asyncio.Event()
            t = asyncio.create_task(
                ping_loop(
                    site_id=site_id,
                    url=url,
                    interval=interval,
                    nc=nc,
                    client=client,
                    stop=stop_evt,
                )
            )
            tasks[site_id] = RunningTask(
                stop=stop_evt, task=t, url=url, interval=interval
            )
            logger.info(
                "Started task site_id=%s interval=%s url=%s", site_id, interval, url
            )

        async def on_config_msg(msg):
            try:
                raw = json.loads(msg.data.decode("utf-8"))
                update = ConfigUpdate(**raw)

                if update.type == "REMOVE":
                    await stop_task(update.site_id)
                    return

                # ADD
                if update.url is None or update.interval is None:
                    logger.warning("Invalid ADD payload missing url/interval: %s", raw)
                    return

                await start_or_restart_task(
                    update.site_id, str(update.url), int(update.interval)
                )

            except Exception:
                logger.exception("Failed to handle config message: %r", msg.data)

        sub = await nc.subscribe(NATS_CONFIG_SUBJECT, cb=on_config_msg)

        # Graceful shutdown
        shutdown_evt = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_evt.set)
            except NotImplementedError:
                # Some platforms (e.g. Windows) may not support it
                pass

        await shutdown_evt.wait()

        # Cleanup
        try:
            await sub.unsubscribe()
        except Exception:
            pass

        # Stop all ping loops
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
