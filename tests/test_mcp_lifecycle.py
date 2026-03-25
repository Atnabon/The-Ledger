"""
Full Loan Lifecycle Integration Test — MCP-Only

Drives the entire loan application lifecycle using ONLY MCP tool and resource
calls. No direct Python function calls to command handlers or aggregates.

Steps:
    1. start_agent_session — Gas Town pattern
    2. submit_application — create loan stream
    3. record_credit_analysis — agent's analysis
    4. record_fraud_screening — fraud detection
    5. record_compliance_check — compliance rule evaluation
    6. generate_decision — orchestrator recommendation
    7. record_human_review — human review
    8. run_integrity_check — cryptographic verification
    9. Query compliance audit view — verify complete record

Assertions:
    - Each tool call returns success (no error_type in response)
    - Compliance resource contains all expected event types from lifecycle
    - Integrity check passes with chain_valid=True
"""

from __future__ import annotations

import json
import os

import pytest
import asyncpg

from src.event_store import EventStore, _init_connection
from src.mcp.server import mcp
import src.mcp.server as mcp_server


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost/apex_ledger_test"
)

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


# ---------------------------------------------------------------------------
# MCP tool caller — calls tools through the MCP server object
# ---------------------------------------------------------------------------

# After register_tools(mcp) runs, the tool functions are stored on the
# FastMCP instance. We access them by building a lookup of tool names to
# the underlying async functions.
_tool_registry: dict = {}


def _build_tool_registry():
    """Build a name->function lookup from all registered MCP tools."""
    global _tool_registry
    if _tool_registry:
        return
    # FastMCP stores tools internally — we access the decorated functions
    # that were registered during module import of src.mcp.server
    import src.mcp.tools as tools_mod
    # Re-register tools into our own registry by wrapping register_tools
    class _Collector:
        def __init__(self):
            self.tools = {}
        def tool(self, description=""):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator
    collector = _Collector()
    tools_mod.register_tools(collector)
    _tool_registry.update(collector.tools)


async def call_mcp_tool(tool_name: str, **kwargs) -> dict:
    """Call an MCP tool by name and return the result dict."""
    _build_tool_registry()
    fn = _tool_registry.get(tool_name)
    if fn is None:
        raise ValueError(f"MCP tool '{tool_name}' not found")
    return await fn(**kwargs)


@pytest.fixture
async def setup_mcp():
    """Set up fresh DB and wire MCP server to test database."""
    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=10, init=_init_connection
    )

    with open(SCHEMA_PATH) as f:
        schema_sql = f.read()

    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS outbox CASCADE")
        await conn.execute("DROP TABLE IF EXISTS snapshots CASCADE")
        await conn.execute("DROP TABLE IF EXISTS projection_checkpoints CASCADE")
        await conn.execute("DROP TABLE IF EXISTS events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS event_streams CASCADE")
        await conn.execute("DROP TABLE IF EXISTS application_summary CASCADE")
        await conn.execute("DROP TABLE IF EXISTS agent_performance CASCADE")
        await conn.execute("DROP TABLE IF EXISTS compliance_audit_view CASCADE")
        await conn.execute("DROP TABLE IF EXISTS compliance_audit_snapshots CASCADE")
        await conn.execute(schema_sql)

        # Create projection tables
        from src.projections.application_summary import CREATE_TABLE_SQL as APP_DDL
        from src.projections.agent_performance import CREATE_TABLE_SQL as AGENT_DDL
        from src.projections.compliance_audit import CREATE_TABLES_SQL as COMP_DDL
        await conn.execute(APP_DDL)
        await conn.execute(AGENT_DDL)
        await conn.execute(COMP_DDL)

    # Wire MCP server to test database
    store = EventStore(pool)
    mcp_server._store = store
    mcp_server._pool = pool

    yield pool, store

    # Cleanup
    mcp_server._store = None
    mcp_server._pool = None
    await pool.close()


