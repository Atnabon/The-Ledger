"""
Full Loan Lifecycle Integration Test

Drives the entire loan application lifecycle through command handlers
(simulating MCP tool calls), verifying that:
- Each command handler correctly appends events
- The event stream contains all expected events in order
- The application state machine transitions are correct
- Business rules are enforced throughout the lifecycle
"""

from __future__ import annotations

import os

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.models.events import ApplicationState
from src.commands.handlers import (
    SubmitApplicationCommand,
    CreditAnalysisCompletedCommand,
    FraudScreeningCompletedCommand,
    GenerateDecisionCommand,
    HumanReviewCompletedCommand,
    StartAgentSessionCommand,
    handle_submit_application,
    handle_credit_analysis_completed,
    handle_fraud_screening_completed,
    handle_generate_decision,
    handle_human_review_completed,
    handle_start_agent_session,
)
from src.aggregates.loan_application import LoanApplicationAggregate


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


@pytest.mark.asyncio
async def test_full_loan_lifecycle(store: EventStore):
    """
    Full lifecycle integration test driven through command handlers.

    Steps:
        1. Submit application
        2. Start agent session (Gas Town pattern)
        3. Record credit analysis completed
        4. Record fraud screening completed
        5. Generate decision (APPROVE)
        6. Record human review completed (no override)
        7. Verify complete event stream
        8. Verify application state machine transitions
    """
    app_id = "LIFECYCLE-001"
    agent_id = "agent-credit-001"
    session_id = "sess-credit-001"
    correlation_id = "corr-lifecycle-001"

    # -----------------------------------------------------------------------
    # Step 1: Submit application
    # -----------------------------------------------------------------------
    v1 = await handle_submit_application(
        SubmitApplicationCommand(
            application_id=app_id,
            applicant_id="applicant-lifecycle-001",
            requested_amount_usd=750_000.0,
            loan_purpose="commercial real estate",
            correlation_id=correlation_id,
        ),
        store,
    )
    assert v1 == 1

    # Verify state
    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.SUBMITTED
    assert app.requested_amount == 750_000.0

    # -----------------------------------------------------------------------
    # Step 2: Start agent session (Gas Town pattern)
    # -----------------------------------------------------------------------
    v_agent = await handle_start_agent_session(
        StartAgentSessionCommand(
            agent_id=agent_id,
            session_id=session_id,
            agent_type="credit_analyzer",
            model_version="v2.3",
            context_source="event_store",
            context_token_count=5000,
            correlation_id=correlation_id,
        ),
        store,
    )
    assert v_agent == 2  # AgentSessionStarted + AgentContextLoaded

    # We also need a CreditAnalysisRequested to move the state machine
    # to AWAITING_ANALYSIS before completing the credit analysis.
    from src.models.events import CreditAnalysisRequested
    req_event = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id=agent_id,
    )
    await store.append(
        stream_id=f"loan-{app_id}",
        events=[req_event],
        expected_version=1,
    )

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.AWAITING_ANALYSIS

    # -----------------------------------------------------------------------
    # Step 3: Record credit analysis completed
    # -----------------------------------------------------------------------
    v3 = await handle_credit_analysis_completed(
        CreditAnalysisCompletedCommand(
            application_id=app_id,
            agent_id=agent_id,
            session_id=session_id,
            model_version="v2.3",
            confidence_score=0.85,
            risk_tier="MEDIUM",
            recommended_limit_usd=600_000.0,
            duration_ms=2500,
            input_data={"credit_report": "hashed"},
            correlation_id=correlation_id,
            causation_id="cause-credit",
        ),
        store,
    )
    assert v3 == 3  # Third event on the loan stream

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.credit_analysis_completed is True
    assert app.risk_tier == "MEDIUM"

    # -----------------------------------------------------------------------
    # Step 4: Record fraud screening completed
    # -----------------------------------------------------------------------
    v4 = await handle_fraud_screening_completed(
        FraudScreeningCompletedCommand(
            application_id=app_id,
            agent_id="agent-fraud-001",
            fraud_score=0.12,
            anomaly_flags=[],
            screening_model_version="fraud-v1.0",
            input_data={"identity_check": "passed"},
            correlation_id=correlation_id,
            causation_id="cause-fraud",
        ),
        store,
    )
    assert v4 == 4

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.fraud_screening_completed is True
    assert app.fraud_score == 0.12
    assert app.state == ApplicationState.ANALYSIS_COMPLETE

    # -----------------------------------------------------------------------
    # Step 5: Generate decision (APPROVE)
    # -----------------------------------------------------------------------
    v5 = await handle_generate_decision(
        GenerateDecisionCommand(
            application_id=app_id,
            orchestrator_agent_id="orchestrator-001",
            recommendation="APPROVE",
            confidence_score=0.90,
            contributing_agent_sessions=[session_id],
            decision_basis_summary="Low risk, strong financials, clean fraud screen.",
            model_versions={"credit": "v2.3", "fraud": "fraud-v1.0"},
            correlation_id=correlation_id,
            causation_id="cause-decision",
        ),
        store,
    )
    assert v5 == 5

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.PENDING_DECISION
    assert app.decision == "APPROVE"

    # -----------------------------------------------------------------------
    # Step 6: Record human review completed (no override)
    # -----------------------------------------------------------------------
    v6 = await handle_human_review_completed(
        HumanReviewCompletedCommand(
            application_id=app_id,
            reviewer_id="reviewer-senior-001",
            override=False,
            final_decision="APPROVE",
            correlation_id=correlation_id,
        ),
        store,
    )
    assert v6 == 6

    app = await LoanApplicationAggregate.load(store, app_id)
    assert app.state == ApplicationState.APPROVED_PENDING_HUMAN
    assert app.human_reviewer_id == "reviewer-senior-001"
    assert app.has_human_review_override is False

    # -----------------------------------------------------------------------
    # Step 7: Verify the complete event stream
    # -----------------------------------------------------------------------
    loan_stream = await store.load_stream(f"loan-{app_id}")

    expected_event_types = [
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    ]

    actual_event_types = [e.event_type for e in loan_stream]
    assert actual_event_types == expected_event_types, (
        f"Event stream order mismatch.\n"
        f"Expected: {expected_event_types}\n"
        f"Actual:   {actual_event_types}"
    )

    # Verify stream positions are sequential
    for i, event in enumerate(loan_stream, start=1):
        assert event.stream_position == i, (
            f"Event at index {i-1} has stream_position={event.stream_position}, expected {i}"
        )

    # Verify global positions are monotonically increasing
    for i in range(1, len(loan_stream)):
        assert loan_stream[i].global_position > loan_stream[i - 1].global_position

    # -----------------------------------------------------------------------
    # Step 8: Verify agent session stream
    # -----------------------------------------------------------------------
    agent_stream = await store.load_stream(f"agent-{agent_id}-{session_id}")
    assert len(agent_stream) == 2
    assert agent_stream[0].event_type == "AgentSessionStarted"
    assert agent_stream[1].event_type == "AgentContextLoaded"

    # Verify correlation IDs in metadata
    for event in loan_stream:
        if event.metadata.get("correlation_id"):
            assert event.metadata["correlation_id"] == correlation_id

    # Verify final stream version
    final_version = await store.stream_version(f"loan-{app_id}")
    assert final_version == 6

    print("\n--- Full Loan Lifecycle Integration Test PASSED ---")
    print(f"  Events in loan stream: {len(loan_stream)}")
    print(f"  Events in agent stream: {len(agent_stream)}")
    print(f"  Final loan stream version: {final_version}")
    print(f"  Application state: {app.state}")
    print(f"  Event types: {actual_event_types}")
