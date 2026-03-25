"""
Projection and Daemon Tests

Verifies that:
- ApplicationSummary projection correctly processes loan lifecycle events
- AgentPerformanceLedger projection correctly tracks agent metrics
- Projection checkpoints are updated correctly after each batch
- The daemon is fault-tolerant: continues after a projection handler error
- Projection rebuild (truncate + reset checkpoint) works correctly
"""

from __future__ import annotations

import os

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.models.events import (
    ApplicationSubmitted,
    CreditAnalysisRequested,
    CreditAnalysisCompleted,
    FraudScreeningCompleted,
    DecisionGenerated,
    HumanReviewCompleted,
    AgentSessionStarted,
    AgentSessionCompleted,
    StoredEvent,
)
from src.projections.daemon import ProjectionDaemon
from src.projections.application_summary import (
    ApplicationSummaryProjection,
    CREATE_TABLE_SQL as APP_SUMMARY_DDL,
)
from src.projections.agent_performance import (
    AgentPerformanceLedgerProjection,
    CREATE_TABLE_SQL as AGENT_PERF_DDL,
)
from src.projections.compliance_audit import (
    ComplianceAuditViewProjection,
    CREATE_TABLES_SQL as COMPLIANCE_DDL,
)


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
        # Drop all tables for a clean slate
        await conn.execute("DROP TABLE IF EXISTS outbox CASCADE")
        await conn.execute("DROP TABLE IF EXISTS snapshots CASCADE")
        await conn.execute("DROP TABLE IF EXISTS projection_checkpoints CASCADE")
        await conn.execute("DROP TABLE IF EXISTS events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS event_streams CASCADE")
        # Drop projection tables
        await conn.execute("DROP TABLE IF EXISTS application_summary CASCADE")
        await conn.execute("DROP TABLE IF EXISTS agent_performance CASCADE")
        await conn.execute("DROP TABLE IF EXISTS compliance_audit_view CASCADE")
        await conn.execute("DROP TABLE IF EXISTS compliance_audit_snapshots CASCADE")
        # Apply core schema
        await conn.execute(schema_sql)
        # Create projection tables
        await conn.execute(APP_SUMMARY_DDL)
        await conn.execute(AGENT_PERF_DDL)
        await conn.execute(COMPLIANCE_DDL)

    yield p
    await p.close()


@pytest.fixture
def store(pool) -> EventStore:
    return EventStore(pool)


# ---------------------------------------------------------------------------
# Helper: seed a loan application stream with events
# ---------------------------------------------------------------------------

