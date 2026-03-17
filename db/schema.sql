-- db/schema.sql

-- Core append-only event log
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    stream_id     TEXT        NOT NULL,
    version       BIGINT      NOT NULL,
    event_type    TEXT        NOT NULL,
    event_data    JSONB       NOT NULL DEFAULT '{}',
    metadata      JSONB       NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT events_stream_version_unique UNIQUE (stream_id, version)
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id ON events (stream_id);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);

-- Tracks the current (latest) version of each stream — used for optimistic concurrency
CREATE TABLE IF NOT EXISTS event_streams (
    stream_id      TEXT        PRIMARY KEY,
    current_version BIGINT     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tracks how far each named projection has read through the events table
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name TEXT        PRIMARY KEY,
    last_event_id   BIGINT      NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Outbox for guaranteed at-least-once delivery to downstream consumers
CREATE TABLE IF NOT EXISTS outbox (
    id            BIGSERIAL   PRIMARY KEY,
    event_id      BIGINT      NOT NULL REFERENCES events(id),
    destination   TEXT        NOT NULL,
    payload       JSONB       NOT NULL DEFAULT '{}',
    status        TEXT        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts      INT         NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox (status);