# ClickHouse Call-Leg Event Pipeline

A self-contained Docker Compose setup that runs ClickHouse and a Python event
generator simulating a call-centre data model.

---

## Data Model

### The problem

Call legs come from multiple sources. The common key across all sources is
`(org_id, leg_id)`. A leg is linked to a conversation via a separate event that
may arrive later. Multiple legs can share a conversation, and you often need to
query "what happened to all legs that were in the same conversation as leg X?"

### Event types

| # | Table | Who emits | Key fields |
|---|-------|-----------|------------|
| 1 | `agent_legs` | Agent (user) side | `org_id, leg_id, user_id` |
| 2 | `conversation_legs` | Both sides | `org_id, leg_id, conversation_id` |
| 3 | `csat_events` | Caller side only | `org_id, leg_id, csat_offered, csat_score` |

### Relationships

```
conversation
  ├── 1..n  agent legs   (leg with user_id)   ─── type-1 + type-2
  └── exactly 1 caller leg                    ─── type-2 + optionally type-3
```

`conversation_legs` is the **join spine**: to find all legs that shared a
conversation with a given leg:

```sql
SELECT cl2.leg_id, cl2.conversation_id
FROM calls.conversation_legs AS cl1
JOIN calls.conversation_legs AS cl2 USING (org_id, conversation_id)
WHERE cl1.org_id = 'org-001'
  AND cl1.leg_id = '<your-leg-id>'
  AND cl2.leg_id != cl1.leg_id;
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Docker Compose                                          │
│                                                         │
│  ┌──────────────────┐       ┌────────────────────────┐  │
│  │   generator      │──────▶│     clickhouse         │  │
│  │  (Python)        │ HTTP  │  port 8123 (HTTP)      │  │
│  │                  │ :8123 │  port 9000 (native)    │  │
│  └──────────────────┘       └───────────┬────────────┘  │
│                                         │               │
│                               /var/lib/clickhouse       │
│                               /var/log/clickhouse-server│
└─────────────────────────────────────────┼───────────────┘
                                          │ volume mount
                              ┌───────────▼──────────────┐
                              │  Host /tmp/clickhouse/    │
                              │    data/   ← DB files     │
                              │    logs/   ← server logs  │
                              └──────────────────────────┘
```

---

## Quick Start

### Prerequisites

- Docker ≥ 24
- Docker Compose v2

### 1. Create host directories

```bash
mkdir -p /tmp/clickhouse/{data,logs}
```

> The `init-dirs` service in the compose file also does this automatically.

### 2. Start ClickHouse

```bash
docker compose up clickhouse
```

ClickHouse is ready when you see:

```
clickhouse  | <Info> Application: Ready for connections.
```

Or poll the health endpoint:

```bash
curl -s http://localhost:8123/ping   # → Ok.
```

### 3. Run the generator

**Streaming mode** (default — runs forever, ~20 events/sec):

```bash
docker compose up generator
```

**Bulk mode** (generates N conversations and exits):

```bash
docker compose run -e MODE=bulk -e NUM_CONVOS=2000 generator
```

**Custom throughput**:

```bash
docker compose run -e MODE=streaming -e EVENTS_PER_SEC=100 generator
```

---

## Querying the Data

Connect with the CLI client:

```bash
docker compose exec clickhouse clickhouse-client --database calls
```

Or via HTTP:

```bash
curl -s 'http://localhost:8123/?database=calls&query=SELECT+count()+FROM+agent_legs'
```

### Useful queries

#### Row counts per table

```sql
SELECT
    'agent_legs'        AS tbl, count() AS n FROM calls.agent_legs
UNION ALL
SELECT 'conversation_legs', count() FROM calls.conversation_legs
UNION ALL
SELECT 'csat_events',       count() FROM calls.csat_events;
```

#### All legs in the same conversation as a given leg

```sql
SELECT cl2.leg_id, cl2.conversation_id
FROM calls.conversation_legs AS cl1
JOIN calls.conversation_legs AS cl2 USING (org_id, conversation_id)
WHERE cl1.org_id    = 'org-001'
  AND cl1.leg_id    = '<target-leg-id>'
  AND cl2.leg_id   != cl1.leg_id;
```

#### CSAT score for callers whose conversation also had a specific agent

```sql
SELECT
    csat.leg_id    AS caller_leg,
    csat.csat_score,
    al.user_id     AS agent_user_id
FROM calls.csat_events AS csat
-- caller leg → conversation
JOIN calls.conversation_legs AS cl_caller
    ON  cl_caller.org_id  = csat.org_id
    AND cl_caller.leg_id  = csat.leg_id
-- other legs in same conversation
JOIN calls.conversation_legs AS cl_agent
    ON  cl_agent.org_id          = cl_caller.org_id
    AND cl_agent.conversation_id = cl_caller.conversation_id
    AND cl_agent.leg_id         != cl_caller.leg_id
-- agent detail
JOIN calls.agent_legs AS al
    ON  al.org_id  = cl_agent.org_id
    AND al.leg_id  = cl_agent.leg_id
WHERE csat.org_id       = 'org-001'
  AND csat.csat_offered = true
ORDER BY csat.csat_score DESC
LIMIT 20;
```

#### Average CSAT per org (last 7 days)

```sql
SELECT
    org_id,
    countIf(csat_offered)                     AS surveys_offered,
    countIf(csat_score IS NOT NULL)            AS surveys_answered,
    round(avg(csat_score), 2)                  AS avg_score
FROM calls.csat_events
WHERE timestamp >= now() - INTERVAL 7 DAY
GROUP BY org_id
ORDER BY avg_score DESC;
```

---

## Configuration

### Volume paths

Edit `docker-compose.yml` to change where data lands on the host:

```yaml
volumes:
  - /your/path/data:/var/lib/clickhouse
  - /your/path/logs:/var/log/clickhouse-server
```

### ClickHouse tuning

`config/clickhouse-config.xml` controls async insert behaviour (buffer size,
flush interval). The defaults buffer for up to 200 ms or 10 MB, whichever
comes first — good for the streaming generator at low-to-moderate throughput.

For high-throughput production use, tune:

```xml
<async_insert_max_data_size>104857600</async_insert_max_data_size>  <!-- 100 MB -->
<async_insert_busy_timeout_ms>1000</async_insert_busy_timeout_ms>
```

---

## File Layout

```
clickhouse-calls/
├── docker-compose.yml          # Compose stack definition
├── config/
│   ├── clickhouse-config.xml   # Server config overrides (paths, async inserts)
│   └── clickhouse-users.xml    # User/profile/quota definitions
├── init/
│   └── 01_schema.sql           # Auto-runs on first container start
├── generator/
│   ├── generator.py            # Event generator (bulk + streaming modes)
│   ├── requirements.txt
│   └── Dockerfile
├── notes.md                    # Investigation log
└── README.md                   # This file
```
