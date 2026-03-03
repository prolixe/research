# TSDB Shootout: ClickHouse vs QuestDB

A head-to-head comparison of **ClickHouse** and **QuestDB** for a non-metrics
time-series use case with high cardinality, idempotent writes, and SQL support.

## Summary

| Criterion | ClickHouse | QuestDB | Winner |
|---|---|---|---|
| **Ingestion speed** | 448,880 rows/s | 167,163 rows/s | ClickHouse (2.7×) |
| **Disk usage (10M rows)** | 146 MiB | 785 MiB | ClickHouse (5.4×) |
| **Idempotent writes** | ✅ ReplacingMergeTree + token | ✅ DEDUP UPSERT KEYS | Tie |
| **Query-time dedup complexity** | Needs `FINAL` or `argMax` | Transparent | QuestDB |
| **SQL support** | Full SQL, JOINs, subqueries | SQL subset (time-series focus) | ClickHouse |
| **High-cardinality handling** | Excellent (columnar compression) | Good (STRING type) | ClickHouse |
| **License** | Apache 2.0 | Apache 2.0 | Tie |
| **Installation (no Docker)** | Single binary, trivial | Tarball with bundled JRE | Tie |

## Recommendation

**ClickHouse** is the stronger choice for this use case, primarily because:

1. **Ingestion performance**: 2.7× faster throughput matters at scale
2. **Compression**: 5.4× better disk efficiency is significant for 2-week retention
3. **SQL completeness**: Full SQL support simplifies the thin API layer
4. **High-cardinality columns**: Better optimized via columnar compression and
   `LowCardinality` type for auxiliary columns
5. **Ecosystem**: Broader client library support and tooling

**QuestDB's advantage** is simpler idempotency — dedup is transparent at query
time with no `FINAL` keyword needed. If operational simplicity of dedup is the
primary concern, QuestDB is compelling.

---

## Test Setup

### Environment
- **OS**: Linux (Ubuntu-based runner)
- **ClickHouse**: v26.3.1.350 (standalone binary)
- **QuestDB**: v9.3.3 (standalone tarball with bundled JRE)

### Installation (No Docker Required)

**ClickHouse:**
```bash
curl https://clickhouse.com/ | sh
./clickhouse server --daemon   # starts in background
./clickhouse client            # interactive shell
```

**QuestDB:**
```bash
curl -L https://github.com/questdb/questdb/releases/download/9.3.3/questdb-9.3.3-rt-linux-x86-64.tar.gz \
  -o questdb.tar.gz
tar xzf questdb.tar.gz
cd questdb-9.3.3-rt-linux-x86-64/bin
./questdb.sh start
```

### Data Model

A synthetic IoT event-tracking scenario with:
- **10,000,000 rows** total
- **800,000 unique `device_id` values** (high cardinality)
- Timestamps spanning **14 days** (2026-02-17 to 2026-03-02)
- **Zipf-distributed** device activity (some devices report much more frequently)
- 20 regions, 10 event types (low cardinality)
- Two float value columns

---

## Schema Design

### ClickHouse

```sql
CREATE TABLE events (
    timestamp DateTime64(3),
    device_id String,
    region LowCardinality(String),
    event_type LowCardinality(String),
    value_1 Float64,
    value_2 Float64
) ENGINE = ReplacingMergeTree()
ORDER BY (device_id, timestamp)
```

**Design choices:**
- **`ReplacingMergeTree`**: Deduplicates rows with identical `ORDER BY` keys
  during background merges. Combined with `insert_deduplication_token` for
  block-level idempotency.
- **`ORDER BY (device_id, timestamp)`**: Primary index supports efficient
  point-lookups by device_id and time-range scans.
- **`LowCardinality(String)`**: Dictionary encoding for region/event_type (~20
  and ~10 unique values respectively) — reduces memory and improves scan speed.
- **`DateTime64(3)`**: Millisecond timestamp precision.

**Dedup query patterns:**
- **`SELECT ... FROM events FINAL WHERE ...`**: Forces merge at query time.
  Simple but potentially slow for large result sets.
- **`argMax()` pattern**: `SELECT device_id, argMax(value_1, timestamp) FROM events GROUP BY device_id`.
  More efficient for aggregations but requires explicit grouping.
