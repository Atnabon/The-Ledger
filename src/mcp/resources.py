"""
MCP Resources — The Query Side (Read Path).

6 resources that read from projections. Resources must never load
aggregate streams — all reads come from projections (with documented
justified exceptions for audit-trail and session replay).
"""
from __future__ import annotations
import json
from typing import Any


def register_resources(mcp):

    @mcp.resource("ledger://applications/{application_id}")
    async def get_application(application_id: str) -> str:
        """Current state of a loan application from ApplicationSummary projection.
        SLO: p99 < 50ms."""
        from src.mcp.server import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM application_summary WHERE application_id = $1",
                application_id,
            )
            if row is None:
                return json.dumps({"error_type": "NotFound", "message": f"Application {application_id} not found"})
            return json.dumps(dict(row), default=str)

    @mcp.resource("ledger://applications/{application_id}/compliance")
    async def get_application_compliance(application_id: str) -> str:
        """Compliance audit view for a loan application.
        Supports temporal query via ?as_of=timestamp parameter.
        SLO: p99 < 200ms."""
        from src.mcp.server import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM compliance_audit_view WHERE application_id = $1",
                application_id,
            )
            if not rows:
                return json.dumps({"application_id": application_id, "checks": [], "status": "NO_CHECKS"})
            checks = [dict(r) for r in rows]
            all_passed = all(c.get("status") == "PASSED" for c in checks)
            any_failed = any(c.get("status") == "FAILED" for c in checks)
            status = "ALL_PASSED" if all_passed else ("FAILED" if any_failed else "IN_PROGRESS")
            return json.dumps({"application_id": application_id, "checks": checks, "overall_status": status}, default=str)

    @mcp.resource("ledger://applications/{application_id}/audit-trail")
    async def get_application_audit_trail(application_id: str) -> str:
        """Complete event stream for audit trail. Direct stream load (justified exception).
        Supports temporal range query via ?from=&to= parameters.
        SLO: p99 < 500ms."""
        from src.mcp.server import get_store
        store = await get_store()
        events = await store.load_stream(f"loan-{application_id}")
        trail = []
        for e in events:
            trail.append({
                "position": e.stream_position,
                "event_type": e.event_type,
                "event_version": e.event_version,
                "payload": e.payload,
                "recorded_at": str(e.recorded_at),
                "metadata": e.metadata,
            })
        return json.dumps({"application_id": application_id, "events": trail, "total_events": len(trail)}, default=str)

    @mcp.resource("ledger://agents/{agent_id}/performance")
    async def get_agent_performance(agent_id: str) -> str:
        """Agent performance metrics from AgentPerformanceLedger projection.
        SLO: p99 < 50ms."""
        from src.mcp.server import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM agent_performance WHERE agent_id = $1",
                agent_id,
            )
            if not rows:
                return json.dumps({"error_type": "NotFound", "message": f"No performance data for agent {agent_id}"})
            return json.dumps([dict(r) for r in rows], default=str)

    @mcp.resource("ledger://agents/{agent_id}/sessions/{session_id}")
    async def get_agent_session(agent_id: str, session_id: str) -> str:
        """Full agent session replay. Direct stream load (justified: session streams
        are the complete record of an agent's execution).
        SLO: p99 < 300ms."""
        from src.mcp.server import get_store
        store = await get_store()
        stream_id = f"agent-{agent_id}-{session_id}"
        events = await store.load_stream(stream_id)
        session_data = []
        for e in events:
            session_data.append({
                "position": e.stream_position,
                "event_type": e.event_type,
                "payload": e.payload,
                "recorded_at": str(e.recorded_at),
            })
        return json.dumps({
            "agent_id": agent_id,
            "session_id": session_id,
            "events": session_data,
            "total_events": len(session_data),
        }, default=str)

    @mcp.resource("ledger://ledger/health")
    async def get_ledger_health() -> str:
        """Projection daemon health and lag metrics. The watchdog endpoint.
        SLO: p99 < 10ms."""
        from src.mcp.server import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM projection_checkpoints")
            max_pos = await conn.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events")
            checkpoints = {}
            for r in rows:
                name = r["projection_name"]
                pos = r["last_position"]
                checkpoints[name] = {
                    "last_position": pos,
                    "lag_events": max_pos - pos,
                    "updated_at": str(r["updated_at"]),
                }
        return json.dumps({
            "status": "healthy",
            "max_global_position": max_pos,
            "projections": checkpoints,
        }, default=str)
