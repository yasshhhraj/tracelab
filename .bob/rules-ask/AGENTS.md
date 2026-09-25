# Project Documentation Context

- The entire specification lives in a single file [`idea`](../../idea) at the repo root — it is a PRD, not source code
- "Verification Engine" is the most critical component (PRD §12) — sections on architecture, API, and build order all subordinate to its fail-before/pass-after contract
- The Arbiter is NOT an LLM judge evaluating quality — it evaluates structured execution evidence; this distinction is intentional and core to the product pitch
- Jira integration is via Jira MCP (not direct REST API calls) per PRD §18
- Dashboard routes are `/investigations` (list) and `/investigations/:id` (detail with timeline, evidence, diffs, decision)
- The hero demo bug (duplicate records under concurrent requests) has three intentionally distinct hypotheses: cache issue (rejected), idempotency race (verified), malformed hash (rejected)
