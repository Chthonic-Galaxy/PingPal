from src.infrastructure.database.connection import get_db_session

active_agents: dict[str, AgentHeartbeat] = {}  # TODO: replace by Redis
