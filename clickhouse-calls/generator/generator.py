"""
Call-leg event generator
========================
Generates three types of events that mirror a call-centre data model:

  Type 1 – agent_legs    : an agent (user) joined a call leg
  Type 2 – conversation_legs : a leg is mapped to a conversation
  Type 3 – csat_events   : CSAT survey result on the caller's leg

One *conversation* contains:
  • Exactly 1 caller leg  (emits type-2 and optionally type-3)
  • 1–4 agent legs        (each emits type-1 and type-2)

All legs eventually emit a type-2 event, but at staggered times so the
join table grows gradually, simulating real ingestion order.

Modes
-----
  bulk       – generate NUM_CONVOS conversations, insert everything, exit.
  streaming  – run forever, emitting EVENTS_PER_SEC synthetic events per second.
               Useful for watching ClickHouse ingest in real time.

Environment variables
---------------------
  CLICKHOUSE_HOST   (default: localhost)
  CLICKHOUSE_PORT   (default: 8123)
  CLICKHOUSE_DB     (default: calls)
  NUM_CONVOS        (default: 500)
  MODE              (default: streaming)
  EVENTS_PER_SEC    (default: 20)
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

import clickhouse_connect

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB   = os.getenv("CLICKHOUSE_DB", "calls")
NUM_CONVOS      = int(os.getenv("NUM_CONVOS", "500"))
MODE            = os.getenv("MODE", "streaming")          # "bulk" | "streaming"
EVENTS_PER_SEC  = float(os.getenv("EVENTS_PER_SEC", "20"))

# Simulated organisations
ORGS = [f"org-{i:03d}" for i in range(1, 11)]

# Simulated agent pool per org
AGENTS_PER_ORG = {org: [f"user-{org}-{i:04d}" for i in range(1, 51)] for org in ORGS}

# CSAT probability: 70 % of conversations offer CSAT; 80 % of those get a score
CSAT_OFFERED_PROB  = 0.70
CSAT_ANSWERED_PROB = 0.80


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Event:
    table: str
    row: dict


@dataclass
class Conversation:
    org_id:          str
    conversation_id: str
    caller_leg_id:   str
    agent_legs:      list[tuple[str, str]]   # [(leg_id, user_id), ...]
    base_time:       datetime
    # derived later
    events: list[Event] = field(default_factory=list)


# ── Generator ────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _leg_id() -> str:
    return str(uuid.uuid4())


def _conv_id() -> str:
    return str(uuid.uuid4())


def build_conversation(base_time: datetime | None = None) -> Conversation:
    """Build one conversation with its associated events."""
    org_id = random.choice(ORGS)
    base_time = base_time or _now_utc()

    num_agents   = random.randint(1, 4)
    caller_leg   = _leg_id()
    agent_leg_ids = [(_leg_id(), random.choice(AGENTS_PER_ORG[org_id])) for _ in range(num_agents)]
    conv_id      = _conv_id()

    convo = Conversation(
        org_id=org_id,
        conversation_id=conv_id,
        caller_leg_id=caller_leg,
        agent_legs=agent_leg_ids,
        base_time=base_time,
    )

    events: list[Event] = []

    # ── Type 1: agent legs ────────────────────────────────────────────────────
    # Agents join at slightly different times
    for i, (leg_id, user_id) in enumerate(agent_leg_ids):
        t = datetime(
            base_time.year, base_time.month, base_time.day,
            base_time.hour, base_time.minute, base_time.second,
            tzinfo=timezone.utc,
        )
        # stagger agent joins by 0–30 seconds
        offset_ms = random.randint(0, 30_000)
        ts = _offset_datetime(t, offset_ms)
        events.append(Event(
            table="agent_legs",
            row={"timestamp": ts, "org_id": org_id, "leg_id": leg_id, "user_id": user_id},
        ))

    # ── Type 2: conversation associations (all legs, staggered) ──────────────
    all_legs = [(caller_leg, "caller")] + [(lid, "agent") for lid, _ in agent_leg_ids]
    for leg_id, role in all_legs:
        # Caller joins immediately; agents stagger by up to 45 seconds
        offset_ms = 0 if role == "caller" else random.randint(0, 45_000)
        ts = _offset_datetime(base_time, offset_ms)
        events.append(Event(
            table="conversation_legs",
            row={"timestamp": ts, "org_id": org_id, "leg_id": leg_id, "conversation_id": conv_id},
        ))

    # ── Type 3: CSAT on caller leg ────────────────────────────────────────────
    offered = random.random() < CSAT_OFFERED_PROB
    score   = None
    if offered and random.random() < CSAT_ANSWERED_PROB:
        score = random.randint(1, 5)
    # CSAT arrives after the call (~60–300 s after base)
    csat_offset_ms = random.randint(60_000, 300_000)
    csat_ts = _offset_datetime(base_time, csat_offset_ms)
    events.append(Event(
        table="csat_events",
        row={
            "timestamp":    csat_ts,
            "org_id":       org_id,
            "leg_id":       caller_leg,
            "csat_offered": offered,
            "csat_score":   score,
        },
    ))

    convo.events = events
    return convo


def _offset_datetime(dt: datetime, offset_ms: int) -> datetime:
    from datetime import timedelta
    return dt + timedelta(milliseconds=offset_ms)


def conversations_stream(start_offset_seconds: int = 0) -> Iterator[Conversation]:
    """Yield conversations forever, with base_time marching forward."""
    base = datetime.now(tz=timezone.utc)
    while True:
        convo = build_conversation(base_time=base)
        yield convo
        # Next conversation starts 1–10 seconds later
        base = _offset_datetime(base, random.randint(1_000, 10_000))


# ── ClickHouse insert helpers ─────────────────────────────────────────────────

def connect(retries: int = 10, delay: float = 2.0) -> clickhouse_connect.driver.Client:
    for attempt in range(1, retries + 1):
        try:
            client = clickhouse_connect.get_client(
                host=CLICKHOUSE_HOST,
                port=CLICKHOUSE_PORT,
                database=CLICKHOUSE_DB,
            )
            client.ping()
            log.info("Connected to ClickHouse at %s:%s", CLICKHOUSE_HOST, CLICKHOUSE_PORT)
            return client
        except Exception as exc:
            log.warning("Connection attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(delay)


def insert_events(client: clickhouse_connect.driver.Client, events: list[Event]) -> None:
    """
    Group events by table and do one insert per table per call.
    ClickHouse performs best with columnar batched inserts.
    """
    by_table: dict[str, list[dict]] = {}
    for ev in events:
        by_table.setdefault(ev.table, []).append(ev.row)

    for table, rows in by_table.items():
        columns = list(rows[0].keys())
        data    = [[r[c] for c in columns] for r in rows]
        client.insert(
            table=f"{CLICKHOUSE_DB}.{table}",
            data=data,
            column_names=columns,
            settings={
                "async_insert": 1,
                "wait_for_async_insert": 0,
            },
        )


# ── Modes ─────────────────────────────────────────────────────────────────────

def run_bulk(client: clickhouse_connect.driver.Client) -> None:
    log.info("BULK mode: generating %d conversations …", NUM_CONVOS)
    all_events: list[Event] = []
    for i in range(NUM_CONVOS):
        convo = build_conversation()
        all_events.extend(convo.events)

    log.info("Inserting %d events …", len(all_events))
    insert_events(client, all_events)
    log.info("Done.")


def run_streaming(client: clickhouse_connect.driver.Client) -> None:
    log.info(
        "STREAMING mode: emitting ~%.0f events/sec (Ctrl-C to stop) …",
        EVENTS_PER_SEC,
    )
    sleep_per_event = 1.0 / EVENTS_PER_SEC
    total = 0

    # Buffer: collect a small batch before inserting
    BATCH_SIZE = max(1, int(EVENTS_PER_SEC))
    buffer: list[Event] = []

    for convo in conversations_stream():
        buffer.extend(convo.events)
        if len(buffer) >= BATCH_SIZE:
            insert_events(client, buffer)
            total += len(buffer)
            log.info("Inserted batch of %d | total=%d | latest convo=%s",
                     len(buffer), total, convo.conversation_id)
            buffer.clear()
            time.sleep(sleep_per_event * BATCH_SIZE)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    client = connect()
    if MODE == "bulk":
        run_bulk(client)
    else:
        run_streaming(client)


if __name__ == "__main__":
    main()
