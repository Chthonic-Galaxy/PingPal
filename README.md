# PingPal

A distributed uptime monitoring system. You define sites, agents across regions ping them on a schedule, and metrics flow back through a message broker into a time-series database. The whole thing is region-aware — you can deploy agents in different locations and get a per-region view of your sites' health.

---

## How it works

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PingPal Core                               │
│  FastAPI · REST API · Metric ingestion · Agent heartbeat tracker   │
└───────────────────────┬─────────────────────────────────────────────┘
                        │  mTLS
                        │
              ┌─────────▼──────────┐
              │   NATS JetStream   │
              │  KV · Streams      │
              └──────┬──────┬──────┘
                     │      │  mTLS
           ┌─────────▼──┐  ┌▼──────────┐
           │  Agent     │  │  Agent    │
           │  us-east   │  │  eu-west  │
           └────────────┘  └───────────┘
                        │
               ┌────────▼────────┐
               │  TimescaleDB    │
               │  (PostgreSQL)   │
               └─────────────────┘
```

**Core** is the control plane. It exposes the REST API, syncs site configuration into a NATS KV bucket on startup, and runs a pull-based worker that ingests metric messages from agents into the database in batches.

**Agents** are stateless workers. Each one watches the KV bucket for configuration changes and manages a pool of async ping tasks accordingly. They publish metrics to a JetStream stream and send periodic heartbeats so Core knows they're alive.

**NATS JetStream** does two things: acts as a durable key-value store for site configs (so agents always have a source of truth on reconnect), and as a persistent stream for metrics (so nothing is lost if Core is temporarily unavailable).

All communication between components happens over **mTLS** — every service presents a client certificate signed by the same CA.

---

## Tech stack

| Component | Choice | Why |
|-----------|--------|-----|
| API | FastAPI | Async-native, Pydantic v2 built-in |
| Message broker | NATS JetStream | JetStream gives durable streams and KV — no need for separate Redis + Kafka. Pull consumers let the ingestion worker control its own pace. |
| Database | TimescaleDB | Hypertables compress and auto-expire time-series data well without changing how you write SQL. Regular PostgreSQL would work, but chunk-based queries are noticeably faster at scale. |
| HTTP client (agent) | httpx | Async, timeout-aware, follows redirects |
| ORM / migrations | SQLAlchemy 2 + Alembic | Async session support, typed mapped columns |

### Why JetStream over plain NATS Pub/Sub

Plain NATS is fire-and-forget. If Core is restarting when an agent publishes a metric, that message is gone. JetStream persists messages to disk and gives consumers a durable cursor — the pull subscriber in Core reads exactly what it can handle, acknowledges each batch, and picks up where it left off after a restart. At-least-once delivery without managing offsets manually.

### Why TimescaleDB

Metrics are append-only and time-ordered. TimescaleDB partitions the `metrics` table into time-based chunks automatically (hypertable), which makes range queries fast and makes retention policies trivial — one SQL call drops an entire old chunk instead of running a DELETE across millions of rows.

---

## Key technical details

### Batch ingestion

Agents publish metrics as individual JetStream messages. Core runs a background pull consumer that fetches up to 500 messages at a time, bulk-inserts them with a single `INSERT`, and acknowledges the batch only after a successful commit. This avoids per-row round-trips to the database under load.

### mTLS everywhere

`pki.py` generates a small CA and three leaf certificates: one for NATS server, one for Core, one for agents. Every component loads its certificate at startup. There's no unauthenticated path into the broker.

### Region-aware routing

Each agent has a `PINGPAL_REGION` env variable. When Core puts a `SiteConfig` into KV, the config carries a `regions` list. Agents check whether their region is in that list (or if `"global"` is set) before starting a ping task. A site can be monitored from a specific set of regions or from all of them simultaneously.

### Health state logic

The `/api/v1/sites/{id}/state/` endpoint looks at the last metric per region from the past 5 minutes. If zero regions are failing — `UP`. One failing region — `PARTIAL_OUTAGE`. More than one — `DOWN`. This gives a coarse but useful signal without storing computed state anywhere.

### Database schema

```sql
-- metrics is a TimescaleDB hypertable partitioned by time
CREATE TABLE metrics (
    time        TIMESTAMPTZ NOT NULL,
    site_id     UUID        NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    region      VARCHAR(50) NOT NULL DEFAULT 'global',
    status_code INTEGER     NOT NULL,
    latency_ms  FLOAT       NOT NULL,
    error_message TEXT,
    PRIMARY KEY (time, site_id, region)
);
```

The composite primary key `(time, site_id, region)` means each time-slot is unique per site per region — which matches how agents publish. The 30-day retention policy is applied via TimescaleDB's `add_retention_policy`.

---

## Running locally

### Prerequisites

- Docker + Docker Compose
- Python 3.13 with `cryptography` installed (only for the cert generation step)

```bash
uv init
```
or
```bash
python -m venv .venv ; source .venv/bin/activate ; pip install -r requirements.txt
```

### 1. Generate certificates

```bash
uv run pki.py
```
or
```bash
python pki.py
```

This writes `certs/ca.crt`, `certs/server.{crt,key}`, `certs/client-core.{crt,key}`, and `certs/client-agent.{crt,key}`. Both Core and agents mount this directory read-only at `/app/certs`.

### 2. Create the secrets file

```bash
mkdir -p secrets
echo "your_pg_password" > secrets/pg_password.txt
```

The password is passed to both Postgres and the application via Docker Secrets — it never appears in environment variables or image layers.

### 3. Configure environment

Copy `.env.example` to `.env` and set at least `APP_API_KEY`. Everything else has a working default for local use (see [Configuration](#configuration)).

### 4. Start everything

```bash
docker compose up
```

Compose starts services in the right order: NATS and Postgres first, then the `migration` service runs `alembic upgrade head` and exits, then Core and agents come up. You don't need to run migrations manually.

The stack includes three agents out of the box:

| Service | Region |
|---------|--------|
| `agent` | `global` |
| `agent-us` | `us-east` |
| `agent-eu` | `eu-central` |

Core is available at `http://localhost:8000`. All requests require an `X-API-Key` header. NATS monitoring is available at `http://localhost:8222`.

