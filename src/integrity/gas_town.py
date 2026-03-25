"""
Gas Town Agent Memory Pattern — crash recovery via event replay.

An AI agent that crashes mid-session can restart and reconstruct its
exact context from the event store, then continue where it left off
without repeating completed work.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from src.event_store import EventStore
from src.models.events import StoredEvent


@dataclass
class AgentContext:
    """Reconstructed agent context from event stream replay."""
    session_id: str
    agent_id: str
    agent_type: str | None
    model_version: str | None
    context_text: str
    last_event_position: int
    last_successful_node: str | None
    completed_nodes: list[str]
    pending_work: list[str]
    session_health_status: str  # "HEALTHY", "NEEDS_RECONCILIATION", "COMPLETED", "FAILED"
    events_replayed: int
    total_llm_calls: int = 0
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    raw_events: list[StoredEvent] = field(default_factory=list)


# Standard node sequence all agents follow
STANDARD_NODE_SEQUENCE = [
    "validate_inputs",
    "open_aggregate_record",
    "load_external_data",
    # domain nodes vary per agent
    "write_output",
]


async def reconstruct_agent_context(
    store: EventStore,
    agent_id: str,
    session_id: str,
    token_budget: int = 8000,
) -> AgentContext:
    """
    Reconstruct an agent's context from its event stream for crash recovery.

    - Load full AgentSession stream
    - Identify: last completed action, pending work, current state
    - Summarise old events into prose (token-efficient)
    - Preserve verbatim: last 3 events, any PENDING or ERROR state events
    - Detect NEEDS_RECONCILIATION if last event was partial
    """
    stream_id = f"agent-{agent_id}-{session_id}"
    events = await store.load_stream(stream_id)

    if not events:
        return AgentContext(
            session_id=session_id,
            agent_id=agent_id,
            agent_type=None,
            model_version=None,
            context_text="No events found for this session.",
            last_event_position=0,
            last_successful_node=None,
            completed_nodes=[],
            pending_work=["start_session"],
            session_health_status="NEEDS_RECONCILIATION",
            events_replayed=0,
        )

    # Extract state from events
    agent_type = None
    model_version = None
    completed_nodes: list[str] = []
    last_successful_node: str | None = None
    session_started = False
    context_loaded = False
    session_completed = False
    session_failed = False
    total_llm_calls = 0
    total_tokens = 0
    total_cost = 0.0
    error_events: list[StoredEvent] = []

    for event in events:
        et = event.event_type
        p = event.payload

        if et == "AgentSessionStarted":
            session_started = True
            agent_type = p.get("agent_type")
            model_version = p.get("model_version")
        elif et == "AgentContextLoaded":
            context_loaded = True
        elif et == "AgentNodeExecuted":
            node_name = p.get("node_name", "unknown")
            completed_nodes.append(node_name)
            last_successful_node = node_name
            if p.get("llm_called"):
                total_llm_calls += 1
                total_tokens += (p.get("llm_tokens_input") or 0) + (p.get("llm_tokens_output") or 0)
                total_cost += p.get("llm_cost_usd") or 0.0
        elif et == "AgentSessionCompleted":
            session_completed = True
        elif et == "AgentSessionFailed":
            session_failed = True
            error_events.append(event)
        elif et == "AgentInputValidationFailed":
            error_events.append(event)

    # Determine health status
    if session_completed:
        health = "COMPLETED"
    elif session_failed:
        health = "FAILED"
    elif not session_started:
        health = "NEEDS_RECONCILIATION"
    else:
        # Check if the last event suggests incomplete work
        last_event = events[-1]
        if last_event.event_type in ("AgentNodeExecuted", "AgentToolCalled", "AgentOutputWritten"):
            # Was mid-work — check if there's a completion event
            if last_event.event_type == "AgentOutputWritten":
                health = "NEEDS_RECONCILIATION"  # Output written but session not completed
            else:
                health = "HEALTHY"  # Can resume from last node
        elif last_event.event_type == "AgentContextLoaded":
            health = "HEALTHY"  # Ready to start work
        else:
            health = "HEALTHY"

    # Determine pending work
    pending: list[str] = []
    if not session_completed and not session_failed:
        if not session_started:
            pending.append("start_session")
        if not context_loaded:
            pending.append("load_context")
        # Check which standard nodes haven't been completed
        for node in STANDARD_NODE_SEQUENCE:
            if node not in completed_nodes:
                pending.append(node)

    # Build context text (token-efficient summary)
    context_parts: list[str] = []

    # Summary of older events
    if len(events) > 3:
        old_events = events[:-3]
        summary = _summarize_events(old_events, token_budget // 2)
        context_parts.append(f"Session Summary ({len(old_events)} prior events):\n{summary}")

    # Verbatim last 3 events
    recent = events[-3:] if len(events) >= 3 else events
    context_parts.append("\nRecent Events (verbatim):")
    for e in recent:
        context_parts.append(f"  [{e.stream_position}] {e.event_type}: {_truncate_payload(e.payload)}")

    # Error events (always preserved verbatim)
    if error_events:
        context_parts.append("\nError Events:")
        for e in error_events:
            context_parts.append(f"  [{e.stream_position}] {e.event_type}: {e.payload}")

    context_text = "\n".join(context_parts)

    return AgentContext(
        session_id=session_id,
        agent_id=agent_id,
        agent_type=agent_type,
        model_version=model_version,
        context_text=context_text,
        last_event_position=events[-1].stream_position,
        last_successful_node=last_successful_node,
        completed_nodes=completed_nodes,
        pending_work=pending,
        session_health_status=health,
        events_replayed=len(events),
        total_llm_calls=total_llm_calls,
        total_tokens_used=total_tokens,
        total_cost_usd=total_cost,
        raw_events=events,
    )


def _summarize_events(events: list[StoredEvent], max_chars: int) -> str:
    """Summarize a list of events into a token-efficient prose string."""
    lines: list[str] = []
    for e in events:
        line = f"- {e.event_type}"
        if e.event_type == "AgentNodeExecuted":
            line += f" ({e.payload.get('node_name', '?')})"
        elif e.event_type == "AgentToolCalled":
            line += f" ({e.payload.get('tool_name', '?')})"
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... ({len(events)} events total)"
    return text


def _truncate_payload(payload: dict, max_len: int = 200) -> str:
    """Truncate a payload dict to a readable string."""
    import json
    text = json.dumps(payload, default=str)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text
