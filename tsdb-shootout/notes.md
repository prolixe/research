# TSDB Shootout — Working Notes

## 2026-03-03: Project Start

### Goal
Compare ClickHouse vs QuestDB for a non-metrics time-series use case with:
- High cardinality (800K+ unique device_ids)
- 2-week retention
- Idempotent writes
- SQL query support
- Open-source license

### Plan
1. Install both databases (standalone binaries, no Docker)
2. Design idiomatic schemas for each
3. Generate 10M+ rows of synthetic data and ingest
4. Test idempotency
5. Document findings

---

## Installation Notes

### ClickHouse
- Installed via `curl https://clickhouse.com/ | sh` in /tmp
- Version: 26.3.1.350
- Started with `./clickhouse server --daemon`
- Native protocol: [::1]:9000 (IPv6 loopback)
- HTTP API: 127.0.0.1:8123 and [::1]:8123

### QuestDB
- Downloaded tarball: questdb-9.3.3-rt-linux-x86-64.tar.gz
- Started with `./questdb.sh start`
- REST API / Web Console: 0.0.0.0:9000 (shares port with ClickHouse but on IPv4)
- PostgreSQL wire protocol: 0.0.0.0:8812
- ILP (InfluxDB Line Protocol): 0.0.0.0:9009

Port overlap: QuestDB got IPv4:9000, ClickHouse got IPv6:9000. Both accessible.

---

## Schema Design

### ClickHouse Schema
Using `ReplacingMergeTree` engine:
- `ORDER BY (device_id, timestamp)` — supports both high-cardinality device lookups and time-range queries
- ReplacingMergeTree deduplicates rows with the same sort key during merges
- For query-time dedup: use `FINAL` modifier or `argMax()` pattern
- `insert_deduplication_token` can provide insert-level idempotency (block-level)
- Trade-offs: `FINAL` is simpler but can be slower; `argMax` requires manual grouping but avoids merge overhead

### QuestDB Schema
Using WAL table with deduplication:
- `DEDUP UPSERT KEYS(timestamp, device_id)` — native idempotent writes
- `SYMBOL` type for `region` and `event_type` (low cardinality, indexed)
- `device_id` stays as `STRING` (high cardinality, not suitable for SYMBOL)
- Timestamp is designated timestamp for time-series optimizations
- High cardinality dedup keys may have performance implications — measuring this

---

## Ingestion Results

| Metric | ClickHouse | QuestDB |
|---|---|---|
| Total time | 22.3s | 59.8s |
| Rows/sec | 448,880 | 167,163 |
| Disk usage | 146 MiB (compressed from 439 MiB) | ~785 MiB |
| Row count | 9,998,192 | 9,998,192 |
| Ingestion method | clickhouse-connect HTTP | HTTP /imp CSV import |

Note: Both DBs ended up with 9,998,192 rows instead of 10,000,000.
This is because 1,808 rows had identical (device_id, timestamp) pairs
in the generated data, and both databases correctly deduplicated them.

### Observations
- ClickHouse is ~2.7x faster at ingestion
- ClickHouse has 5.4x better compression (146 MiB vs 785 MiB)
- QuestDB /imp endpoint requires multipart/form-data, not plain text/csv
- QuestDB pgwire protocol struggles with large batch INSERTs (100K+ rows)
- ClickHouse's HTTP interface worked well with clickhouse-connect library

---

## Idempotency Test Results

### ClickHouse
- ReplacingMergeTree: ✅ PASS — dedup after OPTIMIZE TABLE ... FINAL
- SELECT ... FINAL: ✅ PASS — correct count without explicit merge
- insert_deduplication_token: ✅ PASS — block-level idempotency

Trade-offs:
- `FINAL` keyword: Simple in queries, forces merge at query time, can be slower
- `argMax()` pattern: Select latest version via aggregation, avoids merge overhead
- `insert_deduplication_token`: Block-level dedup at insert time, best for MQ delivery

### QuestDB
- DEDUP UPSERT KEYS: ✅ PASS — 3 consecutive inserts of same data, count stays at 3
- WAL-based deduplication is transparent and automatic
- No special query syntax needed (unlike ClickHouse's FINAL)

---

## Lessons Learned
- ClickHouse IPv6 literal `::1` breaks urllib3 URL parsing — use 127.0.0.1 for HTTP
- QuestDB /imp needs multipart/form-data, not raw CSV body
- Zipf distribution with s=1.2 only hits ~330K of 800K devices — need guaranteed base coverage
- Both DBs handle dedup correctly but through different mechanisms
