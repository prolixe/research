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