async def _seed_loan_events(store: EventStore, app_id: str) -> int:
    """Seed a full loan application lifecycle and return final version."""
    stream_id = f"loan-{app_id}"

    e1 = ApplicationSubmitted.create(
        application_id=app_id,
        applicant_id="applicant-001",
        requested_amount_usd=500_000.0,
        loan_purpose="expansion",
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    e2 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-credit-001",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    e3 = CreditAnalysisCompleted.create(
        application_id=app_id,
        agent_id="agent-credit-001",
        session_id="session-001",
        model_version="v2.3",
        confidence_score=0.85,
        risk_tier="MEDIUM",
        recommended_limit_usd=400_000.0,
        analysis_duration_ms=1500,
        input_data_hash="hash-001",
    )
    await store.append(stream_id=stream_id, events=[e3], expected_version=2)

    e4 = FraudScreeningCompleted.create(
        application_id=app_id,
        agent_id="agent-fraud-001",
        fraud_score=0.15,
        anomaly_flags=[],
        screening_model_version="fraud-v1.0",
        input_data_hash="hash-002",
    )
    await store.append(stream_id=stream_id, events=[e4], expected_version=3)

    e5 = DecisionGenerated.create(
        application_id=app_id,
        orchestrator_agent_id="orchestrator-001",
        recommendation="APPROVE",
        confidence_score=0.90,
        contributing_agent_sessions=["session-001"],
        decision_basis_summary="Low risk, strong financials.",
        model_versions={"credit": "v2.3", "fraud": "fraud-v1.0"},
    )
    await store.append(stream_id=stream_id, events=[e5], expected_version=4)

    e6 = HumanReviewCompleted.create(
        application_id=app_id,
        reviewer_id="reviewer-001",
        override=False,
        final_decision="APPROVE",
    )
    version = await store.append(stream_id=stream_id, events=[e6], expected_version=5)
    return version


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_application_summary_projection(pool, store: EventStore):
    """
    Seed loan events, run the daemon's process_batch, and verify
    the application_summary table has the correct data.
    """
    app_id = "PROJ-APP-001"
    await _seed_loan_events(store, app_id)

    # Set up daemon with ApplicationSummary projection
    projection = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(pool, [projection])

    # Process all events
    processed = await daemon._process_batch()
    assert processed > 0

    # Verify the application_summary table
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1",
            app_id,
        )

    assert row is not None, "application_summary row not found"
    assert row["applicant_id"] == "applicant-001"
    assert float(row["requested_amount_usd"]) == 500_000.0
    assert row["risk_tier"] == "MEDIUM"
    assert float(row["fraud_score"]) == 0.15
    assert row["decision"] == "APPROVE"
    assert row["human_reviewer_id"] == "reviewer-001"
    assert row["last_event_type"] == "HumanReviewCompleted"

    print("\n--- Application Summary Projection Test PASSED ---")


@pytest.mark.asyncio
async def test_agent_performance_projection(pool, store: EventStore):
    """
    Create agent events, process through daemon, verify agent_performance table.
    """
    agent_id = "agent-perf-001"
    session_id = "sess-perf-001"
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

    e2 = AgentSessionCompleted.create(
        agent_id=agent_id,
        session_id=session_id,
        total_nodes_executed=5,
        total_llm_calls=3,
        total_tokens_used=12000,
        total_cost_usd=0.15,
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    # Also seed a CreditAnalysisCompleted on a loan stream to test that handler
    loan_stream = "loan-PERF-LOAN-001"
    e_sub = ApplicationSubmitted.create(
        application_id="PERF-LOAN-001",
        applicant_id="applicant-perf",
        requested_amount_usd=100_000.0,
        loan_purpose="test",
    )
    await store.append(stream_id=loan_stream, events=[e_sub], expected_version=-1)

    e_credit = CreditAnalysisCompleted.create(
        application_id="PERF-LOAN-001",
        agent_id=agent_id,
        session_id=session_id,
        model_version="v2.3",
        confidence_score=0.88,
        risk_tier="LOW",
        recommended_limit_usd=90_000.0,
        analysis_duration_ms=1200,
        input_data_hash="perf-hash",
    )
    await store.append(stream_id=loan_stream, events=[e_credit], expected_version=1)

    # Set up daemon with AgentPerformanceLedger projection
    projection = AgentPerformanceLedgerProjection()
    daemon = ProjectionDaemon(pool, [projection])

    processed = await daemon._process_batch()
    assert processed > 0

    # Verify the agent_performance table
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM agent_performance WHERE agent_id = $1 AND model_version = $2",
            agent_id, "v2.3",
        )

    assert row is not None, "agent_performance row not found"
    assert row["sessions_started"] == 1
    assert row["sessions_completed"] == 1
    assert row["analyses_completed"] == 1
    assert float(row["total_confidence_score"]) == 0.88
    assert row["confidence_score_count"] == 1
    assert row["total_duration_ms"] == 1200
    assert row["first_seen_at"] is not None

    print("\n--- Agent Performance Projection Test PASSED ---")


