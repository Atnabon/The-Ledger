"""
Event models for the Apex Ledger event store.

This file is the single source of truth for all event types.
Every agent, test, and projection imports from here.
Never redefine event classes elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# =============================================================================
# Custom Exceptions
# =============================================================================

class OptimisticConcurrencyError(Exception):
    """Raised when a stream's actual version does not match expected_version."""

    def __init__(
        self,
        stream_id: str,
        expected_version: int,
        actual_version: int,
    ):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict on stream '{stream_id}': "
            f"expected version {expected_version}, actual {actual_version}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": "OptimisticConcurrencyError",
            "message": str(self),
            "stream_id": self.stream_id,
            "expected_version": self.expected_version,
            "actual_version": self.actual_version,
            "suggested_action": "reload_stream_and_retry",
        }


class DomainError(Exception):
    """Raised when a business rule is violated in aggregate logic."""

    def __init__(self, message: str, rule: str | None = None):
        self.rule = rule
        super().__init__(message)


class StreamNotFoundError(Exception):
    """Raised when a stream does not exist."""

    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        super().__init__(f"Stream '{stream_id}' not found")


# =============================================================================
# Application State Machine
# =============================================================================

class ApplicationState(str, Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


# Valid state transitions
VALID_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.SUBMITTED: {ApplicationState.AWAITING_ANALYSIS},
    ApplicationState.AWAITING_ANALYSIS: {ApplicationState.ANALYSIS_COMPLETE},
    ApplicationState.ANALYSIS_COMPLETE: {ApplicationState.COMPLIANCE_REVIEW},
    ApplicationState.COMPLIANCE_REVIEW: {ApplicationState.PENDING_DECISION},
    ApplicationState.PENDING_DECISION: {
        ApplicationState.APPROVED_PENDING_HUMAN,
        ApplicationState.DECLINED_PENDING_HUMAN,
    },
    ApplicationState.APPROVED_PENDING_HUMAN: {ApplicationState.FINAL_APPROVED},
    ApplicationState.DECLINED_PENDING_HUMAN: {ApplicationState.FINAL_DECLINED},
    ApplicationState.FINAL_APPROVED: set(),
    ApplicationState.FINAL_DECLINED: set(),
}


# =============================================================================
# Base Event Models
# =============================================================================

class BaseEvent(BaseModel):
    """Base class for all domain events before they are stored."""

    event_type: str
    event_version: int = 1
    payload: dict[str, Any]

    def model_post_init(self, __context: Any) -> None:
        if "event_type" not in self.payload:
            self.payload["event_type"] = self.event_type


class StoredEvent(BaseModel):
    """An event as loaded from the event store (with position metadata)."""

    event_id: uuid.UUID
    stream_id: str
    stream_position: int
    global_position: int
    event_type: str
    event_version: int
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime

    def with_payload(self, new_payload: dict[str, Any], version: int) -> StoredEvent:
        """Return a copy with updated payload and version (used by upcasters)."""
        return self.model_copy(
            update={"payload": new_payload, "event_version": version}
        )


class StreamMetadata(BaseModel):
    """Metadata about an event stream."""

    stream_id: str
    aggregate_type: str
    current_version: int
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# LoanApplication Aggregate Events
# =============================================================================

class ApplicationSubmitted(BaseEvent):
    event_type: str = "ApplicationSubmitted"

    @classmethod
    def create(
        cls,
        application_id: str,
        applicant_id: str,
        requested_amount_usd: float,
        loan_purpose: str,
        submission_channel: str = "api",
    ) -> ApplicationSubmitted:
        return cls(
            payload={
                "application_id": application_id,
                "applicant_id": applicant_id,
                "requested_amount_usd": requested_amount_usd,
                "loan_purpose": loan_purpose,
                "submission_channel": submission_channel,
                "submitted_at": datetime.utcnow().isoformat(),
            }
        )


class CreditAnalysisRequested(BaseEvent):
    event_type: str = "CreditAnalysisRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        assigned_agent_id: str,
        priority: str = "normal",
    ) -> CreditAnalysisRequested:
        return cls(
            payload={
                "application_id": application_id,
                "assigned_agent_id": assigned_agent_id,
                "requested_at": datetime.utcnow().isoformat(),
                "priority": priority,
            }
        )


