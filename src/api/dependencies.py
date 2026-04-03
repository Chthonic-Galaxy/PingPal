from types import CoroutineType
from typing import Any, Protocol

from fastapi import Request

from src.infrastructure.database.connection import get_db_manual_session, get_db_session
from src.schemas import AgentHeartbeat

active_agents: dict[str, AgentHeartbeat] = {}  # TODO: replace by Redis


class KVStore(Protocol):
    async def put(
        self, key: str, value: bytes, validate_keys: bool = True
    ) -> CoroutineType[Any, Any, int]: ...

    async def delete(
        self, key: str, last: int | None = None, validate_keys: bool = True
    ) -> CoroutineType[Any, Any, bool]: ...


async def get_nats_kv(request: Request) -> KVStore:
    return request.app.state.kv
