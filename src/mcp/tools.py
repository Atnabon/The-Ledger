"""
MCP Tools — The Command Side (Write Path).

8 tools that write events to the event store via command handlers.
Each tool has structured error types and precondition documentation
for LLM consumption.
"""
from __future__ import annotations
import json
from typing import Any

def register_tools(mcp):

    @mcp.tool(
        description="""Submit a new loan application. Creates a new loan stream.

        Preconditions: None — this is the entry point for a new application.
        Returns: {stream_id, initial_version} on success.
        Errors: {error_type: "DuplicateApplication"} if application_id already exists."""
    )
    async def submit_application(
        application_id: str,
        applicant_id: str,
        requested_amount_usd: float,
        loan_purpose: str,
        submission_channel: str = "api",
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import SubmitApplicationCommand, handle_submit_application
            cmd = SubmitApplicationCommand(
                application_id=application_id,
                applicant_id=applicant_id,
                requested_amount_usd=requested_amount_usd,
                loan_purpose=loan_purpose,
                submission_channel=submission_channel,
            )
            version = await handle_submit_application(cmd, store)
            return {"stream_id": f"loan-{application_id}", "initial_version": version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Record a completed credit analysis from an AI agent.

        Preconditions:
        - An active agent session must exist (created by start_agent_session) with context loaded.
        - The loan application must be in AWAITING_ANALYSIS state.
        - No prior CreditAnalysisCompleted for this application (unless HumanReviewOverride applied).

        Returns: {event_id, new_stream_version} on success.
        Errors: {error_type: "OptimisticConcurrencyError", suggested_action: "reload_stream_and_retry"}
                {error_type: "DomainError", rule: "model_version_locking"}"""
    )
    async def record_credit_analysis(
        application_id: str,
        agent_id: str,
        session_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        duration_ms: int,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import CreditAnalysisCompletedCommand, handle_credit_analysis_completed
            cmd = CreditAnalysisCompletedCommand(
                application_id=application_id,
                agent_id=agent_id,
                session_id=session_id,
                model_version=model_version,
                confidence_score=confidence_score,
                risk_tier=risk_tier,
                recommended_limit_usd=recommended_limit_usd,
                duration_ms=duration_ms,
                input_data=input_data,
            )
            version = await handle_credit_analysis_completed(cmd, store)
            return {"new_stream_version": version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Record a completed fraud screening from an AI agent.

        Preconditions:
        - Credit analysis must be completed for this application.
        - fraud_score must be between 0.0 and 1.0.

        Returns: {new_stream_version} on success.
        Errors: {error_type: "DomainError", rule: "analysis_ordering"}"""
    )
    async def record_fraud_screening(
        application_id: str,
        agent_id: str,
        fraud_score: float,
        anomaly_flags: list[str],
        screening_model_version: str,
        input_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            if not 0.0 <= fraud_score <= 1.0:
                return {"error_type": "ValidationError", "message": "fraud_score must be between 0.0 and 1.0"}
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import FraudScreeningCompletedCommand, handle_fraud_screening_completed
            cmd = FraudScreeningCompletedCommand(
                application_id=application_id,
                agent_id=agent_id,
                fraud_score=fraud_score,
                anomaly_flags=anomaly_flags,
                screening_model_version=screening_model_version,
                input_data=input_data,
            )
            version = await handle_fraud_screening_completed(cmd, store)
            return {"new_stream_version": version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Record compliance check results (pass or fail) for a specific rule.

        Preconditions:
        - A compliance check must have been requested for this application.
        - rule_id must be in the active regulation_set_version.

        Returns: {check_id, compliance_status} on success."""
    )
    async def record_compliance_check(
        application_id: str,
        rule_id: str,
        rule_version: str,
        passed: bool,
        failure_reason: str | None = None,
        evidence_hash: str | None = None,
        remediation_required: bool = False,
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            if passed:
                from src.models.events import ComplianceRulePassed
                event = ComplianceRulePassed.create(
                    application_id=application_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                    evidence_hash=evidence_hash or "",
                )
            else:
                from src.models.events import ComplianceRuleFailed
                event = ComplianceRuleFailed.create(
                    application_id=application_id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                    failure_reason=failure_reason or "No reason provided",
                    remediation_required=remediation_required,
                )

            stream_id = f"compliance-{application_id}"
            version = await store.stream_version(stream_id)
            if version == -1:
                # Auto-create the compliance stream
                from src.models.events import ComplianceCheckRequested
                init_event = ComplianceCheckRequested.create(
                    application_id=application_id,
                    regulation_set_version=rule_version,
                    checks_required=[rule_id],
                )
                await store.append(stream_id=stream_id, events=[init_event], expected_version=-1)
                version = 1

            new_version = await store.append(stream_id=stream_id, events=[event], expected_version=version)
            return {"check_id": rule_id, "compliance_status": "PASSED" if passed else "FAILED", "new_version": new_version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Generate a decision for a loan application.

        Preconditions:
        - All required analyses (credit, fraud) must be completed.
        - Confidence floor enforcement: score < 0.6 forces recommendation to REFER.

        Returns: {decision_id, recommendation} on success."""
    )
    async def generate_decision(
        application_id: str,
        orchestrator_agent_id: str,
        recommendation: str,
        confidence_score: float,
        contributing_agent_sessions: list[str],
        decision_basis_summary: str,
        model_versions: dict[str, str],
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import GenerateDecisionCommand, handle_generate_decision
            cmd = GenerateDecisionCommand(
                application_id=application_id,
                orchestrator_agent_id=orchestrator_agent_id,
                recommendation=recommendation,
                confidence_score=confidence_score,
                contributing_agent_sessions=contributing_agent_sessions,
                decision_basis_summary=decision_basis_summary,
                model_versions=model_versions,
            )
            version = await handle_generate_decision(cmd, store)
            # Confidence floor may have changed recommendation
            actual_rec = recommendation
            if confidence_score < 0.6 and recommendation == "APPROVE":
                actual_rec = "REFER"
            return {"decision_id": f"loan-{application_id}@{version}", "recommendation": actual_rec}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Record a human review decision on a loan application.

        Preconditions:
        - A decision must have been generated (application in PENDING_DECISION state).
        - If override=True, override_reason is required.
        - reviewer_id must be authenticated.

        Returns: {final_decision, application_state} on success."""
    )
    async def record_human_review(
        application_id: str,
        reviewer_id: str,
        override: bool,
        final_decision: str,
        override_reason: str | None = None,
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import HumanReviewCompletedCommand, handle_human_review_completed
            cmd = HumanReviewCompletedCommand(
                application_id=application_id,
                reviewer_id=reviewer_id,
                override=override,
                final_decision=final_decision,
                override_reason=override_reason,
            )
            version = await handle_human_review_completed(cmd, store)
            return {"final_decision": final_decision, "application_state": "PENDING_HUMAN_REVIEW", "new_version": version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Start a new agent session. REQUIRED before any agent decision tools.

        This is the Gas Town pattern: the session stream is the agent's memory.
        On crash recovery, a new agent instance replays its session stream to
        reconstruct context and resume from the last successful node.

        Calling any decision tool (record_credit_analysis, record_fraud_screening,
        generate_decision) without an active session will return a PreconditionFailed error.

        Returns: {session_id, context_position} on success."""
    )
    async def start_agent_session(
        agent_id: str,
        session_id: str,
        agent_type: str,
        model_version: str,
        context_source: str = "fresh",
        context_token_count: int = 0,
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.commands.handlers import StartAgentSessionCommand, handle_start_agent_session
            cmd = StartAgentSessionCommand(
                agent_id=agent_id,
                session_id=session_id,
                agent_type=agent_type,
                model_version=model_version,
                context_source=context_source,
                context_token_count=context_token_count,
            )
            version = await handle_start_agent_session(cmd, store)
            return {"session_id": session_id, "context_position": version}
        except Exception as e:
            return _error_response(e)

    @mcp.tool(
        description="""Run a cryptographic integrity check on an entity's event stream.

        Preconditions:
        - Rate-limited to 1 per minute per entity.
        - Verifies the SHA-256 hash chain over all events.

        Returns: {check_result, chain_valid, tamper_detected} on success."""
    )
    async def run_integrity_check(
        entity_type: str,
        entity_id: str,
    ) -> dict[str, Any]:
        try:
            from src.mcp.server import get_store
            store = await get_store()
            from src.integrity.audit_chain import run_integrity_check as _run_check
            result = await _run_check(store, entity_type, entity_id)
            return {
                "check_result": "PASS" if result.chain_valid else "FAIL",
                "chain_valid": result.chain_valid,
                "tamper_detected": result.tamper_detected,
                "events_verified": result.events_verified,
                "integrity_hash": result.integrity_hash[:16] + "...",
                "details": result.details,
            }
        except Exception as e:
            return _error_response(e)


def _error_response(e: Exception) -> dict[str, Any]:
    """Convert exceptions to structured error responses for LLM consumption."""
    from src.models.events import OptimisticConcurrencyError, DomainError, StreamNotFoundError

    if isinstance(e, OptimisticConcurrencyError):
        return e.to_dict()
    elif isinstance(e, DomainError):
        return {
            "error_type": "DomainError",
            "message": str(e),
            "rule": e.rule,
            "suggested_action": "check_preconditions_and_retry",
        }
    elif isinstance(e, StreamNotFoundError):
        return {
            "error_type": "StreamNotFoundError",
            "message": str(e),
            "stream_id": e.stream_id,
            "suggested_action": "verify_stream_exists",
        }
    else:
        return {
            "error_type": type(e).__name__,
            "message": str(e),
            "suggested_action": "investigate_error",
        }
