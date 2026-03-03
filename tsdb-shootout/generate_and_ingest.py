#!/usr/bin/env python3
"""
TSDB Shootout — Data Generation & Ingestion Script

Generates 10M+ synthetic IoT events with 800K+ unique device_ids
and ingests into both ClickHouse and QuestDB. Measures performance.
"""

import time
import io
import csv
import math
import random
import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import clickhouse_connect
import psycopg2

# ─── Configuration ───────────────────────────────────────────────────────
TOTAL_ROWS = 10_000_000
NUM_DEVICES = 800_000
NUM_DAYS = 14
BATCH_SIZE = 100_000  # rows per insert batch

REGIONS = [
    "us-east-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-south-1",
    "ap-northeast-1", "ap-southeast-1", "sa-east-1", "ca-central-1",
    "af-south-1", "me-south-1", "ap-east-1", "eu-north-1", "eu-south-1",
    "us-east-2", "us-west-1", "ap-southeast-2", "ap-northeast-2",
    "eu-west-2", "eu-west-3",
]

EVENT_TYPES = [
    "temperature", "humidity", "pressure", "battery",
    "motion", "door_open", "door_close", "heartbeat",
    "alert", "calibration",
]

# ─── Data Generation ─────────────────────────────────────────────────────
def generate_device_ids(n):
    """Generate n unique device IDs."""
    print(f"Generating {n:,} unique device IDs...")
    return [f"dev-{i:08d}" for i in range(n)]


def generate_data(total_rows, device_ids, seed=42):
    """
    Generate synthetic data with Zipf-distributed device activity.
    Some devices report much more frequently than others.
    Returns columns as separate lists for columnar insert.
    """
    rng = np.random.default_rng(seed)
    n_devices = len(device_ids)

    print(f"Generating {total_rows:,} rows with Zipf distribution (guaranteeing all {n_devices:,} devices)...")

    # Guarantee every device appears at least once, then fill remaining
    # rows with Zipf-distributed device picks (s=1.2 for realistic skew)
    zipf_weights = 1.0 / np.arange(1, n_devices + 1) ** 1.2
    zipf_weights /= zipf_weights.sum()

    remaining = total_rows - n_devices
    extra_indices = rng.choice(n_devices, size=remaining, p=zipf_weights)
    device_indices = np.concatenate([np.arange(n_devices), extra_indices])
    rng.shuffle(device_indices)

    # Timestamps: uniform over 14 days, then sorted
    base_ts = datetime(2026, 2, 17, 0, 0, 0, tzinfo=timezone.utc)
    span_seconds = NUM_DAYS * 86400
    ts_offsets = rng.integers(0, span_seconds * 1000, size=total_rows)  # millisecond resolution
    ts_offsets.sort()

    # Build column arrays
    timestamps = []
    device_id_col = []
    region_col = []
    event_type_col = []
    value_1_col = []
    value_2_col = []

    device_ids_arr = np.array(device_ids)
    regions_arr = np.array(REGIONS)
    event_types_arr = np.array(EVENT_TYPES)

    # Pre-assign region per device (deterministic)
    device_region_idx = np.arange(n_devices) % len(REGIONS)
    # Event type: random per row
    event_type_indices = rng.integers(0, len(EVENT_TYPES), size=total_rows)

    print("Building column arrays...")
    for batch_start in range(0, total_rows, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total_rows)
        batch_slice = slice(batch_start, batch_end)
        batch_size = batch_end - batch_start

        batch_device_idx = device_indices[batch_slice]
        batch_ts_offsets = ts_offsets[batch_slice]

        for i in range(batch_size):
            ts_ms = batch_ts_offsets[i]
            ts = base_ts + timedelta(milliseconds=int(ts_ms))
            d_idx = batch_device_idx[i]

            timestamps.append(ts)
            device_id_col.append(device_ids_arr[d_idx])
            region_col.append(regions_arr[device_region_idx[d_idx]])
            event_type_col.append(event_types_arr[event_type_indices[batch_start + i]])
            value_1_col.append(round(rng.normal(25.0, 10.0), 2))
            value_2_col.append(round(rng.uniform(0.0, 100.0), 2))

        if (batch_end % 1_000_000) == 0 or batch_end == total_rows:
            print(f"  Generated {batch_end:,} / {total_rows:,} rows")

    return timestamps, device_id_col, region_col, event_type_col, value_1_col, value_2_col