class CreditAnalysisCompleted(BaseEvent):
    event_type: str = "CreditAnalysisCompleted"
    event_version: int = 2

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        session_id: str,
        model_version: str,
        confidence_score: float,
        risk_tier: str,
        recommended_limit_usd: float,
        analysis_duration_ms: int,
        input_data_hash: str,
    ) -> CreditAnalysisCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": model_version,
                "confidence_score": confidence_score,
                "risk_tier": risk_tier,
                "recommended_limit_usd": recommended_limit_usd,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": input_data_hash,
            }
        )


class FraudScreeningCompleted(BaseEvent):
    event_type: str = "FraudScreeningCompleted"

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        fraud_score: float,
        anomaly_flags: list[str],
        screening_model_version: str,
        input_data_hash: str,
    ) -> FraudScreeningCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "fraud_score": fraud_score,
                "anomaly_flags": anomaly_flags,
                "screening_model_version": screening_model_version,
                "input_data_hash": input_data_hash,
            }
        )


class ComplianceCheckRequested(BaseEvent):
    event_type: str = "ComplianceCheckRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        regulation_set_version: str,
        checks_required: list[str],
    ) -> ComplianceCheckRequested:
        return cls(
            payload={
                "application_id": application_id,
                "regulation_set_version": regulation_set_version,
                "checks_required": checks_required,
            }
        )


class ComplianceRulePassed(BaseEvent):
    event_type: str = "ComplianceRulePassed"

    @classmethod
    def create(
        cls,
        application_id: str,
        rule_id: str,
        rule_version: str,
        evidence_hash: str,
    ) -> ComplianceRulePassed:
        return cls(
            payload={
                "application_id": application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "evaluation_timestamp": datetime.utcnow().isoformat(),
                "evidence_hash": evidence_hash,
            }
        )


class ComplianceRuleFailed(BaseEvent):
    event_type: str = "ComplianceRuleFailed"

    @classmethod
    def create(
        cls,
        application_id: str,
        rule_id: str,
        rule_version: str,
        failure_reason: str,
        remediation_required: bool = False,
    ) -> ComplianceRuleFailed:
        return cls(
            payload={
                "application_id": application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "failure_reason": failure_reason,
                "remediation_required": remediation_required,
            }
        )


class DecisionGenerated(BaseEvent):
    event_type: str = "DecisionGenerated"
    event_version: int = 2

    @classmethod
    def create(
        cls,
        application_id: str,
        orchestrator_agent_id: str,
        recommendation: str,
        confidence_score: float,
        contributing_agent_sessions: list[str],
        decision_basis_summary: str,
        model_versions: dict[str, str],
    ) -> DecisionGenerated:
        return cls(
            payload={
                "application_id": application_id,
                "orchestrator_agent_id": orchestrator_agent_id,
                "recommendation": recommendation,
                "confidence_score": confidence_score,
                "contributing_agent_sessions": contributing_agent_sessions,
                "decision_basis_summary": decision_basis_summary,
                "model_versions": model_versions,
            }
        )


class HumanReviewCompleted(BaseEvent):
    event_type: str = "HumanReviewCompleted"

    @classmethod
    def create(
        cls,
        application_id: str,
        reviewer_id: str,
        override: bool,
        final_decision: str,
        override_reason: str | None = None,
    ) -> HumanReviewCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "reviewer_id": reviewer_id,
                "override": override,
                "final_decision": final_decision,
                "override_reason": override_reason,
            }
        )


class ApplicationApproved(BaseEvent):
    event_type: str = "ApplicationApproved"

    @classmethod
    def create(
        cls,
        application_id: str,
        approved_amount_usd: float,
        interest_rate: float,
        conditions: list[str],
        approved_by: str,
        effective_date: str,
    ) -> ApplicationApproved:
        return cls(
            payload={
                "application_id": application_id,
                "approved_amount_usd": approved_amount_usd,
                "interest_rate": interest_rate,
                "conditions": conditions,
                "approved_by": approved_by,
                "effective_date": effective_date,
            }
        )