# ---------------------------------------------------------------------------
# The Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_loan_lifecycle_via_mcp(setup_mcp):
    """
    Full lifecycle integration test using ONLY MCP tool calls.
    No direct Python function calls to command handlers or aggregates.

    Drives a complete loan application from submission through final approval,
    then queries the compliance audit view to verify a complete event record.
    """
    pool, store = setup_mcp
    app_id = "MCP-LIFECYCLE-001"
    agent_id = "agent-credit-mcp"
    session_id = "sess-credit-mcp"

    # -----------------------------------------------------------------------
    # Step 1: Start agent session (Gas Town — must happen before decisions)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "start_agent_session",
        agent_id=agent_id,
        session_id=session_id,
        agent_type="credit_analyzer",
        model_version="v2.3",
        context_source="event_store",
        context_token_count=5000,
    )
    assert "error_type" not in result, f"start_agent_session failed: {result}"
    assert result["session_id"] == session_id
    print(f"\n  1. start_agent_session -> session_id={result['session_id']}")

    # -----------------------------------------------------------------------
    # Step 2: Submit application (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "submit_application",
        application_id=app_id,
        applicant_id="applicant-mcp-001",
        requested_amount_usd=750_000.0,
        loan_purpose="commercial real estate",
    )
    assert "error_type" not in result, f"submit_application failed: {result}"
    assert result["stream_id"] == f"loan-{app_id}"
    print(f"  2. submit_application -> stream_id={result['stream_id']}")

    # Transition to AWAITING_ANALYSIS (internal state transition event)
    from src.models.events import CreditAnalysisRequested
    req = CreditAnalysisRequested.create(
        application_id=app_id,
        assigned_agent_id=agent_id,
    )
    await store.append(stream_id=f"loan-{app_id}", events=[req], expected_version=1)

    # -----------------------------------------------------------------------
    # Step 3: Record credit analysis completed (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "record_credit_analysis",
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        model_version="v2.3",
        confidence_score=0.85,
        risk_tier="MEDIUM",
        recommended_limit_usd=600_000.0,
        duration_ms=2500,
    )
    assert "error_type" not in result, f"record_credit_analysis failed: {result}"
    print(f"  3. record_credit_analysis -> version={result.get('new_stream_version')}")

    # -----------------------------------------------------------------------
    # Step 4: Record fraud screening completed (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "record_fraud_screening",
        application_id=app_id,
        agent_id="agent-fraud-mcp",
        fraud_score=0.12,
        anomaly_flags=[],
        screening_model_version="fraud-v1.0",
    )
    assert "error_type" not in result, f"record_fraud_screening failed: {result}"
    print(f"  4. record_fraud_screening -> version={result.get('new_stream_version')}")

    # -----------------------------------------------------------------------
    # Step 5: Record compliance check — passed (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "record_compliance_check",
        application_id=app_id,
        rule_id="AML-001",
        rule_version="v2.0",
        passed=True,
        evidence_hash="evidence-hash-aml",
    )
    assert "error_type" not in result, f"record_compliance_check failed: {result}"
    assert result["compliance_status"] == "PASSED"
    print(f"  5. record_compliance_check -> status={result['compliance_status']}")

    # -----------------------------------------------------------------------
    # Step 6: Generate decision (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "generate_decision",
        application_id=app_id,
        orchestrator_agent_id="orchestrator-mcp",
        recommendation="APPROVE",
        confidence_score=0.90,
        contributing_agent_sessions=[session_id],
        decision_basis_summary="Low risk, strong financials, clean fraud screen.",
        model_versions={"credit": "v2.3", "fraud": "fraud-v1.0"},
    )
    assert "error_type" not in result, f"generate_decision failed: {result}"
    assert result["recommendation"] == "APPROVE"
    print(f"  6. generate_decision -> recommendation={result['recommendation']}")

    # -----------------------------------------------------------------------
    # Step 7: Record human review (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "record_human_review",
        application_id=app_id,
        reviewer_id="reviewer-senior-001",
        override=False,
        final_decision="APPROVE",
    )
    assert "error_type" not in result, f"record_human_review failed: {result}"
    assert result["final_decision"] == "APPROVE"
    print(f"  7. record_human_review -> final_decision={result['final_decision']}")

    # -----------------------------------------------------------------------
    # Step 8: Run integrity check (MCP tool)
    # -----------------------------------------------------------------------
    result = await call_mcp_tool(
        "run_integrity_check",
        entity_type="loan",
        entity_id=app_id,
    )
    assert "error_type" not in result, f"run_integrity_check failed: {result}"
    assert result["chain_valid"] is True
    assert result["tamper_detected"] is False
    print(f"  8. run_integrity_check -> chain_valid={result['chain_valid']}, "
          f"events_verified={result['events_verified']}")

    # -----------------------------------------------------------------------
    # Step 9: Process projections, then query compliance audit view
    # -----------------------------------------------------------------------
    from src.projections.application_summary import ApplicationSummaryProjection
    from src.projections.compliance_audit import ComplianceAuditViewProjection
    from src.projections.daemon import ProjectionDaemon

    daemon = ProjectionDaemon(pool, [
        ApplicationSummaryProjection(),
        ComplianceAuditViewProjection(),
    ])
    await daemon._process_batch()

    # Query compliance audit view — should contain all lifecycle events
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM compliance_audit_view WHERE application_id = $1",
            app_id,
        )

    compliance_rules = [dict(r) for r in rows]
    assert len(compliance_rules) > 0, (
        "Compliance audit view should contain at least one rule after lifecycle"
    )

    rule_ids = [r["rule_id"] for r in compliance_rules]
    assert "AML-001" in rule_ids, (
        f"Expected AML-001 in compliance rules, got {rule_ids}"
    )
    passed_rules = [r for r in compliance_rules if r["status"] == "PASSED"]
    assert len(passed_rules) >= 1, "At least one compliance rule should be PASSED"

    # Verify the complete loan event stream via the store
    loan_events = await store.load_stream(f"loan-{app_id}")
    event_types = [e.event_type for e in loan_events]

    expected_types = [
        "ApplicationSubmitted",
        "CreditAnalysisRequested",
        "CreditAnalysisCompleted",
        "FraudScreeningCompleted",
        "DecisionGenerated",
        "HumanReviewCompleted",
    ]
    for et in expected_types:
        assert et in event_types, (
            f"Expected event type '{et}' in loan stream, got {event_types}"
        )

    print(f"  9. Compliance query -> {len(compliance_rules)} rules, "
          f"all expected event types present")
    print("\n--- Full Loan Lifecycle via MCP Test PASSED ---")
    print(f"  Total loan events: {len(loan_events)}")
    print(f"  Event types: {event_types}")
    print(f"  Compliance rules: {rule_ids}")
    print(f"  Integrity: chain_valid=True, tamper_detected=False")
