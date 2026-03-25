"""
Upcaster Registry Tests

Verifies that:
- CreditAnalysisCompleted v1 events are correctly upcasted to v2
- DecisionGenerated v1 events are correctly upcasted to v2
- The immutability guarantee holds: upcasting is a read-time transform,
  the stored payload in the events table is NEVER mutated
- Version chains (v1 -> v2) are applied automatically
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.models.events import StoredEvent
from src.upcasting.registry import UpcasterRegistry, registry
from src.upcasting.upcasters import upcast_credit_analysis_v1_to_v2, upcast_decision_v1_to_v2


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost/apex_ledger_test"
)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


@pytest.fixture
async def pool():
    """Create a fresh test database pool and apply schema."""
    p = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, init=_init_connection)

    with open(SCHEMA_PATH) as f:
        schema_sql = f.read()

    async with p.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS outbox CASCADE")
        await conn.execute("DROP TABLE IF EXISTS snapshots CASCADE")
        await conn.execute("DROP TABLE IF EXISTS projection_checkpoints CASCADE")
        await conn.execute("DROP TABLE IF EXISTS events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS event_streams CASCADE")
        await conn.execute(schema_sql)

    yield p
    await p.close()


@pytest.fixture
def store(pool) -> EventStore:
    return EventStore(pool)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stored_event(
    event_type: str,
    event_version: int,
    payload: dict,
    stream_id: str = "loan-TEST-001",
    stream_position: int = 1,
    global_position: int = 1,
) -> StoredEvent:
    """Create a StoredEvent in memory for unit-level upcaster tests."""
    return StoredEvent(
        event_id=uuid.uuid4(),
        stream_id=stream_id,
        stream_position=stream_position,
        global_position=global_position,
        event_type=event_type,
        event_version=event_version,
        payload=payload,
        metadata={},
        recorded_at=datetime(2024, 6, 15, 12, 0, 0),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upcast_credit_analysis_v1_to_v2():
    """A v1 CreditAnalysisCompleted should be upcasted to v2 with new fields."""
    v1_payload = {
        "event_type": "CreditAnalysisCompleted",
        "application_id": "APP-001",
        "agent_id": "agent-credit-001",
        "session_id": "session-001",
        "risk_tier": "MEDIUM",
        "recommended_limit_usd": 400_000.0,
        "analysis_duration_ms": 1500,
        "input_data_hash": "abc123",
        "recorded_at": "2024-06-15T12:00:00Z",
    }

    v1_event = _make_stored_event(
        event_type="CreditAnalysisCompleted",
        event_version=1,
        payload=v1_payload,
    )

    upcasted = registry.upcast(v1_event)

    # Version should be bumped to 2
    assert upcasted.event_version == 2

    # v2 fields should be present
    assert "model_version" in upcasted.payload
    assert upcasted.payload["model_version"] is not None  # inferred from timestamp
    assert upcasted.payload["confidence_score"] is None  # genuinely unknown
    assert "regulatory_basis" in upcasted.payload
    assert upcasted.payload["regulatory_basis"] is not None

    # Original fields preserved
    assert upcasted.payload["application_id"] == "APP-001"
    assert upcasted.payload["risk_tier"] == "MEDIUM"
    assert upcasted.payload["recommended_limit_usd"] == 400_000.0

    # Original event is NOT mutated
    assert v1_event.event_version == 1
    assert "model_version" not in v1_event.payload


@pytest.mark.asyncio
async def test_upcast_decision_v1_to_v2():
    """A v1 DecisionGenerated should be upcasted to v2 with new fields."""
    v1_payload = {
        "event_type": "DecisionGenerated",
        "application_id": "APP-002",
        "orchestrator_agent_id": "orchestrator-001",
        "recommendation": "APPROVE",
        "decision_basis_summary": "Strong financials, low fraud risk.",
    }

    v1_event = _make_stored_event(
        event_type="DecisionGenerated",
        event_version=1,
        payload=v1_payload,
    )

    upcasted = registry.upcast(v1_event)

    # Version should be bumped to 2
    assert upcasted.event_version == 2

    # v2 fields should be present
    assert upcasted.payload["confidence_score"] is None  # genuinely unknown
    assert upcasted.payload["contributing_agent_sessions"] == []
    assert upcasted.payload["model_versions"] == {}

    # Original fields preserved
    assert upcasted.payload["application_id"] == "APP-002"
    assert upcasted.payload["recommendation"] == "APPROVE"
    assert upcasted.payload["decision_basis_summary"] == "Strong financials, low fraud risk."

    # Original event is NOT mutated
    assert v1_event.event_version == 1
    assert "confidence_score" not in v1_event.payload


@pytest.mark.asyncio
async def test_immutability_guarantee(pool, store: EventStore):
    """
    THE MANDATORY TEST: Upcasting MUST NOT mutate stored data.

    1. Insert a v1 event directly into the events table via raw SQL
    2. Load the same event through EventStore.load_stream()
    3. Apply the upcaster — verify it produces v2
    4. Query raw events table — verify stored payload is UNCHANGED
    """
    stream_id = "loan-IMMUT-001"
    event_id = uuid.uuid4()

    v1_payload = {
        "event_type": "CreditAnalysisCompleted",
        "application_id": "IMMUT-001",
        "agent_id": "agent-credit-001",
        "session_id": "session-001",
        "risk_tier": "HIGH",
        "recommended_limit_usd": 250_000.0,
        "analysis_duration_ms": 2000,
        "input_data_hash": "immut-hash",
        "recorded_at": "2024-03-01T10:00:00Z",
    }

    # Step 1: Insert directly via raw SQL — simulating a v1 event already in the store
    async with pool.acquire() as conn:
        # Create the stream first
        await conn.execute(
            "INSERT INTO event_streams (stream_id, aggregate_type, current_version) "
            "VALUES ($1, $2, 1)",
            stream_id, "LoanApplication",
        )
        await conn.execute(
            "INSERT INTO events (event_id, stream_id, stream_position, event_type, "
            "event_version, payload, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)",
            event_id, stream_id, 1, "CreditAnalysisCompleted", 1,
            __import__("json").dumps(v1_payload),
            __import__("json").dumps({}),
        )

    # Step 2: Load through EventStore
    loaded_events = await store.load_stream(stream_id)
    assert len(loaded_events) == 1
    loaded_event = loaded_events[0]
    assert loaded_event.event_version == 1  # store returns raw version

    # Step 3: Apply upcaster manually (as a consumer would)
    upcasted = registry.upcast(loaded_event)
    assert upcasted.event_version == 2
    assert upcasted.payload["confidence_score"] is None
    assert "model_version" in upcasted.payload
    assert "regulatory_basis" in upcasted.payload

    # Step 4: Query the raw events table — stored payload must be UNCHANGED
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT event_version, payload FROM events WHERE event_id = $1",
            event_id,
        )

    assert row["event_version"] == 1, (
        "Stored event_version was mutated! Must remain 1."
    )
    raw_payload = row["payload"]
    assert "model_version" not in raw_payload, (
        "Stored payload was mutated — 'model_version' should NOT be in the raw row."
    )
    assert "confidence_score" not in raw_payload, (
        "Stored payload was mutated — 'confidence_score' should NOT be in the raw row."
    )
    assert "regulatory_basis" not in raw_payload, (
        "Stored payload was mutated — 'regulatory_basis' should NOT be in the raw row."
    )
    # Verify original fields are intact
    assert raw_payload["application_id"] == "IMMUT-001"
    assert raw_payload["risk_tier"] == "HIGH"

    print("\n--- Immutability Guarantee Test PASSED ---")
    print(f"  Upcasted version: {upcasted.event_version}")
    print(f"  Stored version:   {row['event_version']}")
    print(f"  Upcasted has model_version: {'model_version' in upcasted.payload}")
    print(f"  Stored has model_version:   {'model_version' in raw_payload}")


@pytest.mark.asyncio
async def test_upcast_chain():
    """
    Version chain v1 -> v2 is applied automatically by the registry.

    If an event is at v1 and there is a registered upcaster for v1 -> v2,
    a single call to registry.upcast() should produce a v2 event.
    """
    # Use a fresh registry to isolate the chain test
    test_registry = UpcasterRegistry()

    @test_registry.register("TestEvent", from_version=1)
    def v1_to_v2(payload: dict) -> dict:
        return {**payload, "added_in_v2": True}

    @test_registry.register("TestEvent", from_version=2)
    def v2_to_v3(payload: dict) -> dict:
        return {**payload, "added_in_v3": "final"}

    v1_event = _make_stored_event(
        event_type="TestEvent",
        event_version=1,
        payload={"original": "data"},
    )

    # Single upcast call should chain v1 -> v2 -> v3
    result = test_registry.upcast(v1_event)
    assert result.event_version == 3
    assert result.payload["original"] == "data"
    assert result.payload["added_in_v2"] is True
    assert result.payload["added_in_v3"] == "final"

    # A v2 event should only go through v2 -> v3
    v2_event = _make_stored_event(
        event_type="TestEvent",
        event_version=2,
        payload={"original": "data", "added_in_v2": True},
    )
    result2 = test_registry.upcast(v2_event)
    assert result2.event_version == 3
    assert result2.payload["added_in_v3"] == "final"

    # A v3 event should pass through unchanged
    v3_event = _make_stored_event(
        event_type="TestEvent",
        event_version=3,
        payload={"original": "data", "added_in_v2": True, "added_in_v3": "final"},
    )
    result3 = test_registry.upcast(v3_event)
    assert result3.event_version == 3
    assert result3.payload == v3_event.payload

    # Original v1 event must not be mutated
    assert v1_event.event_version == 1
    assert "added_in_v2" not in v1_event.payload

    print("\n--- Upcast Chain Test PASSED ---")