class ApplicationDeclined(BaseEvent):
    event_type: str = "ApplicationDeclined"

    @classmethod
    def create(
        cls,
        application_id: str,
        decline_reasons: list[str],
        declined_by: str,
        adverse_action_notice_required: bool = True,
    ) -> ApplicationDeclined:
        return cls(
            payload={
                "application_id": application_id,
                "decline_reasons": decline_reasons,
                "declined_by": declined_by,
                "adverse_action_notice_required": adverse_action_notice_required,
            }
        )


class HumanReviewRequested(BaseEvent):
    event_type: str = "HumanReviewRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        reason: str,
        assigned_to: str | None = None,
    ) -> HumanReviewRequested:
        return cls(
            payload={
                "application_id": application_id,
                "reason": reason,
                "assigned_to": assigned_to,
                "requested_at": datetime.utcnow().isoformat(),
            }
        )


# =============================================================================
# AgentSession Aggregate Events
# =============================================================================

class AgentSessionStarted(BaseEvent):
    event_type: str = "AgentSessionStarted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        agent_type: str,
        model_version: str,
        context_source: str,
        context_token_count: int,
    ) -> AgentSessionStarted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "agent_type": agent_type,
                "model_version": model_version,
                "context_source": context_source,
                "context_token_count": context_token_count,
                "started_at": datetime.utcnow().isoformat(),
            }
        )


class AgentContextLoaded(BaseEvent):
    event_type: str = "AgentContextLoaded"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        context_source: str,
        event_replay_from_position: int,
        context_token_count: int,
        model_version: str,
    ) -> AgentContextLoaded:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "context_source": context_source,
                "event_replay_from_position": event_replay_from_position,
                "context_token_count": context_token_count,
                "model_version": model_version,
            }
        )


class AgentSessionCompleted(BaseEvent):
    event_type: str = "AgentSessionCompleted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        total_nodes_executed: int,
        total_llm_calls: int,
        total_tokens_used: int,
        total_cost_usd: float,
    ) -> AgentSessionCompleted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "total_nodes_executed": total_nodes_executed,
                "total_llm_calls": total_llm_calls,
                "total_tokens_used": total_tokens_used,
                "total_cost_usd": total_cost_usd,
                "completed_at": datetime.utcnow().isoformat(),
            }
        )


class AgentSessionFailed(BaseEvent):
    event_type: str = "AgentSessionFailed"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        error_type: str,
        error_message: str,
        last_successful_node: str | None,
        recoverable: bool,
    ) -> AgentSessionFailed:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "error_type": error_type,
                "error_message": error_message,
                "last_successful_node": last_successful_node,
                "recoverable": recoverable,
            }
        )


class AgentNodeExecuted(BaseEvent):
    event_type: str = "AgentNodeExecuted"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        node_name: str,
        node_sequence: int,
        input_keys: list[str],
        output_keys: list[str],
        llm_called: bool = False,
        llm_tokens_input: int | None = None,
        llm_tokens_output: int | None = None,
        llm_cost_usd: float | None = None,
        duration_ms: int = 0,
    ) -> AgentNodeExecuted:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "node_name": node_name,
                "node_sequence": node_sequence,
                "input_keys": input_keys,
                "output_keys": output_keys,
                "llm_called": llm_called,
                "llm_tokens_input": llm_tokens_input,
                "llm_tokens_output": llm_tokens_output,
                "llm_cost_usd": llm_cost_usd,
                "duration_ms": duration_ms,
            }
        )


# =============================================================================
# AuditLedger Aggregate Events
# =============================================================================

class AuditIntegrityCheckRun(BaseEvent):
    event_type: str = "AuditIntegrityCheckRun"

    @classmethod
    def create(
        cls,
        entity_id: str,
        check_timestamp: str,
        events_verified_count: int,
        integrity_hash: str,
        previous_hash: str | None,
    ) -> AuditIntegrityCheckRun:
        return cls(
            payload={
                "entity_id": entity_id,
                "check_timestamp": check_timestamp,
                "events_verified_count": events_verified_count,
                "integrity_hash": integrity_hash,
                "previous_hash": previous_hash,
            }
        )


# =============================================================================
# AgentSession Aggregate Events (continued)
# =============================================================================

