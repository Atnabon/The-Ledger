# DESIGN.md — Apex Ledger Architectural Decisions

**Project**: The Ledger — Agentic Event Store & Enterprise Audit Infrastructure
**Author**: TRP1 FDE Candidate
**Date**: March 2026

---

## 1. Aggregate Boundary Justification

### Why ComplianceRecord Is Separate from LoanApplication

**Decision**: Four separate aggregates — `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`.

**Alternative considered**: Merging `ComplianceRecord` into `LoanApplication` as a single aggregate, since compliance checks are conceptually "about" the loan application.

**Why rejected — the coupling problem is write contention**:

In the Apex scenario, the `ComplianceAgent` writes rule results (`ComplianceRulePassed`, `ComplianceRuleFailed`) while the `DecisionOrchestratorAgent` may simultaneously read/write to the loan application stream (`DecisionGenerated`). If compliance events lived in the `loan-{id}` stream:

- Both agents would pass `expected_version` against the **same stream**
- A compliance rule write would conflict with a decision write — even though they are logically independent operations by different actors
- At 100 concurrent applications with 4 agents each, this creates **constant `OptimisticConcurrencyError` retries on non-conflicting writes**
- The retry budget is consumed by **false conflicts** rather than genuine business rule violations

**Specific failure mode**: Agent A (ComplianceAgent) reads `loan-APP-001` at version 7 and prepares `ComplianceRulePassed`. Agent B (Orchestrator) reads the same stream at version 7 and prepares `DecisionGenerated`. Agent A writes first (version → 8). Agent B's write fails with OCC error — even though the two operations are semantically independent. Agent B must reload, re-evaluate business rules, and retry. This wastes ~200ms per false conflict.

**With separate aggregates**: ComplianceAgent writes to `compliance-{id}` and DecisionOrchestrator writes to `loan-{id}`. They **never contend**. The LoanApplication aggregate reads the compliance stream when it needs to check compliance status (Rule 5), but this is a read — not a concurrent write.

**The general principle**: If two different actors write to an entity at different times for different reasons, they should be separate aggregates.

---

## 2. Projection Strategy

### ApplicationSummary

| Aspect | Decision | Justification |
|--------|----------|---------------|
| **Mode** | Async | Write latency is more important than read freshness for this projection. Loan officers tolerate 200ms staleness; agents cannot tolerate 200ms added write latency. |
| **SLO** | < 500ms lag | Based on Apex business requirement: loan officers refresh dashboards every 5 seconds. 500ms lag is imperceptible. |
| **Storage** | One row per application, upserted | O(1) read performance regardless of event count. |

### AgentPerformanceLedger

| Aspect | Decision | Justification |
|--------|----------|---------------|
| **Mode** | Async | Performance analytics are consumed in batch, not real-time. |
| **SLO** | < 2 seconds lag | Analytics dashboards refresh every 30 seconds. 2s lag is invisible. |
| **Storage** | One row per (agent_id, model_version) | Enables direct comparison between model versions without aggregation at query time. |

### ComplianceAuditView (Critical — Temporal Query Support)

| Aspect | Decision | Justification |
|--------|----------|---------------|
| **Mode** | Async | Strong-consistency path available via direct event stream load for critical reads. |
| **SLO** | < 2 seconds lag | Compliance queries are not real-time; the event stream is the authoritative source. |
| **Temporal Query** | Snapshot-based | Snapshots taken every 10 events per application AND on ComplianceCheckCompleted. |
| **Snapshot Strategy** | Event-count trigger (10) + completion trigger | Event-count ensures bounded replay cost (max 10 events from nearest snapshot). Completion trigger guarantees a snapshot exists at every significant compliance milestone. |

**Snapshot invalidation**: Snapshots are never invalidated — they are point-in-time records. If a compliance rule is re-evaluated (e.g., after a regulation update), new events are appended and new snapshots are created. Old snapshots remain valid for their point in time.

**Rebuild strategy**: `rebuild_from_scratch()` truncates the projection table and replays all compliance events from global position 0. During rebuild, live reads continue against the existing table until the new table is populated (swap on completion). This achieves rebuild without downtime.

---

## 3. Concurrency Analysis

### Expected OCC Error Rate Under Peak Load

**Scenario**: 100 concurrent applications, 4 agents each, ~1,000 applications/hour.

**Per-stream analysis**:
- Each `loan-{id}` stream receives ~12 events over its lifecycle
- With 4 agents processing concurrently, the write window is ~2 seconds per agent
- Probability of two agents writing to the **same** stream within the same 100ms window: ~5% per application
- At 100 concurrent applications: **~5 OCC errors per minute** on loan streams

**Cross-stream analysis**:
- Compliance, credit, fraud, and agent session streams are separate → **zero cross-aggregate OCC errors**
- This validates the aggregate boundary design: without separate aggregates, the rate would be ~20 OCC errors per minute (4x higher)

### Retry Strategy

| Parameter | Value | Justification |
|-----------|-------|---------------|
| **Max retries** | 3 | At 5% collision rate, P(3 consecutive failures) < 0.0125% |
| **Retry delay** | None (immediate) | The reload itself takes ~5ms, which is sufficient jitter |
| **Retry budget per request** | 500ms | 3 retries × ~150ms per attempt (load + validate + append) |
| **Failure mode** | Return error to caller | After 3 retries, the caller receives a structured error with `suggested_action: "reload_stream_and_retry"` |

**Key principle**: The retry is NOT blind re-execution. On each retry, the agent reloads the stream, reconstructs the aggregate, and re-evaluates business rules. If the winning agent's event makes the retry unnecessary (e.g., credit analysis already completed), the retrying agent **abandons** rather than retries.

---

## 4. Upcasting Inference Decisions

### CreditAnalysisCompleted v1 → v2

