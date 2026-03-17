-- =============================================================================
-- Schema for call-leg event tracking
--
-- Three event types land in three separate tables.
-- conversation_legs is the "join spine": it maps every leg_id → conversation_id,
-- which lets you find all legs that shared a conversation with any given leg.
--
-- Query pattern: "give me all legs in the same conversation as leg X"
--
--   SELECT cl2.leg_id, cl2.conversation_id
--   FROM calls.conversation_legs AS cl1
--   JOIN calls.conversation_legs AS cl2 USING (org_id, conversation_id)
--   WHERE cl1.org_id = 'acme' AND cl1.leg_id = 'leg-abc'
--     AND cl2.leg_id != cl1.leg_id;
-- =============================================================================

CREATE DATABASE IF NOT EXISTS calls;

-- -----------------------------------------------------------------------------
-- Type 1 – Agent leg
-- Emitted when an agent (user) is associated with a call leg.
-- Many agents can share a conversation (via conversation_legs).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls.agent_legs
(
    timestamp       DateTime64(3, 'UTC'),
    org_id          LowCardinality(String),
    leg_id          String,
    user_id         String
)
ENGINE = MergeTree()
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, leg_id, timestamp)
TTL timestamp + INTERVAL 2 YEAR
SETTINGS index_granularity = 8192;

-- -----------------------------------------------------------------------------
-- Type 2 – Conversation association
-- Emitted when a leg is mapped to a conversation.
-- Uses ReplacingMergeTree so a re-association keeps the newest row.
-- Both caller legs and agent legs emit this event.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls.conversation_legs
(
    timestamp       DateTime64(3, 'UTC'),
    org_id          LowCardinality(String),
    leg_id          String,
    conversation_id String
)
ENGINE = ReplacingMergeTree(timestamp)
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, leg_id)
TTL timestamp + INTERVAL 2 YEAR
SETTINGS index_granularity = 8192;

-- Speeds up "find all legs in conversation X" scans
CREATE INDEX IF NOT EXISTS idx_conv_id ON calls.conversation_legs (conversation_id) TYPE bloom_filter GRANULARITY 1;

-- -----------------------------------------------------------------------------
-- Type 3 – CSAT (Customer Satisfaction)
-- Emitted for the **caller** leg only.
-- csat_score is NULL when the survey was offered but not answered.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls.csat_events
(
    timestamp       DateTime64(3, 'UTC'),
    org_id          LowCardinality(String),
    leg_id          String,           -- caller leg id
    csat_offered    Bool,
    csat_score      Nullable(UInt8)   -- 1-5, NULL if not answered
)
ENGINE = ReplacingMergeTree(timestamp)
PARTITION BY (org_id, toYYYYMM(timestamp))
ORDER BY (org_id, leg_id)
TTL timestamp + INTERVAL 2 YEAR
SETTINGS index_granularity = 8192;

-- =============================================================================
-- Materialized view: denormalised per-conversation summary
-- Keeps a running picture: conversation → caller leg + CSAT + agent legs
-- Useful for dashboards without joining at query time.
-- =============================================================================
CREATE TABLE IF NOT EXISTS calls.conversation_summary
(
    org_id              LowCardinality(String),
    conversation_id     String,
    -- caller side
    caller_leg_id       String,
    csat_offered        Bool    DEFAULT false,
    csat_score          Nullable(UInt8),
    -- aggregated agent side (stored as arrays for simplicity)
    agent_leg_ids       Array(String),
    agent_user_ids      Array(String),
    -- timing
    first_event_at      DateTime64(3, 'UTC'),
    last_event_at       DateTime64(3, 'UTC')
)
ENGINE = AggregatingMergeTree()
PARTITION BY org_id
ORDER BY (org_id, conversation_id);