class AgentInputValidated(BaseEvent):
    event_type: str = "AgentInputValidated"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        inputs_validated: list[str],
        validation_duration_ms: int,
    ) -> AgentInputValidated:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "inputs_validated": inputs_validated,
                "validation_duration_ms": validation_duration_ms,
            }
        )


class AgentInputValidationFailed(BaseEvent):
    event_type: str = "AgentInputValidationFailed"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        missing_inputs: list[str],
        validation_errors: list[str],
    ) -> AgentInputValidationFailed:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "missing_inputs": missing_inputs,
                "validation_errors": validation_errors,
            }
        )


class AgentToolCalled(BaseEvent):
    event_type: str = "AgentToolCalled"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        tool_name: str,
        tool_input_summary: str,
        tool_output_summary: str,
        tool_duration_ms: int,
    ) -> AgentToolCalled:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input_summary": tool_input_summary,
                "tool_output_summary": tool_output_summary,
                "tool_duration_ms": tool_duration_ms,
            }
        )


class AgentOutputWritten(BaseEvent):
    event_type: str = "AgentOutputWritten"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        events_written: list[dict[str, Any]],
        output_summary: str,
    ) -> AgentOutputWritten:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "events_written": events_written,
                "output_summary": output_summary,
            }
        )


class AgentSessionRecovered(BaseEvent):
    event_type: str = "AgentSessionRecovered"

    @classmethod
    def create(
        cls,
        agent_id: str,
        session_id: str,
        recovered_from_session_id: str,
        recovery_point: str,
    ) -> AgentSessionRecovered:
        return cls(
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "recovered_from_session_id": recovered_from_session_id,
                "recovery_point": recovery_point,
            }
        )


# =============================================================================
# DocumentPackage Aggregate Events
# =============================================================================

class DocumentPackageCreated(BaseEvent):
    event_type: str = "DocumentPackageCreated"

    @classmethod
    def create(
        cls,
        package_id: str,
        application_id: str,
        created_at: str,
    ) -> DocumentPackageCreated:
        return cls(
            payload={
                "package_id": package_id,
                "application_id": application_id,
                "created_at": created_at,
            }
        )


class DocumentAdded(BaseEvent):
    event_type: str = "DocumentAdded"

    @classmethod
    def create(
        cls,
        package_id: str,
        document_type: str,
        file_path: str,
    ) -> DocumentAdded:
        return cls(
            payload={
                "package_id": package_id,
                "document_type": document_type,
                "file_path": file_path,
            }
        )


class DocumentFormatValidated(BaseEvent):
    event_type: str = "DocumentFormatValidated"

    @classmethod
    def create(
        cls,
        package_id: str,
        document_type: str,
        format_valid: bool,
        validation_notes: str,
    ) -> DocumentFormatValidated:
        return cls(
            payload={
                "package_id": package_id,
                "document_type": document_type,
                "format_valid": format_valid,
                "validation_notes": validation_notes,
            }
        )


class ExtractionStarted(BaseEvent):
    event_type: str = "ExtractionStarted"

    @classmethod
    def create(
        cls,
        package_id: str,
        document_type: str,
    ) -> ExtractionStarted:
        return cls(
            payload={
                "package_id": package_id,
                "document_type": document_type,
            }
        )


class ExtractionCompleted(BaseEvent):
    event_type: str = "ExtractionCompleted"

    @classmethod
    def create(
        cls,
        package_id: str,
        document_type: str,
        extracted_facts: dict[str, Any],
    ) -> ExtractionCompleted:
        return cls(
            payload={
                "package_id": package_id,
                "document_type": document_type,
                "extracted_facts": extracted_facts,
            }
        )


class QualityAssessmentCompleted(BaseEvent):
    event_type: str = "QualityAssessmentCompleted"

    @classmethod
    def create(
        cls,
        package_id: str,
        quality_score: float,
        issues: list[str],
    ) -> QualityAssessmentCompleted:
        return cls(
            payload={
                "package_id": package_id,
                "quality_score": quality_score,
                "issues": issues,
            }
        )