> **Production note:** The `pg_data` and `nats_data` volumes are defined as anonymous by default. Uncomment the `external: true` lines in `docker-compose.yaml` before going to production so a `docker compose down` can't accidentally wipe your data.

---

## API

All endpoints require `X-API-Key: <your_key>`.

### Sites

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/sites/` | Register a new site |
| `GET` | `/api/v1/sites/` | List all sites |
| `DELETE` | `/api/v1/sites/{id}/` | Deactivate a site |
| `GET` | `/api/v1/sites/{id}/stats/` | Latency and last status code |
| `GET` | `/api/v1/sites/{id}/state/` | Current health state (`UP` / `PARTIAL_OUTAGE` / `DOWN`) |

**Create a site:**
```bash
curl -X POST http://localhost:8000/api/v1/sites/ \
  -H "X-API-Key: dev-secret-key" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "interval": 60, "regions": ["us-east", "eu-central"]}'
```

**Check health:**
```bash
curl http://localhost:8000/api/v1/sites/<uuid>/state/ \
  -H "X-API-Key: dev-secret-key"
```

### Agents

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/agents/` | List known agents and their status |

An agent is considered `OFFLINE` if no heartbeat has been received in the last 30 seconds.

---

## Configuration

Core and agents share the same `Settings` model, loaded from `.env` with `__` as the nested delimiter.

```env
# Logging
LOG_LEVEL=INFO

# API
APP_API_KEY=your-secret-key

# Database
DATABASE__HOST=localhost
DATABASE__PORT=5432
DATABASE__NAME=pingpal
DATABASE__USER=pingpal
DATABASE__PASSWORD=            # or use secrets/pg_password.txt
DATABASE__ECHO=false
DATABASE__POOL_SIZE=10
DATABASE__POOL_PRE_PING=true

# NATS
NATS__URL=nats://localhost:4222
NATS__KV_BUCKET=pingpal-sites
NATS__KV_KEY_PREFIX=site.
NATS__KV_WATCH_PATTERN=site.*
NATS__KV_DEL=DEL
NATS__KV_PURGE=PURGE
NATS__METRICS_SUBJECT=pingpal.metrics
NATS__AGENTS_HEARTBEAT_SUBJECT=pingpal.agents.heartbeat

# TLS (paths inside container)
TLS_CA_CERT=/app/certs/ca.crt
TLS_CERT=/app/certs/client-core.crt   # override to client-agent for agents
TLS_KEY=/app/certs/client-core.key

# Agent-specific
PINGPAL_REGION=global
```

To add another region, duplicate one of the agent service blocks in `docker-compose.yaml` and set `PINGPAL_REGION` to whatever string you want. Agents use the same image and cert pair — only the region env var changes.

---

## Project structure

```
src/
├── api/v1/endpoints/   # sites.py, agents.py
├── core/               # FastAPI app, lifespan, metric worker
├── agent/              # Standalone agent process
├── infrastructure/
│   ├── database/       # SQLAlchemy models, connection, migrations util
│   └── message_broker/ # NATSManager wrapper
└── schemas.py          # Shared Pydantic models
migrations/             # Alembic versions
pki.py                  # CA + leaf cert generation
```

---

AI was used to write this README.md under my supervision.