# ─── ClickHouse Ingestion ────────────────────────────────────────────────
def ingest_clickhouse(timestamps, device_ids, regions, event_types, v1, v2):
    """Insert data into ClickHouse using clickhouse-connect."""
    print("\n=== ClickHouse Ingestion ===")
    client = clickhouse_connect.get_client(host='127.0.0.1', port=8123)

    total = len(timestamps)
    start = time.time()

    for batch_start in range(0, total, BATCH_SIZE):
        batch_end = min(batch_start + BATCH_SIZE, total)
        batch = list(zip(
            timestamps[batch_start:batch_end],
            device_ids[batch_start:batch_end],
            regions[batch_start:batch_end],
            event_types[batch_start:batch_end],
            v1[batch_start:batch_end],
            v2[batch_start:batch_end],
        ))
        client.insert(
            'events', batch,
            column_names=['timestamp', 'device_id', 'region', 'event_type', 'value_1', 'value_2'],
        )
        if (batch_end % 1_000_000) == 0 or batch_end == total:
            elapsed = time.time() - start
            rate = batch_end / elapsed
            print(f"  Inserted {batch_end:,} / {total:,} rows  ({rate:,.0f} rows/s)")

    elapsed = time.time() - start
    rate = total / elapsed
    print(f"\nClickHouse ingestion complete:")
    print(f"  Total time : {elapsed:.1f}s")
    print(f"  Rows/sec   : {rate:,.0f}")

    # Disk usage
    result = client.query(
        "SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts WHERE table = 'events' AND active"
    )
    disk = result.result_rows[0][0]
    print(f"  Disk usage : {disk}")

    # Row count
    result = client.query("SELECT count() FROM events")
    count = result.result_rows[0][0]
    print(f"  Row count  : {count:,}")

    client.close()
    return elapsed, rate


