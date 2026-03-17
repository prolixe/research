# Notes

## 2026-03-17

### Task

Setup ClickHouse via Docker Compose with:
- Data and logs mounted to /tmp
- Schema for 3 event types related to call legs
- A streaming event generator

### Data Model Design

**Entities:**
- `org_id` + `leg_id` = common key across all events
- `conversation_id` = groups multiple legs together (type 2 event)
- One conversation: many agent legs, exactly 1 caller leg
- CSAT attaches to the caller leg_id
- agent user_id attaches to the agent leg_id

**Event Types:**
1. Agent leg: `(timestamp, org_id, leg_id, user_id)` — agent joined call
2. Conversation association: `(timestamp, org_id, leg_id, conversation_id)` — leg mapped to convo
3. CSAT: `(timestamp, org_id, leg_id, csat_offered, csat_score)` — caller satisfaction

### ClickHouse Schema Decisions

- `agent_legs`: MergeTree, ordered by (org_id, leg_id)
- `conversation_legs`: ReplacingMergeTree so if a leg is re-associated, latest wins; ordered by (org_id, leg_id)
- `csat_events`: ReplacingMergeTree, ordered by (org_id, leg_id)
- Partitioned by month for time-range pruning
- `conversation_legs` is the join spine: to find all legs in the same conversation as a given leg, self-join on conversation_id

### Generator Design

- Simulates batches of conversations
- Each conversation: 1 caller leg + 1-4 agent legs
- All legs emit type 2 (conversation association) at staggered times
- ~70% of conversations emit a CSAT on the caller leg
- Uses `clickhouse-connect` (HTTP interface) for simplicity and batched inserts
- Supports both a one-shot bulk load and a continuous streaming mode

### Docker Compose Notes

- `/tmp/clickhouse/data` → `/var/lib/clickhouse`
- `/tmp/clickhouse/logs` → `/var/log/clickhouse-server`
- Custom config XML to override paths (ClickHouse reads from XML config)
- HTTP port 8123, native port 9000