class PackageReadyForAnalysis(BaseEvent):
    event_type: str = "PackageReadyForAnalysis"

    @classmethod
    def create(
        cls,
        package_id: str,
        application_id: str,
    ) -> PackageReadyForAnalysis:
        return cls(
            payload={
                "package_id": package_id,
                "application_id": application_id,
            }
        )


# =============================================================================
# CreditRecord Aggregate Events
# =============================================================================

class CreditRecordOpened(BaseEvent):
    event_type: str = "CreditRecordOpened"

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        session_id: str,
    ) -> CreditRecordOpened:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "session_id": session_id,
            }
        )


class HistoricalProfileConsumed(BaseEvent):
    event_type: str = "HistoricalProfileConsumed"

    @classmethod
    def create(
        cls,
        application_id: str,
        profile_data_hash: str,
    ) -> HistoricalProfileConsumed:
        return cls(
            payload={
                "application_id": application_id,
                "profile_data_hash": profile_data_hash,
            }
        )


class ExtractedFactsConsumed(BaseEvent):
    event_type: str = "ExtractedFactsConsumed"

    @classmethod
    def create(
        cls,
        application_id: str,
        facts_data_hash: str,
    ) -> ExtractedFactsConsumed:
        return cls(
            payload={
                "application_id": application_id,
                "facts_data_hash": facts_data_hash,
            }
        )


class CreditAnalysisDeferred(BaseEvent):
    event_type: str = "CreditAnalysisDeferred"

    @classmethod
    def create(
        cls,
        application_id: str,
        reason: str,
    ) -> CreditAnalysisDeferred:
        return cls(
            payload={
                "application_id": application_id,
                "reason": reason,
            }
        )


# =============================================================================
# FraudScreening Aggregate Events
# =============================================================================

class FraudScreeningInitiated(BaseEvent):
    event_type: str = "FraudScreeningInitiated"

    @classmethod
    def create(
        cls,
        application_id: str,
        agent_id: str,
        session_id: str,
    ) -> FraudScreeningInitiated:
        return cls(
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "session_id": session_id,
            }
        )


class FraudAnomalyDetected(BaseEvent):
    event_type: str = "FraudAnomalyDetected"

    @classmethod
    def create(
        cls,
        application_id: str,
        anomaly_type: str,
        severity: str,
        details: str,
    ) -> FraudAnomalyDetected:
        return cls(
            payload={
                "application_id": application_id,
                "anomaly_type": anomaly_type,
                "severity": severity,
                "details": details,
            }
        )


# =============================================================================
# ComplianceRecord Aggregate Events
# =============================================================================

class ComplianceCheckInitiated(BaseEvent):
    event_type: str = "ComplianceCheckInitiated"

    @classmethod
    def create(
        cls,
        application_id: str,
        regulation_set_version: str,
        checks_required: list[str],
    ) -> ComplianceCheckInitiated:
        return cls(
            payload={
                "application_id": application_id,
                "regulation_set_version": regulation_set_version,
                "checks_required": checks_required,
            }
        )


class ComplianceCheckCompleted(BaseEvent):
    event_type: str = "ComplianceCheckCompleted"

    @classmethod
    def create(
        cls,
        application_id: str,
        all_passed: bool,
        total_checks: int,
        passed_count: int,
        failed_count: int,
    ) -> ComplianceCheckCompleted:
        return cls(
            payload={
                "application_id": application_id,
                "all_passed": all_passed,
                "total_checks": total_checks,
                "passed_count": passed_count,
                "failed_count": failed_count,
            }
        )


class ComplianceRuleNoted(BaseEvent):
    event_type: str = "ComplianceRuleNoted"

    @classmethod
    def create(
        cls,
        application_id: str,
        rule_id: str,
        rule_version: str,
        note: str,
    ) -> ComplianceRuleNoted:
        return cls(
            payload={
                "application_id": application_id,
                "rule_id": rule_id,
                "rule_version": rule_version,
                "note": note,
            }
        )


# =============================================================================
# LoanApplication Aggregate Events (continued)
# =============================================================================

class DocumentUploadRequested(BaseEvent):
    event_type: str = "DocumentUploadRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        required_documents: list[str],
    ) -> DocumentUploadRequested:
        return cls(
            payload={
                "application_id": application_id,
                "required_documents": required_documents,
            }
        )