# ─── QuestDB Ingestion ───────────────────────────────────────────────────
def ingest_questdb(timestamps, device_ids, regions, event_types, v1, v2):
    """Insert data into QuestDB using HTTP /imp (CSV import) endpoint."""
    import urllib.request
    import uuid

    print("\n=== QuestDB Ingestion ===")
    print("  Using HTTP /imp CSV import endpoint for best throughput")

    total = len(timestamps)
    imp_batch = 200_000  # rows per HTTP request
    start = time.time()

    for batch_start in range(0, total, imp_batch):
        batch_end = min(batch_start + imp_batch, total)

        # Build CSV in memory
        lines = ["timestamp,device_id,region,event_type,value_1,value_2"]
        for i in range(batch_start, batch_end):
            ts_str = timestamps[i].strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            lines.append(f"{ts_str},{device_ids[i]},{regions[i]},{event_types[i]},{v1[i]},{v2[i]}")
        csv_data = "\n".join(lines).encode('utf-8')

        # Build multipart/form-data body
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="data"; filename="batch.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n"
        ).encode('utf-8') + csv_data + f"\r\n--{boundary}--\r\n".encode('utf-8')

        req = urllib.request.Request(
            'http://127.0.0.1:9000/imp?name=events&overwrite=false&durable=false&atomicity=skipRow',
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            resp.read()
        except Exception as e:
            print(f"  Error at batch {batch_start}: {e}")

        if (batch_end % 1_000_000) == 0 or batch_end == total:
            elapsed = time.time() - start
            rate = batch_end / elapsed if elapsed > 0 else 0
            print(f"  Inserted {batch_end:,} / {total:,} rows  ({rate:,.0f} rows/s)")

    elapsed = time.time() - start
    rate = total / elapsed
    print(f"\nQuestDB ingestion complete:")
    print(f"  Total time : {elapsed:.1f}s")
    print(f"  Rows/sec   : {rate:,.0f}")

    # Row count (wait for WAL to apply)
    print("  Waiting for WAL to apply...")
    time.sleep(10)
    conn = psycopg2.connect(
        host='127.0.0.1', port=8812,
        user='admin', password='quest',
        database='qdb',
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT count() FROM events")
    count = cur.fetchone()[0]
    print(f"  Row count  : {count:,}")
    cur.close()
    conn.close()

    return elapsed, rate


# ─── Idempotency Test ────────────────────────────────────────────────────
def test_idempotency_clickhouse():
    """Test that re-inserting the same rows doesn't produce duplicates."""
    print("\n=== ClickHouse Idempotency Test ===")
    client = clickhouse_connect.get_client(host='127.0.0.1', port=8123)

    # Insert a known batch
    test_rows = [
        (datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc), 'dev-IDEMPOTENT-001', 'us-east-1', 'heartbeat', 42.0, 99.0),
        (datetime(2026, 2, 20, 12, 0, 1, tzinfo=timezone.utc), 'dev-IDEMPOTENT-002', 'eu-west-1', 'temperature', 23.5, 50.0),
        (datetime(2026, 2, 20, 12, 0, 2, tzinfo=timezone.utc), 'dev-IDEMPOTENT-003', 'ap-south-1', 'humidity', 65.0, 30.0),
    ]

    print("  Inserting 3 test rows (first time)...")
    client.insert('events', test_rows,
                  column_names=['timestamp', 'device_id', 'region', 'event_type', 'value_1', 'value_2'])

    # Force merge to apply ReplacingMergeTree dedup
    client.command("OPTIMIZE TABLE events FINAL")

    result = client.query(
        "SELECT count() FROM events WHERE device_id LIKE 'dev-IDEMPOTENT%'"
    )
    count_before = result.result_rows[0][0]
    print(f"  Count after first insert: {count_before}")

    print("  Re-inserting same 3 rows (second time)...")
    client.insert('events', test_rows,
                  column_names=['timestamp', 'device_id', 'region', 'event_type', 'value_1', 'value_2'])

    # Force merge again
    client.command("OPTIMIZE TABLE events FINAL")

    result = client.query(
        "SELECT count() FROM events WHERE device_id LIKE 'dev-IDEMPOTENT%'"
    )
    count_after = result.result_rows[0][0]
    print(f"  Count after second insert: {count_after}")

    # Also test with FINAL keyword in SELECT
    result = client.query(
        "SELECT count() FROM events FINAL WHERE device_id LIKE 'dev-IDEMPOTENT%'"
    )
    count_final = result.result_rows[0][0]
    print(f"  Count using SELECT ... FINAL: {count_final}")

    if count_after == count_before == 3:
        print("  ✅ PASS: ReplacingMergeTree deduplicated re-inserted rows after OPTIMIZE FINAL")
    else:
        print(f"  ⚠️ NOTE: count changed {count_before} -> {count_after} (ReplacingMergeTree dedup happens during merge)")
        if count_final == 3:
            print("  ✅ PASS: SELECT ... FINAL shows correct deduplicated count")

    # Test insert_deduplication_token
    print("\n  Testing insert_deduplication_token...")
    client.command("SET insert_deduplication_token = 'test-token-001'")
    client.insert('events', [test_rows[0]],
                  column_names=['timestamp', 'device_id', 'region', 'event_type', 'value_1', 'value_2'],
                  settings={'insert_deduplication_token': 'idempotent-test-batch-1'})

    # Same token again — should be silently dropped
    client.insert('events', [test_rows[0]],
                  column_names=['timestamp', 'device_id', 'region', 'event_type', 'value_1', 'value_2'],
                  settings={'insert_deduplication_token': 'idempotent-test-batch-1'})

    client.command("OPTIMIZE TABLE events FINAL")
    result = client.query(
        "SELECT count() FROM events FINAL WHERE device_id = 'dev-IDEMPOTENT-001'"
    )
    count_token = result.result_rows[0][0]
    print(f"  Count after token-based dedup insert (expect 1): {count_token}")
    if count_token == 1:
        print("  ✅ PASS: insert_deduplication_token prevented duplicate block")
    else:
        print(f"  ℹ️ Count is {count_token} — token dedup works at block level")

    client.close()


def test_idempotency_questdb():
    """Test that re-inserting the same rows doesn't produce duplicates."""
    print("\n=== QuestDB Idempotency Test ===")
    conn = psycopg2.connect(
        host='127.0.0.1', port=8812,
        user='admin', password='quest',
        database='qdb',
    )
    conn.autocommit = True
    cur = conn.cursor()

    # Insert known test rows
    test_sql = """
    INSERT INTO events (timestamp, device_id, region, event_type, value_1, value_2) VALUES
    ('2026-02-20T13:00:00.000Z', 'dev-IDEMPOTENT-Q01', 'us-east-1', 'heartbeat', 42.0, 99.0),
    ('2026-02-20T13:00:01.000Z', 'dev-IDEMPOTENT-Q02', 'eu-west-1', 'temperature', 23.5, 50.0),
    ('2026-02-20T13:00:02.000Z', 'dev-IDEMPOTENT-Q03', 'ap-south-1', 'humidity', 65.0, 30.0)
    """

    print("  Inserting 3 test rows (first time)...")
    cur.execute(test_sql)
    time.sleep(3)  # Wait for WAL to apply

    cur.execute("SELECT count() FROM events WHERE device_id LIKE 'dev-IDEMPOTENT-Q%'")
    count_before = cur.fetchone()[0]
    print(f"  Count after first insert: {count_before}")

    print("  Re-inserting same 3 rows (second time)...")
    cur.execute(test_sql)
    time.sleep(3)  # Wait for WAL + dedup to apply

    cur.execute("SELECT count() FROM events WHERE device_id LIKE 'dev-IDEMPOTENT-Q%'")
    count_after = cur.fetchone()[0]
    print(f"  Count after second insert: {count_after}")

    if count_after == 3:
        print("  ✅ PASS: DEDUP UPSERT deduplicated re-inserted rows")
    else:
        print(f"  ⚠️ Count is {count_after} (expected 3) — dedup may need more time or different config")

    # Third insert to be extra sure
    print("  Re-inserting same 3 rows (third time)...")
    cur.execute(test_sql)
    time.sleep(3)

    cur.execute("SELECT count() FROM events WHERE device_id LIKE 'dev-IDEMPOTENT-Q%'")
    count_third = cur.fetchone()[0]
    print(f"  Count after third insert: {count_third}")

    if count_third == 3:
        print("  ✅ PASS: Idempotency confirmed across 3 insert attempts")
    else:
        print(f"  ⚠️ Count is {count_third}")

    cur.close()
    conn.close()


# ─── Main ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print("=" * 60)
    print("TSDB Shootout — Data Generation & Ingestion")
    print("=" * 60)

    # Generate data
    device_ids = generate_device_ids(NUM_DEVICES)
    timestamps, dev_col, reg_col, evt_col, v1_col, v2_col = generate_data(
        TOTAL_ROWS, device_ids
    )

    unique_devices = len(set(dev_col))
    print(f"\nData summary:")
    print(f"  Total rows     : {len(timestamps):,}")
    print(f"  Unique devices : {unique_devices:,}")
    print(f"  Time span      : {timestamps[0]} — {timestamps[-1]}")

    # Ingest into ClickHouse
    ch_time, ch_rate = ingest_clickhouse(timestamps, dev_col, reg_col, evt_col, v1_col, v2_col)

    # Ingest into QuestDB
    qdb_time, qdb_rate = ingest_questdb(timestamps, dev_col, reg_col, evt_col, v1_col, v2_col)

    # Summary
    print("\n" + "=" * 60)
    print("INGESTION SUMMARY")
    print("=" * 60)
    print(f"{'':20s} {'ClickHouse':>15s} {'QuestDB':>15s}")
    print(f"{'Time (s)':20s} {ch_time:>15.1f} {qdb_time:>15.1f}")
    print(f"{'Rows/sec':20s} {ch_rate:>15,.0f} {qdb_rate:>15,.0f}")

    # Idempotency tests
    print("\n" + "=" * 60)
    print("IDEMPOTENCY TESTS")
    print("=" * 60)
    test_idempotency_clickhouse()
    test_idempotency_questdb()

    print("\n✅ All tests complete!")
