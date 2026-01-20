from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field


class SiteConfig(BaseModel):
    site_id: UUID
    url: AnyHttpUrl
    interval: int = Field(ge=5, le=24 * 60 * 60)
    is_active: bool = True


class MetricPayload(BaseModel):
    site_id: UUID
    region: str
    status_code: int
    latency_ms: float
    timestamp: datetime
    error_message: str | None = None


class SiteHealth(BaseModel):
    site_id: UUID
    status: Literal["UP", "DOWN", "PARTIAL_OUTAGE", "UNKNOWN"]
    failing_regions: list[str]