class DocumentUploaded(BaseEvent):
    event_type: str = "DocumentUploaded"

    @classmethod
    def create(
        cls,
        application_id: str,
        document_type: str,
        file_path: str,
    ) -> DocumentUploaded:
        return cls(
            payload={
                "application_id": application_id,
                "document_type": document_type,
                "file_path": file_path,
            }
        )


class DecisionRequested(BaseEvent):
    event_type: str = "DecisionRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        requested_by: str,
    ) -> DecisionRequested:
        return cls(
            payload={
                "application_id": application_id,
                "requested_by": requested_by,
            }
        )


class FraudScreeningRequested(BaseEvent):
    event_type: str = "FraudScreeningRequested"

    @classmethod
    def create(
        cls,
        application_id: str,
        assigned_agent_id: str,
    ) -> FraudScreeningRequested:
        return cls(
            payload={
                "application_id": application_id,
                "assigned_agent_id": assigned_agent_id,
            }
        )


# =============================================================================
# Event Registry — maps event_type string to class
# =============================================================================

EVENT_REGISTRY: dict[str, type[BaseEvent]] = {
    # LoanApplication Aggregate
    "ApplicationSubmitted": ApplicationSubmitted,
    "CreditAnalysisRequested": CreditAnalysisRequested,
    "CreditAnalysisCompleted": CreditAnalysisCompleted,
    "FraudScreeningCompleted": FraudScreeningCompleted,
    "ComplianceCheckRequested": ComplianceCheckRequested,
    "ComplianceRulePassed": ComplianceRulePassed,
    "ComplianceRuleFailed": ComplianceRuleFailed,
    "DecisionGenerated": DecisionGenerated,
    "HumanReviewRequested": HumanReviewRequested,
    "HumanReviewCompleted": HumanReviewCompleted,
    "ApplicationApproved": ApplicationApproved,
    "ApplicationDeclined": ApplicationDeclined,
    "DocumentUploadRequested": DocumentUploadRequested,
    "DocumentUploaded": DocumentUploaded,
    "DecisionRequested": DecisionRequested,
    "FraudScreeningRequested": FraudScreeningRequested,
    # AgentSession Aggregate
    "AgentSessionStarted": AgentSessionStarted,
    "AgentContextLoaded": AgentContextLoaded,
    "AgentSessionCompleted": AgentSessionCompleted,
    "AgentSessionFailed": AgentSessionFailed,
    "AgentNodeExecuted": AgentNodeExecuted,
    "AgentInputValidated": AgentInputValidated,
    "AgentInputValidationFailed": AgentInputValidationFailed,
    "AgentToolCalled": AgentToolCalled,
    "AgentOutputWritten": AgentOutputWritten,
    "AgentSessionRecovered": AgentSessionRecovered,
    # DocumentPackage Aggregate
    "DocumentPackageCreated": DocumentPackageCreated,
    "DocumentAdded": DocumentAdded,
    "DocumentFormatValidated": DocumentFormatValidated,
    "ExtractionStarted": ExtractionStarted,
    "ExtractionCompleted": ExtractionCompleted,
    "QualityAssessmentCompleted": QualityAssessmentCompleted,
    "PackageReadyForAnalysis": PackageReadyForAnalysis,
    # CreditRecord Aggregate
    "CreditRecordOpened": CreditRecordOpened,
    "HistoricalProfileConsumed": HistoricalProfileConsumed,
    "ExtractedFactsConsumed": ExtractedFactsConsumed,
    "CreditAnalysisDeferred": CreditAnalysisDeferred,
    # FraudScreening Aggregate
    "FraudScreeningInitiated": FraudScreeningInitiated,
    "FraudAnomalyDetected": FraudAnomalyDetected,
    # ComplianceRecord Aggregate
    "ComplianceCheckInitiated": ComplianceCheckInitiated,
    "ComplianceCheckCompleted": ComplianceCheckCompleted,
    "ComplianceRuleNoted": ComplianceRuleNoted,
    # AuditLedger Aggregate
    "AuditIntegrityCheckRun": AuditIntegrityCheckRun,
}
