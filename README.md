# Apex Ledger — Agentic Event Store & Enterprise Audit Infrastructure

An event-sourced system for processing commercial loan applications with multi-agent AI collaboration. Built for the TRP1 FDE Program, Arc 5.

## Overview

Apex Ledger is the immutable memory and governance backbone for multi-agent AI systems. It provides:

- **Append-only event store** with optimistic concurrency control (PostgreSQL-backed)
- **4 aggregate boundaries** — LoanApplication, AgentSession, ComplianceRecord, AuditLedger
- **CQRS** with async projection daemon and 3 read-model projections
- **43 event types** across 7 aggregate stream types
- **Gas Town pattern** for agent crash recovery via event replay
- **Cryptographic audit chains** with SHA-256 hash chain tamper detection
- **UpcasterRegistry** for transparent event schema evolution
- **MCP Server** with 8 tools (commands) and 6 resources (queries)
- **What-If Projector** for counterfactual regulatory scenarios
- **Regulatory Package Generator** for self-contained examination packages

## Prerequisites

- Python 3.12+
- PostgreSQL 15+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Quick Start

### 1. Install Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e ".[dev]"
```

### 2. Create Database

```bash
createdb apex_ledger
createdb apex_ledger_test  # for tests
```

### 3. Run Migrations

```bash
psql apex_ledger < db/schema.sql
psql apex_ledger_test < db/schema.sql
```

### 4. Environment Setup

```bash
export DATABASE_URL=postgresql://localhost/apex_ledger
```

### 5. Run Tests

```bash
# Run all tests
DATABASE_URL=postgresql://localhost/apex_ledger_test pytest tests/ -v

# Run specific test suites
pytest tests/test_concurrency.py -v      # OCC double-decision test
pytest tests/test_upcasting.py -v        # Upcasting + immutability
pytest tests/test_projections.py -v      # Projections + daemon
pytest tests/test_gas_town.py -v         # Crash recovery
pytest tests/test_mcp_lifecycle.py -v    # Full loan lifecycle
```

### 6. Start MCP Server

```bash
python -m src.mcp.server
```

## Project Structure

```
apex-ledger/
├── db/
│   └── schema.sql                          # PostgreSQL schema (5 tables)
├── docs/
│   ├── DOMAIN_NOTES.md                     # Graded deliverable (6 domain answers)
│   ├── DESIGN.md                           # Architectural decisions (6 sections)
├── src/
│   ├── event_store.py                      # EventStore async class
│   ├── models/events.py                    # 43 event types + exceptions
│   ├── aggregates/
│   │   ├── loan_application.py             # State machine + 6 business rules
│   │   ├── agent_session.py                # Gas Town enforcement
│   │   ├── compliance_record.py            # Compliance check tracking
│   │   └── audit_ledger.py                 # Hash chain audit trail
│   ├── commands/handlers.py                # 6 command handlers
│   ├── projections/
│   │   ├── daemon.py                       # ProjectionDaemon (fault-tolerant)
│   │   ├── application_summary.py          # ApplicationSummary projection
│   │   ├── agent_performance.py            # AgentPerformanceLedger projection
│   │   └── compliance_audit.py             # ComplianceAuditView (temporal queries)
│   ├── upcasting/
│   │   ├── registry.py                     # UpcasterRegistry
│   │   └── upcasters.py                    # CreditAnalysis + Decision v1→v2
│   ├── integrity/
│   │   ├── audit_chain.py                  # SHA-256 hash chain + tamper detection
│   │   └── gas_town.py                     # Agent crash recovery
│   ├── mcp/
│   │   ├── server.py                       # FastMCP server entry point
│   │   ├── tools.py                        # 8 MCP tools (command side)
│   │   └── resources.py                    # 6 MCP resources (query side)
│   ├── what_if/projector.py                # What-If counterfactual projector
│   └── regulatory/package.py               # Regulatory examination package
├── tests/
│   ├── test_concurrency.py                 # 4 OCC tests
│   ├── test_upcasting.py                   # 4 upcasting + immutability tests
│   ├── test_projections.py                 # 5 projection + daemon tests
│   ├── test_gas_town.py                    # 5 crash recovery tests
│   └── test_mcp_lifecycle.py               # Full lifecycle integration test
├── pyproject.toml
└── README.md
```

## Architecture

The system follows Event Sourcing + CQRS:

- **Write side**: Commands → Command Handlers → Aggregates (validate) → EventStore.append()
- **Read side**: EventStore → ProjectionDaemon → Projections → MCP Resources
- **Concurrency**: Optimistic concurrency control via `expected_version` on every append
- **Auditability**: Every decision is an immutable event with SHA-256 hash chain verification

## Key Design Decisions

1. **PostgreSQL as event store** — ubiquitous, ACID-compliant, supports LISTEN/NOTIFY for real-time subscriptions
2. **4 separate aggregates** — LoanApplication, AgentSession, ComplianceRecord, AuditLedger prevent write contention between concurrent agents
3. **Gas Town pattern** — agents write session-start events before any work, enabling crash recovery via stream replay
4. **Outbox pattern** — events written to outbox in same transaction for guaranteed downstream delivery
5. **Async projections with SLOs** — ApplicationSummary < 500ms lag, ComplianceAuditView < 2s lag
6. **UpcasterRegistry** — transparent event schema evolution without mutating stored events
