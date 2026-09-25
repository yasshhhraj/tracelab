# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project: TraceLab

Multi-agent differential debugging platform. Full PRD is in [`idea`](idea).

## Recommended Stack (from PRD)

- **Backend/Orchestrator**: Python 3.12+, FastAPI, Pydantic, asyncio
- **Database**: PostgreSQL (SQLite acceptable for prototype)
- **Agent parallelism**: `asyncio.gather()` — no external workflow framework needed for MVP

## Architecture

Three parallel investigation agents (Code-Path, Git-History, Test-Behavior) feed into a Verification Engine, then an Arbiter Agent produces a Diagnosis Report requiring human approval before any PR is created.

```
Jira MCP → Orchestrator → [Code Agent | Git Agent | Test Agent] → Hypothesis Store
  → Verification Engine (isolated git worktrees per hypothesis) → Arbiter → Diagnosis → Human Approval → PR/Jira
```

## Core Verification Contract

A hypothesis is VERIFIED only when **all four** hold:
1. `reproduction = successful`
2. `regression_test_before_fix = FAIL`
3. `regression_test_after_fix = PASS`
4. `relevant_existing_tests = PASS`

Hypotheses may be: `VERIFIED | REJECTED | INCONCLUSIVE | BLOCKED`

## Key Data Entities

`Investigation → Hypothesis → Experiment + TestEvidence + Patch → Diagnosis`

Branch format for hypothesis worktrees: `ai-debug/{ISSUE_ID}-h{N}` (e.g. `ai-debug/PVS-421-h2`)

## Critical Constraints

- **Never auto-merge** — human approval is mandatory before PR creation; PR creation requires prior `approve` call
- **Arbiter must not select winner by model confidence** — evidence (reproduction + test proof) determines the winner
- **Agents must not have unrestricted shell access** — use allowlisted tool layer (`git`, `pytest`, `npm test`, etc.)
- **Credentials never in prompts** — API tokens and repo credentials stay server-side only
- **Resource limits per investigation**: max 3 hypotheses, max 2 patch attempts per hypothesis, max 120s per command

## Flaky Bug Handling

Run repeated experiments (default 50, max 100 runs). Do not declare fixed after a single pass.

## Investigation States

`CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING → WAITING_FOR_REVIEW → APPROVED / REJECTED / FAILED / BLOCKED`

## API Shape

- `POST /api/investigations` — start (body: `jiraIssueId`, `repository`, `baseBranch`)
- `GET /api/investigations/{id}` — status + diagnosis
- `GET /api/investigations/{id}/hypotheses`
- `GET /api/hypotheses/{id}/evidence`
- `POST /api/investigations/{id}/approve` → enables PR creation
- `POST /api/investigations/{id}/reject`
- `POST /api/investigations/{id}/pull-request` — only after approval

## Build Order (Phases)

1. Local debugging engine (git worktrees, test runner, command execution)
2. Single investigation agent + structured hypothesis output
3. Parallel investigation (3 agents via `asyncio.gather`)
4. Verification engine (fail-before/pass-after proof)
5. Arbiter agent
6. Dashboard (`/investigations`, `/investigations/:id`)
7. Jira MCP intake
8. GitHub draft PR creation
9. Jira feedback/comment
