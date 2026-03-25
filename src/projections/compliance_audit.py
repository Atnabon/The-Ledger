"""
ComplianceAuditView projection — regulatory read model with temporal query support.

Maintains per-application compliance rule evaluations in `compliance_audit_view`
and periodic snapshots in `compliance_audit_snapshots` for point-in-time queries.
"""
import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from src.models.events import StoredEvent

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS compliance_audit_view (
    application_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT,
    status TEXT NOT NULL,  -- 'PASSED', 'FAILED', 'NOTED', 'PENDING'
    regulation_set_version TEXT,
    failure_reason TEXT,
    evidence_hash TEXT,
    note TEXT,
    evaluated_at TIMESTAMPTZ,
    PRIMARY KEY (application_id, rule_id)
);

CREATE TABLE IF NOT EXISTS compliance_audit_snapshots (
    application_id TEXT NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    state JSONB NOT NULL,
    global_position BIGINT NOT NULL,
    PRIMARY KEY (application_id, snapshot_at)
);
"""

# Take a snapshot every N events per application
SNAPSHOT_INTERVAL = 10


class ComplianceAuditViewProjection:
    name: str = "ComplianceAuditView"
    event_types: list[str] = [
        "ComplianceCheckRequested",
        "ComplianceCheckInitiated",
        "ComplianceRulePassed",
        "ComplianceRuleFailed",
        "ComplianceRuleNoted",
        "ComplianceCheckCompleted",
    ]

    def __init__(self) -> None:
        # Track event count per application for snapshot strategy
        self._event_counts: dict[str, int] = {}

    async def handle(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        """Route event to the appropriate handler."""
        handler = getattr(self, f"_handle_{event.event_type}", None)
        if handler is None:
            logger.warning(f"No handler for event type: {event.event_type}")
            return
        await handler(event, conn)

        # Snapshot strategy: periodically store full compliance state
        application_id = event.payload.get("application_id", event.stream_id)
        count = self._event_counts.get(application_id, 0) + 1
        self._event_counts[application_id] = count
        if count % SNAPSHOT_INTERVAL == 0:
            await self._take_snapshot(conn, application_id, event.global_position)

    async def rebuild(self, conn: asyncpg.Connection) -> None:
        """Truncate both tables so the daemon can replay from position 0."""
        await conn.execute("TRUNCATE TABLE compliance_audit_view")
        await conn.execute("TRUNCATE TABLE compliance_audit_snapshots")
        self._event_counts.clear()
        logger.info("ComplianceAuditView projection tables truncated for rebuild.")

    # ------------------------------------------------------------------
    # Per-event-type handlers
    # ------------------------------------------------------------------

    async def _handle_ComplianceCheckRequested(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        regulation_set_version = payload.get("regulation_set_version")
        checks_required = payload.get("checks_required", [])

        for rule_id in checks_required:
            await conn.execute(
                """INSERT INTO compliance_audit_view
                       (application_id, rule_id, status, regulation_set_version, evaluated_at)
                   VALUES ($1, $2, 'PENDING', $3, $4)
                   ON CONFLICT (application_id, rule_id) DO UPDATE
                       SET status = 'PENDING',
                           regulation_set_version = EXCLUDED.regulation_set_version,
                           evaluated_at = EXCLUDED.evaluated_at""",
                application_id, rule_id, regulation_set_version, event.recorded_at,
            )

    async def _handle_ComplianceCheckInitiated(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        regulation_set_version = payload.get("regulation_set_version")
        checks_required = payload.get("checks_required", [])

        for rule_id in checks_required:
            await conn.execute(
                """INSERT INTO compliance_audit_view
                       (application_id, rule_id, status, regulation_set_version, evaluated_at)
                   VALUES ($1, $2, 'PENDING', $3, $4)
                   ON CONFLICT (application_id, rule_id) DO UPDATE
                       SET status = 'PENDING',
                           regulation_set_version = COALESCE(EXCLUDED.regulation_set_version, compliance_audit_view.regulation_set_version),
                           evaluated_at = EXCLUDED.evaluated_at""",
                application_id, rule_id, regulation_set_version, event.recorded_at,
            )

    async def _handle_ComplianceRulePassed(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        rule_id = payload["rule_id"]
        rule_version = payload.get("rule_version")
        evidence_hash = payload.get("evidence_hash")

        await conn.execute(
            """INSERT INTO compliance_audit_view
                   (application_id, rule_id, rule_version, status, evidence_hash, evaluated_at)
               VALUES ($1, $2, $3, 'PASSED', $4, $5)
               ON CONFLICT (application_id, rule_id) DO UPDATE
                   SET status = 'PASSED',
                       rule_version = EXCLUDED.rule_version,
                       evidence_hash = EXCLUDED.evidence_hash,
                       failure_reason = NULL,
                       note = NULL,
                       evaluated_at = EXCLUDED.evaluated_at""",
            application_id, rule_id, rule_version, evidence_hash, event.recorded_at,
        )

    async def _handle_ComplianceRuleFailed(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        rule_id = payload["rule_id"]
        rule_version = payload.get("rule_version")
        failure_reason = payload.get("failure_reason")

        await conn.execute(
            """INSERT INTO compliance_audit_view
                   (application_id, rule_id, rule_version, status, failure_reason, evaluated_at)
               VALUES ($1, $2, $3, 'FAILED', $4, $5)
               ON CONFLICT (application_id, rule_id) DO UPDATE
                   SET status = 'FAILED',
                       rule_version = EXCLUDED.rule_version,
                       failure_reason = EXCLUDED.failure_reason,
                       note = NULL,
                       evaluated_at = EXCLUDED.evaluated_at""",
            application_id, rule_id, rule_version, failure_reason, event.recorded_at,
        )

    async def _handle_ComplianceRuleNoted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        rule_id = payload["rule_id"]
        rule_version = payload.get("rule_version")
        note = payload.get("note")

        await conn.execute(
            """INSERT INTO compliance_audit_view
                   (application_id, rule_id, rule_version, status, note, evaluated_at)
               VALUES ($1, $2, $3, 'NOTED', $4, $5)
               ON CONFLICT (application_id, rule_id) DO UPDATE
                   SET status = 'NOTED',
                       rule_version = EXCLUDED.rule_version,
                       note = EXCLUDED.note,
                       evaluated_at = EXCLUDED.evaluated_at""",
            application_id, rule_id, rule_version, note, event.recorded_at,
        )

    async def _handle_ComplianceCheckCompleted(
        self, event: StoredEvent, conn: asyncpg.Connection
    ) -> None:
        """
        ComplianceCheckCompleted is a summary event. We take a snapshot
        to mark the completion boundary for temporal queries.
        """
        payload = event.payload
        application_id = payload.get("application_id", event.stream_id)
        await self._take_snapshot(conn, application_id, event.global_position)

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _take_snapshot(
        conn: asyncpg.Connection,
        application_id: str,
        global_position: int,
    ) -> None:
        """Store a point-in-time snapshot of all compliance rules for an application."""
        rows = await conn.fetch(
            """SELECT rule_id, rule_version, status, regulation_set_version,
                      failure_reason, evidence_hash, note, evaluated_at
               FROM compliance_audit_view
               WHERE application_id = $1""",
            application_id,
        )
        state = [
            {
                "rule_id": r["rule_id"],
                "rule_version": r["rule_version"],
                "status": r["status"],
                "regulation_set_version": r["regulation_set_version"],
                "failure_reason": r["failure_reason"],
                "evidence_hash": r["evidence_hash"],
                "note": r["note"],
                "evaluated_at": r["evaluated_at"].isoformat() if r["evaluated_at"] else None,
            }
            for r in rows
        ]
        await conn.execute(
            """INSERT INTO compliance_audit_snapshots (application_id, snapshot_at, state, global_position)
               VALUES ($1, NOW(), $2::jsonb, $3)
               ON CONFLICT (application_id, snapshot_at) DO UPDATE
                   SET state = EXCLUDED.state, global_position = EXCLUDED.global_position""",
            application_id, json.dumps(state), global_position,
        )

    # ------------------------------------------------------------------
    # Temporal query methods
    # ------------------------------------------------------------------

    @staticmethod
    async def get_current_compliance(
        conn: asyncpg.Connection, application_id: str
    ) -> dict[str, Any]:
        """Return the current compliance state for an application."""
        rows = await conn.fetch(
            """SELECT rule_id, rule_version, status, regulation_set_version,
                      failure_reason, evidence_hash, note, evaluated_at
               FROM compliance_audit_view
               WHERE application_id = $1""",
            application_id,
        )
        rules = []
        for r in rows:
            rules.append({
                "rule_id": r["rule_id"],
                "rule_version": r["rule_version"],
                "status": r["status"],
                "regulation_set_version": r["regulation_set_version"],
                "failure_reason": r["failure_reason"],
                "evidence_hash": r["evidence_hash"],
                "note": r["note"],
                "evaluated_at": r["evaluated_at"].isoformat() if r["evaluated_at"] else None,
            })
        statuses = [r["status"] for r in rows]
        overall = "UNKNOWN"
        if statuses:
            if any(s == "FAILED" for s in statuses):
                overall = "FAILED"
            elif all(s == "PASSED" for s in statuses):
                overall = "PASSED"
            elif any(s == "PENDING" for s in statuses):
                overall = "PENDING"
            else:
                overall = "MIXED"
        return {
            "application_id": application_id,
            "overall_status": overall,
            "rules": rules,
            "rule_count": len(rules),
        }

    @staticmethod
    async def get_compliance_at(
        conn: asyncpg.Connection,
        application_id: str,
        timestamp: datetime,
    ) -> dict[str, Any]:
        """
        Return the compliance state as it was at a specific point in time.
        Uses the most recent snapshot at or before the given timestamp.
        """
        row = await conn.fetchrow(
            """SELECT state, snapshot_at, global_position
               FROM compliance_audit_snapshots
               WHERE application_id = $1 AND snapshot_at <= $2
               ORDER BY snapshot_at DESC
               LIMIT 1""",
            application_id, timestamp,
        )
        if not row:
            return {
                "application_id": application_id,
                "as_of": timestamp.isoformat(),
                "overall_status": "UNKNOWN",
                "rules": [],
                "rule_count": 0,
                "snapshot_at": None,
                "global_position": None,
            }
        state = row["state"] if isinstance(row["state"], list) else json.loads(row["state"])
        statuses = [r["status"] for r in state]
        overall = "UNKNOWN"
        if statuses:
            if any(s == "FAILED" for s in statuses):
                overall = "FAILED"
            elif all(s == "PASSED" for s in statuses):
                overall = "PASSED"
            elif any(s == "PENDING" for s in statuses):
                overall = "PENDING"
            else:
                overall = "MIXED"
        return {
            "application_id": application_id,
            "as_of": timestamp.isoformat(),
            "overall_status": overall,
            "rules": state,
            "rule_count": len(state),
            "snapshot_at": row["snapshot_at"].isoformat(),
            "global_position": row["global_position"],
        }

    def get_projection_lag(self) -> float:
        """
        Return the current projection lag (events behind).
        This is a convenience method; the actual lag is tracked by
        ProjectionDaemon.get_lag(self.name).
        Returns -1 if not available (daemon tracks this externally).
        """
        return -1.0
