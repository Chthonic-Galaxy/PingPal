from uuid import UUID

from src.config import settings
from src.infrastructure.database.models import (
    Site,
)
from src.schemas import SiteConfig


def _site_key(site_id: UUID) -> str:
    return f"{settings.nats.kv_key_prefix}{site_id}"


def _site_value(site: Site) -> bytes:
    payload = SiteConfig(
        site_id=site.id,
        url=site.url,  # pyright: ignore[reportArgumentType]
        interval=site.interval,
        is_active=site.is_active,
        regions=site.regions,
    )
    return payload.model_dump_json().encode("utf-8")