- **`insert_deduplication_token`**: Assign a token per insert batch; ClickHouse
  silently drops blocks with previously-seen tokens. Best for exactly-once
  delivery from message queues.

### QuestDB

```sql
CREATE TABLE events (
    timestamp TIMESTAMP,
    device_id STRING,
    region SYMBOL,
    event_type SYMBOL,
    value_1 DOUBLE,
    value_2 DOUBLE
) TIMESTAMP(timestamp) PARTITION BY DAY WAL
DEDUP UPSERT KEYS(timestamp, device_id)
```

**Design choices:**
- **`DEDUP UPSERT KEYS(timestamp, device_id)`**: Native WAL-level deduplication.
  Any row with the same (timestamp, device_id) replaces the previous one
  automatically. No special query syntax needed.
- **`SYMBOL`** for region/event_type: Indexed and dictionary-encoded, optimized
  for low-cardinality columns in QuestDB.
- **`STRING`** for device_id: High-cardinality columns should NOT use SYMBOL
  (would create an unbounded symbol table).
- **`PARTITION BY DAY`**: Enables efficient time-range pruning and makes
  2-week retention trivial (drop old partitions).
- **`WAL`** (Write-Ahead Log): Required for `DEDUP` support and ensures durability.

---

## Ingestion Benchmark

### Results

| Metric | ClickHouse | QuestDB |
|---|---|---|
| Ingestion method | clickhouse-connect (HTTP) | HTTP `/imp` (CSV import) |
| Total time | 22.3 seconds | 59.8 seconds |
| Throughput | **448,880 rows/s** | 167,163 rows/s |
| Disk usage | **146 MiB** | 785 MiB |
| Compression ratio | 3.0× (439→146 MiB) | ~1× (minimal) |
| Final row count | 9,998,192 | 9,998,192 |

Both databases correctly deduplicated 1,808 rows that had identical
`(device_id, timestamp)` pairs in the generated data, resulting in 9,998,192
final rows from 10,000,000 generated.

### Notes
- ClickHouse ingestion used the `clickhouse-connect` Python library (batch insert via HTTP)
- QuestDB ingestion used the HTTP `/imp` endpoint with multipart CSV upload
- QuestDB's PostgreSQL wire protocol (port 8812) failed on large batch INSERTs (100K+ values) —
  the HTTP CSV import is the recommended path for bulk loading
- Batch sizes: 100K rows for ClickHouse, 200K rows for QuestDB CSV import

---

## Idempotency Tests

Both databases **pass** the idempotency requirement.

### ClickHouse

| Test | Result |
|---|---|
| ReplacingMergeTree dedup (after OPTIMIZE FINAL) | ✅ PASS |
| SELECT ... FINAL (query-time dedup) | ✅ PASS |
| `insert_deduplication_token` (block-level) | ✅ PASS |

**How it works**: Insert 3 test rows, re-insert the exact same 3 rows, verify
count remains 3. `OPTIMIZE TABLE events FINAL` forces a merge that eliminates
duplicates. `SELECT ... FINAL` achieves the same at query time.

**Trade-off**: Without `FINAL` or explicit merge, duplicate rows exist in
storage until a background merge happens. Applications must either:
1. Always use `FINAL` in queries (simple but slower)
2. Use `argMax()` aggregation (more complex but efficient)
3. Accept eventual consistency (duplicates resolve during background merges)

### QuestDB

| Test | Result |
|---|---|
| DEDUP UPSERT KEYS (1st re-insert) | ✅ PASS |
| DEDUP UPSERT KEYS (2nd re-insert) | ✅ PASS |
| DEDUP UPSERT KEYS (3rd re-insert) | ✅ PASS |

**How it works**: The WAL-level `DEDUP UPSERT KEYS(timestamp, device_id)`
automatically replaces rows with matching keys. No special query syntax needed —
deduplication is transparent to the reader.

**Advantage**: Simpler application code. No `FINAL` keyword or `argMax` patterns.
The database guarantees a single row per (timestamp, device_id) at all times.

---

## Files in This Directory

| File | Description |
|---|---|
| `README.md` | This report |
| `notes.md` | Working notes and raw observations |
| `generate_and_ingest.py` | Python script for data generation, ingestion, and idempotency testing |
