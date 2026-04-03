from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, Field


#!NOTE: This sceme is used to configure Agent - give it a task(site to ping)
class SiteConfig(BaseModel):
    site_id: UUID
    url: AnyHttpUrl
    interval: int = Field(ge=5, le=24 * 60 * 60)
    is_active: bool = True
    regions: list[str] = Field(default_factory=lambda: ["global"])


#!NOTE: This scheme is based on `Metric` DB model | Used in endpoint to show site's status
class SiteHealth(BaseModel):
    site_id: UUID
    status: Literal["UP", "DOWN", "PARTIAL_OUTAGE", "UNKNOWN"]
    failing_regions: list[str]


#!NOTE: This scheme is used in endpoint as payload -> persists into DB as `Site` model -> this data is putted into NATS in `SiteConfig` model format -> Agent gets it -> Agent works
class SiteCreate(BaseModel):
    url: AnyHttpUrl
    interval: int = Field(default=60, ge=5, le=24 * 60 * 60)
    regions: list[str] = Field(
        min_length=1,
        default_factory=lambda: ["global"],
        examples=[["us-east", "eu-central"]],
    )


#!NOTE: This scheme based on `Site` | Used in endpoints to return/fetch data about Site(not site's statistics)
class SiteOut(BaseModel):
    id: UUID
    url: str
    interval: int
    is_active: bool
    regions: list[str]
    created_at: datetime


#!NOTE: This scheme is based on `Metric` | Used in endpoints to fetch site statisics
class SiteStatsOut(BaseModel):
    site_id: UUID
    region: str | None
    avg_latency_ms: float | None
    last_status_code: int | None
    last_check_at: datetime | None


#!NOTE: Agent sends this message to NATS on `pingpal.agents.heartbeat` -> Core subscribed on tbis theme -> New post appears -> Core calls callback which writes it to 'Memory DB'(`active_agents: dict[str, AgentHeartbeat] = {}` variable)(need to replace it by Redis)
class AgentHeartbeat(BaseModel):
    agent_id: str
    region: str
    timestamp: datetime
    is_busy: bool = False
    started_at: datetime


#!NOTE: On request to endpoint(`@app.get("/agents"`) -> endpoint fetchs data from 'Memory DB' -> Compute some data and logic -> Converts `AgentHeartbeat` model into `AgentStatusOut` moedel and responds with it.
class AgentStatusOut(BaseModel):
    agent_id: str
    region: str
    status: Literal["ONLINE", "OFFLINE"]
    last_seen_seconds_ago: float
    started_at: datetime


#!NOTE: This model is for MetricPayload
# Sheme of work: Agent pings sites -> constructs this model -> sends to NATS Broker Message -> Core Fetchs this data -> *Converts manualy this Model into DB Model* -> Starts Async Worker to flush this info into DB('batch method' if items >= 500 else flushes as is)
class MetricPayload(BaseModel):
    site_id: UUID
    region: str
    status_code: int
    latency_ms: float
    timestamp: datetime
    error_message: str | None = None
