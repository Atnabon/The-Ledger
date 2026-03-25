"""
Gas Town Crash Recovery Tests

Verifies that an AI agent can reconstruct its exact context from the event
store after a crash and continue where it left off.

Tests:
- Basic context reconstruction from a sequence of node execution events
- Context reconstruction after a simulated crash (no in-memory agent)
- NEEDS_RECONCILIATION detection when output is written but session not completed
- Empty session handling
- Completed session detection
"""

from __future__ import annotations

import os

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.models.events import (
    AgentSessionStarted,
    AgentContextLoaded,
    AgentNodeExecuted,
    AgentOutputWritten,
    AgentSessionCompleted,
)
from src.integrity.gas_town import reconstruct_agent_context


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
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconstruct_agent_context_basic(store: EventStore):
    """
    Start an agent session, append several node execution events,
    then call reconstruct_agent_context() and verify the context
    has correct completed_nodes, last_successful_node, and health status.
    """
    agent_id = "agent-gt-basic"
    session_id = "sess-gt-basic"
    stream_id = f"agent-{agent_id}-{session_id}"

    # Session started
    e1 = AgentSessionStarted.create(
        agent_id=agent_id,
        session_id=session_id,
        agent_type="credit_analyzer",
        model_version="v2.3",
        context_source="event_store",
        context_token_count=4000,
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    # Context loaded
    e2 = AgentContextLoaded.create(
        agent_id=agent_id,
        session_id=session_id,
        context_source="event_store",
        event_replay_from_position=0,
        context_token_count=4000,
        model_version="v2.3",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    # Node executions
    nodes = ["validate_inputs", "open_aggregate_record", "load_external_data"]
    version = 2
    for i, node in enumerate(nodes):
        e = AgentNodeExecuted.create(
            agent_id=agent_id,
            session_id=session_id,
            node_name=node,
            node_sequence=i + 1,
            input_keys=["input_data"],
            output_keys=["output_data"],
            llm_called=(node == "load_external_data"),
            llm_tokens_input=500 if node == "load_external_data" else None,
            llm_tokens_output=200 if node == "load_external_data" else None,
            llm_cost_usd=0.01 if node == "load_external_data" else None,
            duration_ms=100 + i * 50,
        )
        await store.append(stream_id=stream_id, events=[e], expected_version=version)
        version += 1

    # Reconstruct context
    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    assert ctx.agent_id == agent_id
    assert ctx.session_id == session_id
    assert ctx.agent_type == "credit_analyzer"
    assert ctx.model_version == "v2.3"
    assert ctx.completed_nodes == ["validate_inputs", "open_aggregate_record", "load_external_data"]
    assert ctx.last_successful_node == "load_external_data"
    assert ctx.session_health_status == "HEALTHY"
    assert ctx.events_replayed == 5  # started + context + 3 nodes
    assert ctx.total_llm_calls == 1
    assert ctx.total_tokens_used == 700  # 500 + 200
    assert ctx.total_cost_usd == pytest.approx(0.01)
    assert len(ctx.pending_work) > 0  # write_output still pending
    assert "write_output" in ctx.pending_work

    print("\n--- Basic Agent Context Reconstruction Test PASSED ---")


@pytest.mark.asyncio
async def test_reconstruct_context_after_crash(store: EventStore):
    """
    Append 5 events (session started, context loaded, 3 node executions),
    then call reconstruct_agent_context WITHOUT any in-memory agent.
    Verify the reconstructed context is sufficient to continue.
    """
    agent_id = "agent-gt-crash"
    session_id = "sess-gt-crash"
    stream_id = f"agent-{agent_id}-{session_id}"

    # Build the event sequence
    events_to_append = [
        AgentSessionStarted.create(
            agent_id=agent_id,
            session_id=session_id,
            agent_type="fraud_screener",
            model_version="fraud-v1.0",
            context_source="event_store",
            context_token_count=3500,
        ),
        AgentContextLoaded.create(
            agent_id=agent_id,
            session_id=session_id,
            context_source="event_store",
            event_replay_from_position=0,
            context_token_count=3500,
            model_version="fraud-v1.0",
        ),
    ]

    # Append session started + context loaded as a batch
    await store.append(stream_id=stream_id, events=events_to_append, expected_version=-1)

    # Append 3 node executions one by one
    node_names = ["validate_inputs", "open_aggregate_record", "run_fraud_model"]
    version = 2
    for i, node in enumerate(node_names):
        e = AgentNodeExecuted.create(
            agent_id=agent_id,
            session_id=session_id,
            node_name=node,
            node_sequence=i + 1,
            input_keys=["fraud_data"],
            output_keys=["fraud_result"],
            llm_called=True,
            llm_tokens_input=400,
            llm_tokens_output=150,
            llm_cost_usd=0.005,
            duration_ms=200,
        )
        await store.append(stream_id=stream_id, events=[e], expected_version=version)
        version += 1

    # Simulate crash: no in-memory state, reconstruct purely from store
    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    # Verify reconstruction is complete
    assert ctx.session_id == session_id
    assert ctx.agent_type == "fraud_screener"
    assert ctx.model_version == "fraud-v1.0"
    assert ctx.completed_nodes == ["validate_inputs", "open_aggregate_record", "run_fraud_model"]
    assert ctx.last_successful_node == "run_fraud_model"
    assert ctx.session_health_status == "HEALTHY"
    assert ctx.events_replayed == 5  # 2 (started + context) + 3 nodes
    assert ctx.total_llm_calls == 3
    assert ctx.total_tokens_used == 3 * (400 + 150)  # 1650
    assert ctx.total_cost_usd == pytest.approx(3 * 0.005)

    # Context text should contain recent events
    assert "AgentNodeExecuted" in ctx.context_text
    assert ctx.last_event_position == 5

    print("\n--- Crash Recovery Context Reconstruction Test PASSED ---")


@pytest.mark.asyncio
async def test_needs_reconciliation(store: EventStore):
    """
    Create a session with output written but no completion event.
    Verify NEEDS_RECONCILIATION status.
    """
    agent_id = "agent-gt-recon"
    session_id = "sess-gt-recon"
    stream_id = f"agent-{agent_id}-{session_id}"

    e1 = AgentSessionStarted.create(
        agent_id=agent_id,
        session_id=session_id,
        agent_type="credit_analyzer",
        model_version="v2.3",
        context_source="event_store",
        context_token_count=4000,
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    e2 = AgentContextLoaded.create(
        agent_id=agent_id,
        session_id=session_id,
        context_source="event_store",
        event_replay_from_position=0,
        context_token_count=4000,
        model_version="v2.3",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    e3 = AgentNodeExecuted.create(
        agent_id=agent_id,
        session_id=session_id,
        node_name="validate_inputs",
        node_sequence=1,
        input_keys=["data"],
        output_keys=["validated"],
        duration_ms=100,
    )
    await store.append(stream_id=stream_id, events=[e3], expected_version=2)

    # Output written but NO AgentSessionCompleted — this is the crash scenario
    e4 = AgentOutputWritten.create(
        agent_id=agent_id,
        session_id=session_id,
        events_written=[{"event_type": "CreditAnalysisCompleted"}],
        output_summary="Credit analysis written to loan stream",
    )
    await store.append(stream_id=stream_id, events=[e4], expected_version=3)

    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    assert ctx.session_health_status == "NEEDS_RECONCILIATION", (
        f"Expected NEEDS_RECONCILIATION, got {ctx.session_health_status}. "
        "Output was written but session was never completed."
    )
    assert ctx.last_successful_node == "validate_inputs"
    assert ctx.events_replayed == 4

    print("\n--- Needs Reconciliation Test PASSED ---")


@pytest.mark.asyncio
async def test_empty_session(store: EventStore):
    """
    No events for a session — verify correct handling.
    """
    agent_id = "agent-gt-empty"
    session_id = "sess-gt-empty"

    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    assert ctx.session_id == session_id
    assert ctx.agent_id == agent_id
    assert ctx.agent_type is None
    assert ctx.model_version is None
    assert ctx.completed_nodes == []
    assert ctx.last_successful_node is None
    assert ctx.session_health_status == "NEEDS_RECONCILIATION"
    assert ctx.events_replayed == 0
    assert ctx.last_event_position == 0
    assert "start_session" in ctx.pending_work

    print("\n--- Empty Session Test PASSED ---")


@pytest.mark.asyncio
async def test_completed_session(store: EventStore):
    """
    Full session with completion event — verify COMPLETED status.
    """
    agent_id = "agent-gt-done"
    session_id = "sess-gt-done"
    stream_id = f"agent-{agent_id}-{session_id}"

    e1 = AgentSessionStarted.create(
        agent_id=agent_id,
        session_id=session_id,
        agent_type="credit_analyzer",
        model_version="v2.3",
        context_source="event_store",
        context_token_count=4000,
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    e2 = AgentContextLoaded.create(
        agent_id=agent_id,
        session_id=session_id,
        context_source="event_store",
        event_replay_from_position=0,
        context_token_count=4000,
        model_version="v2.3",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    e3 = AgentNodeExecuted.create(
        agent_id=agent_id,
        session_id=session_id,
        node_name="validate_inputs",
        node_sequence=1,
        input_keys=["data"],
        output_keys=["validated"],
        duration_ms=100,
    )
    await store.append(stream_id=stream_id, events=[e3], expected_version=2)

    e4 = AgentNodeExecuted.create(
        agent_id=agent_id,
        session_id=session_id,
        node_name="write_output",
        node_sequence=2,
        input_keys=["validated"],
        output_keys=["result"],
        duration_ms=150,
    )
    await store.append(stream_id=stream_id, events=[e4], expected_version=3)

    e5 = AgentSessionCompleted.create(
        agent_id=agent_id,
        session_id=session_id,
        total_nodes_executed=2,
        total_llm_calls=0,
        total_tokens_used=0,
        total_cost_usd=0.0,
    )
    await store.append(stream_id=stream_id, events=[e5], expected_version=4)

    ctx = await reconstruct_agent_context(store, agent_id, session_id)

    assert ctx.session_health_status == "COMPLETED"
    assert ctx.completed_nodes == ["validate_inputs", "write_output"]
    assert ctx.last_successful_node == "write_output"
    assert ctx.events_replayed == 5
    assert ctx.pending_work == []  # No pending work for completed sessions

    print("\n--- Completed Session Test PASSED ---")
