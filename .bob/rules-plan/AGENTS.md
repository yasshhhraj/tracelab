# Project Architecture Rules

- Hypothesis isolation is via **git worktrees**, not Docker containers or VMs — each hypothesis branch format: `ai-debug/{ISSUE_ID}-h{N}`
- The three investigation agents (Code-Path, Git-History, Test-Behavior) are architecturally independent and must not share mutable state — they write to the Hypothesis Store only
- Verification Engine is a separate layer from the investigation agents — investigation produces hypotheses, verification proves/disproves them; conflating the two breaks the evidence chain
- Human approval gate is an absolute architectural constraint — no code path leads from `ARBITRATING` to PR creation without passing through `WAITING_FOR_REVIEW → APPROVED`
- Observability is event-driven per agent action (`agent_events` table) — the dashboard timeline is derived from this event log, not from polling agent status
- Database schema must be forward-only migrations (prototype constraint: rollbacks not planned)
- Resource limits are hard constraints, not soft warnings: max 3 hypotheses, max 2 patch attempts per hypothesis, 120s command timeout — the system must not enter unbounded agent loops
- The product is intentionally NOT an autonomous agent — positioning it as such in architecture would contradict the core differentiator (parallel evidence-based investigation vs. sequential guessing)