| Field | Strategy | Error Rate | Downstream Consequence |
|-------|----------|------------|----------------------|
| `model_version` | Inferred from `recorded_at` timestamp | ~5% during model rollover periods | AgentPerformanceLedger may misattribute ~5% of decisions. Acceptable for analytics, NOT for regulatory reporting. |
| `confidence_score` | **Null** — genuinely unknown | 0% (no fabrication) | Consumers must handle null. This is correct: a null confidence score forces explicit handling rather than silently passing Rule 4's confidence floor check. |
| `regulatory_basis` | Inferred from timestamp + known regulation schedule | ~2% during regulation transition periods | Audit trail context may reference wrong regulation version for ~2% of historical events. Acceptable for context; the original event's regulation version is lost. |

**Why null over fabrication for `confidence_score`**:
- Fabricating 0.5 would **pass** the confidence floor check (Rule 4: < 0.6 → REFER), misrepresenting legacy decisions as "reviewed"
- Any downstream analysis using confidence_score would treat fabricated values as real data
- Null forces the consumer to handle the missing case explicitly — this is the correct behavior

### DecisionGenerated v1 → v2

| Field | Strategy | Error Rate | Downstream Consequence |
|-------|----------|------------|----------------------|
| `confidence_score` | Null | 0% | Same reasoning as above |
| `contributing_agent_sessions` | Preserve if present, else empty list | 0% | Missing session references mean causal chain cannot be verified for v1 events |
| `model_versions` | Preserve if present, else empty dict | 0% | Model version tracking unavailable for v1 decisions |

**Performance note**: Reconstructing `model_versions` from the store (by loading each contributing session's `AgentContextLoaded` event) would require O(N) store lookups per historical event. At 1,847 seed events, this would add ~30 seconds to a full replay. The empty-dict approach is O(1) per event.

---

## 5. EventStoreDB Comparison

| Apex Ledger (PostgreSQL) | EventStoreDB Equivalent | Notes |
|--------------------------|------------------------|-------|
| `events` table | Streams | EventStoreDB stores events directly in named streams; no separate table needed |
| `event_streams` table | Stream metadata | EventStoreDB tracks this automatically |
| `load_all()` async generator | `$all` stream subscription | EventStoreDB's `$all` is a built-in projection; we simulate it with `global_position` ordering |
| `ProjectionDaemon` | Persistent subscriptions | EventStoreDB handles checkpointing, reconnection, and consumer group coordination natively. Our daemon must implement all of this manually |
| `projection_checkpoints` | Subscription checkpoints | Built into persistent subscriptions in EventStoreDB |
| `outbox` table + relay | N/A (built-in) | EventStoreDB supports projections and subscriptions natively; no outbox needed |
| `pg_advisory_lock` for distributed projections | Consumer groups + competing consumers | EventStoreDB handles distributed processing natively with partition assignment |
| Optimistic concurrency via `FOR UPDATE` + version check | `ExpectedVersion` parameter | Same concept, but EventStoreDB implements it at the storage engine level — no row-level locks needed |

**What EventStoreDB gives you that PostgreSQL must work harder to achieve**:

1. **Native stream semantics**: EventStoreDB treats streams as first-class citizens. Our PostgreSQL implementation simulates streams via `stream_id` column + unique constraint — this works but adds index overhead.
2. **Built-in `$all` stream**: EventStoreDB's global ordering is native. We achieve it via `BIGINT GENERATED ALWAYS AS IDENTITY`, which is efficient but requires careful handling of gaps.
3. **Persistent subscriptions with competing consumers**: EventStoreDB distributes events across consumer group members automatically. Our `pg_advisory_lock` approach is functional but has a gap window of up to one poll interval after a crash.
4. **Server-side projections**: EventStoreDB can run projection logic on the server side in JavaScript. We run projections in Python application code — more flexible but adds a network hop.

**Why PostgreSQL was chosen**: Ubiquity. Every Apex client already has PostgreSQL. Adding EventStoreDB introduces a new operational dependency — a decision that must be justified by throughput requirements exceeding ~10,000 events/second (our requirement is ~1,000 events/hour).

---

## 6. What I Would Do Differently

**The single most significant architectural decision I would reconsider**: The projection daemon's single-threaded polling model.

### The Problem

The current `ProjectionDaemon` processes all projections sequentially in a single asyncio loop. Under load, this creates a coupling: a slow projection (e.g., `ComplianceAuditView` with its snapshot writes) delays all other projections. The `ApplicationSummary` projection, which has a 500ms SLO, is held hostage by the `ComplianceAuditView`'s 2-second SLO.

### What I Would Change

Implement **per-projection asyncio tasks** with independent polling loops and checkpoints. Each projection runs in its own coroutine:

```python
async def run_forever(self):
    tasks = [
        asyncio.create_task(self._run_projection(p))
        for p in self._projections.values()
    ]
    await asyncio.gather(*tasks)
```

This decouples projection lag: `ApplicationSummary` maintains its 500ms SLO regardless of `ComplianceAuditView`'s processing time. The cost is slightly higher database connection usage (one connection per projection during polling), which is acceptable given our pool size of 10.

### Why I Didn't Do This

Time constraint. The sequential model was simpler to implement correctly and test. The SLO tests pass under the current load profile. But at 10x the current throughput, the sequential model would fail the `ApplicationSummary` SLO, and this refactor would become necessary.

### Second Consideration

I would also add **snapshot-accelerated aggregate loading** for the `LoanApplication` aggregate. Currently, `load()` replays the entire event stream on every command. For long-lived applications with 50+ events, this adds ~50ms to every write operation. A snapshot at every 20 events would cap replay at 20 events maximum, reducing load time to ~10ms. The schema table exists (`snapshots`); the integration is not yet implemented.
