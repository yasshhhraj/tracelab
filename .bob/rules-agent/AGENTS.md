# Project Coding Rules

- Use `asyncio.gather()` for parallel agent execution — do not introduce LangGraph, Prefect, or any workflow framework for MVP
- All agent interactions with the repo must go through the tool layer (`search_code`, `run_test`, `apply_patch`, etc.) — no raw `subprocess` with arbitrary shell strings
- Block dangerous shell commands; allowlist: `git`, `pytest`, `npm test`, `pnpm test`, `mvn test`, `gradle test`
- The `AgentModel` abstraction must be used for all LLM calls so the model provider can be swapped without touching agent logic
- Hypothesis `confidence` field is informational metadata only — do not use it for selection logic in the Arbiter
- Regression test MUST fail against original code before a hypothesis can be VERIFIED — never skip pre-fix validation
- Each hypothesis gets its own isolated git worktree; never apply patches to the main repo checkout
- `POST /api/investigations/{id}/pull-request` must check investigation status is `APPROVED` before proceeding — enforce in the route handler
- Credentials (Jira token, GitHub token) must never appear in agent prompt context — inject them as environment variables only
- Flaky bug detection requires minimum 20 runs before drawing conclusions (config: `minimumRuns=20`, `defaultRuns=50`, `maximumRuns=100`)
