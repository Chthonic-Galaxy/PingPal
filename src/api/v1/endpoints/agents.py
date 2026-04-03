import logging
from datetime import datetime, timezone

from fastapi import APIRouter

from src.api.dependencies import active_agents
from src.schemas import AgentStatusOut

logger = logging.getLogger("endpoints-agents")

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/", response_model=list[AgentStatusOut])
async def list_agents():
    now = datetime.now(timezone.utc)
    results = []

    for agent_id, hb in active_agents.items():
        delta = (now - hb.timestamp).total_seconds()

        status = "ONLINE"
        if delta > 30:
            status = "OFFLINE"

        results.append(
            AgentStatusOut(
                agent_id=hb.agent_id,
                region=hb.region,
                status=status,
                last_seen_seconds_ago=delta,
                started_at=hb.started_at,
            )
        )

    return results