@pytest.mark.asyncio
async def test_projection_checkpoint_management(pool, store: EventStore):
    """
    Verify that projection checkpoints are updated correctly after processing.
    """
    app_id = "CHKPT-001"
    stream_id = f"loan-{app_id}"

    e1 = ApplicationSubmitted.create(
        application_id=app_id,
        applicant_id="applicant-chk",
        requested_amount_usd=200_000.0,
        loan_purpose="checkpoint test",
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    projection = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(pool, [projection])

    # Before processing, checkpoint should not exist
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            "ApplicationSummary",
        )
    assert row is None, "Checkpoint should not exist before first processing"

    # Process batch
    await daemon._process_batch()

    # After processing, checkpoint should be set to the max global_position
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            "ApplicationSummary",
        )
    assert row is not None, "Checkpoint should exist after processing"
    assert row["last_position"] > 0

    first_checkpoint = row["last_position"]

    # Append more events and process again
    e2 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-chk-001",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    await daemon._process_batch()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            "ApplicationSummary",
        )
    assert row["last_position"] > first_checkpoint, (
        "Checkpoint should advance after processing new events"
    )

    print("\n--- Projection Checkpoint Management Test PASSED ---")


@pytest.mark.asyncio
async def test_daemon_fault_tolerance(pool, store: EventStore):
    """
    Verify the daemon continues after a projection handler error.
    A failing projection should not block other events from being processed.
    """
    app_id = "FAULT-001"
    stream_id = f"loan-{app_id}"

    e1 = ApplicationSubmitted.create(
        application_id=app_id,
        applicant_id="applicant-fault",
        requested_amount_usd=300_000.0,
        loan_purpose="fault tolerance test",
    )
    await store.append(stream_id=stream_id, events=[e1], expected_version=-1)

    e2 = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id="agent-fault-001",
    )
    await store.append(stream_id=stream_id, events=[e2], expected_version=1)

    # Create a projection that fails on the first event
    class FailingProjection:
        name: str = "FailingProjection"
        event_types: list[str] = ["ApplicationSubmitted", "CreditAnalysisRequested"]
        call_count: int = 0

        async def handle(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("Simulated projection failure")
            # Second call succeeds

        async def rebuild(self, conn: asyncpg.Connection) -> None:
            pass

    failing = FailingProjection()
    daemon = ProjectionDaemon(pool, [failing], max_retries=0)

    # Process batch — should not raise even though the first event fails
    processed = await daemon._process_batch()

    # The daemon should have recorded the error but continued
    assert "FailingProjection" in daemon._errors
    assert len(daemon._errors["FailingProjection"]) >= 1

    # Checkpoint should still advance past the failed event
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            "FailingProjection",
        )
    assert row is not None, "Checkpoint should exist even after failure"
    assert row["last_position"] > 0

    print("\n--- Daemon Fault Tolerance Test PASSED ---")


@pytest.mark.asyncio
async def test_projection_rebuild(pool, store: EventStore):
    """
    Test rebuild_from_scratch: truncate projection table and reset checkpoint,
    then re-process events to rebuild the projection state.
    """
    app_id = "REBUILD-001"
    await _seed_loan_events(store, app_id)

    projection = ApplicationSummaryProjection()
    daemon = ProjectionDaemon(pool, [projection])

    # First pass — populate the projection
    await daemon._process_batch()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is not None, "Projection should be populated after first pass"

    # Rebuild: truncate + reset checkpoint
    await daemon.rebuild_projection("ApplicationSummary")

    # Verify truncation
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is None, "Projection table should be empty after rebuild"

    # Verify checkpoint reset to 0
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            "ApplicationSummary",
        )
    assert row is not None
    assert row["last_position"] == 0

    # Re-process all events — projection should be fully rebuilt
    await daemon._process_batch()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM application_summary WHERE application_id = $1", app_id
        )
    assert row is not None, "Projection should be rebuilt after re-processing"
    assert row["applicant_id"] == "applicant-001"
    assert row["risk_tier"] == "MEDIUM"
    assert row["decision"] == "APPROVE"
    assert row["human_reviewer_id"] == "reviewer-001"

    print("\n--- Projection Rebuild Test PASSED ---")
