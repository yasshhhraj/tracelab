# TraceLab — Detailed Implementation Plan

> **Principle:** Every checkpoint leaves the system in a tested, deployable, runnable state.  
> **Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite → PostgreSQL, asyncio, Next.js (dashboard).  
> **Constraint (from AGENTS.md):** `asyncio.gather()` for parallelism; no LangGraph / Prefect; all agent shell access through allowlisted tool layer only; credentials never in prompts.

---

## Checkpoint Map

```
CP-01  Repo scaffold + CI skeleton
CP-02  Database schema + migrations
CP-03  Tool layer (safe shell execution)
CP-04  Git worktree manager
CP-05  AgentModel abstraction + single investigation agent
CP-06  Parallel investigation (3 agents via asyncio.gather)
CP-07  Verification engine (fail-before / pass-after)
CP-08  Arbiter agent + diagnosis report
CP-09  REST API (full lifecycle)
CP-10  Frontend dashboard (investigations list + detail)
CP-11  Jira intake (MCP / REST)
CP-12  GitHub draft PR creation
CP-13  Seeded demo benchmark + end-to-end smoke test
```

---

## CP-01 — Repository Scaffold + CI Skeleton

**Goal:** An empty-but-runnable project that passes linting and a trivial health-check test.  
**Deployable state:** `docker compose up` starts the FastAPI server; `pytest` reports 0 failures.

### Directory layout

```
tracelab/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI app factory
│   │   ├── config.py          # Pydantic Settings (env-driven)
│   │   └── routers/
│   │       └── health.py      # GET /health → { "status": "ok" }
│   ├── tests/
│   │   └── test_health.py
│   ├── pyproject.toml         # dependencies + tool config
│   └── Dockerfile
├── frontend/                  # Next.js (empty scaffold for now)
├── docker-compose.yml
├── .env.example
└── README.md
```

### Tasks

1. `pyproject.toml` — declare deps: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `pytest`, `pytest-asyncio`, `httpx`, `sqlalchemy[asyncio]`, `aiosqlite`, `alembic`.
2. `app/main.py` — create FastAPI app, mount `/health` router, lifespan hook that prints startup banner.
3. `app/config.py` — `Settings` with `DATABASE_URL`, `LLM_API_KEY` (never logged), `ENVIRONMENT`.
4. `tests/test_health.py` — async HTTP test with `httpx.AsyncClient`, assert `200 { "status": "ok" }`.
5. `Dockerfile` — multi-stage: `python:3.12-slim`, install deps, run `uvicorn`.
6. `docker-compose.yml` — services: `api` (FastAPI), `db` (postgres:16-alpine), volume for DB.
7. `README.md` — one-command quickstart.

### Acceptance criteria

- `pytest backend/` → green.
- `docker compose up` → `GET /health` returns `200`.
- `ruff check .` → no errors.

---

## CP-02 — Database Schema + Migrations

**Goal:** All core entities (PRD §31–32) exist in the database with Alembic migrations.  
**Deployable state:** `alembic upgrade head` creates all tables; model unit tests pass.

### Schema (SQLAlchemy async models)

```python
# investigations, hypotheses, experiments, test_evidence,
# patches, diagnoses, agent_events
```

| Table           | Key columns (abbreviated)                                                           |
|-----------------|-------------------------------------------------------------------------------------|
| `investigations`| `id`, `external_issue_id`, `repository`, `base_branch`, `status`, `bug_context` (JSON), timestamps |
| `hypotheses`    | `id`, `investigation_id`, `agent_type` (ENUM), `summary`, `reasoning_summary`, `candidate_fix`, `status` (ENUM), `confidence`, `suspected_files` (JSON) |
| `experiments`   | `id`, `hypothesis_id`, `command`, `working_directory`, `exit_code`, `stdout`, `stderr`, `duration_ms` |
| `test_evidence` | `id`, `hypothesis_id`, `test_path`, `pre_fix_result`, `post_fix_result`, `existing_suite_result`, `runs`, `failures_before`, `failures_after` |
| `patches`       | `id`, `hypothesis_id`, `branch`, `commit_sha`, `diff`, `files_changed` (JSON)      |
| `diagnoses`     | `id`, `investigation_id`, `selected_hypothesis_id`, `summary`, `evidence` (JSON), `risk` |
| `agent_events`  | `id`, `investigation_id`, `agent`, `action`, `target`, `payload` (JSON), `timestamp` |

### Tasks

1. Create `app/db/models.py` — SQLAlchemy `DeclarativeBase` + all seven models.
2. Create `app/db/session.py` — async engine factory, `get_session` dependency.
3. Run `alembic init alembic/` and configure `env.py` to use the async engine.
4. Generate initial migration: `alembic revision --autogenerate -m "initial schema"`.
5. Unit tests: `tests/test_models.py` — create one `Investigation`, persist, retrieve, assert fields.

### Acceptance criteria

- `alembic upgrade head` on a fresh SQLite file creates all 7 tables with no errors.
- `pytest tests/test_models.py` → green.

---

## CP-03 — Tool Layer (Safe Shell Execution)

**Goal:** A controlled execution layer that allowlists commands and enforces resource limits.  
**Deployable state:** Tool layer unit tests all pass; dangerous commands are blocked.

### Interface

```python
# app/tools/executor.py
class CommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

async def run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: int = 120,
) -> CommandResult: ...
```

### Allowlist (from AGENTS.md + PRD §35)

```python
ALLOWED_EXECUTABLES = {"git", "pytest", "npm", "pnpm", "mvn", "gradle", "python", "python3"}
```

Any command whose first token is not in the allowlist raises `CommandNotPermittedError`.

### Additional tools

| Tool function       | Purpose                                              |
|---------------------|------------------------------------------------------|
| `search_code`       | `grep -rn` inside a worktree path                    |
| `read_file`         | read text file, return content + line count          |
| `git_log`           | `git log --oneline -n N -- <path>`                   |
| `git_blame`         | `git blame <file>`                                   |
| `git_diff`          | `git diff <base>..<head> -- <path>`                  |
| `run_test`          | `pytest <path> -x --tb=short` or equivalent runner   |
| `write_test`        | write a string to a file path (inside worktree only) |
| `apply_patch`       | `git apply` from a unified diff string               |

### Resource controls (from PRD §36)

- Max command runtime: `120s` (configurable via `Settings`).
- Max patch attempts per hypothesis: `2`.
- Max hypotheses per investigation: `3`.

### Tasks

1. `app/tools/executor.py` — async subprocess with timeout, allowlist check, result model.
2. `app/tools/git_tools.py` — `git_log`, `git_blame`, `git_diff`, `apply_patch`.
3. `app/tools/code_tools.py` — `search_code`, `read_file`, `write_test`.
4. `app/tools/test_runner.py` — `run_test` (detects pytest/npm/mvn by project file presence).
5. `tests/test_executor.py` — test allowlist enforcement, timeout, exit code capture.
6. `tests/test_git_tools.py` — run against a tiny fixture git repo created in `tmp_path`.

### Acceptance criteria

- `run_command(["rm", "-rf", "/"], ...)` raises `CommandNotPermittedError`.
- `run_command(["git", "status"], cwd=fixture_repo)` returns `exit_code=0`.
- `run_command(["sleep", "200"], ...)` (blocked — sleep not in allowlist) → `CommandNotPermittedError`. *(If sleep were somehow allowed, timeout would fire.)*
- All tool tests green.

---

## CP-04 — Git Worktree Manager

**Goal:** Create and destroy isolated `git worktree` environments per hypothesis.  
**Deployable state:** Worktree manager tests pass; worktrees are cleaned up after use.

### Interface

```python
# app/worktree/manager.py
class WorktreeManager:
    async def create(
        self, repo_path: Path, branch: str, hypothesis_id: str
    ) -> Path: ...

    async def destroy(self, hypothesis_id: str) -> None: ...

    async def get_path(self, hypothesis_id: str) -> Path: ...
```

Branch naming follows PRD §21:

```
ai-debug/{ISSUE_ID}-h{N}   e.g.  ai-debug/PVS-421-h2
```

### Tasks

1. `app/worktree/manager.py` — uses `run_command(["git", "worktree", "add", ...])` internally.
2. Worktree paths stored in a dict (in-memory for prototype; persisted in DB for production).
3. `__aexit__` / `destroy` runs `git worktree remove --force <path>` and deletes the directory.
4. `tests/test_worktree.py` — create a bare fixture repo, add worktree, verify independent file state, destroy, confirm path removed.

### Acceptance criteria

- Three worktrees for the same repo can coexist with independent file modifications.
- `destroy()` removes both the directory and the git worktree reference.
- Tests green.

---

## CP-05 — AgentModel Abstraction + Single Investigation Agent

**Goal:** Implement the `AgentModel` interface (PRD §28) and wire up one working investigation agent that produces a structured `Hypothesis`.  
**Deployable state:** CLI `python -m tracelab.cli investigate --bug "..." --repo ./fixture` prints a valid JSON hypothesis.

### Interfaces

```python
# app/agents/base.py
class AgentContext(BaseModel):
    bug_context: BugContext
    repo_path: Path
    worktree_path: Path
    investigation_id: str

class AgentResult(BaseModel):
    hypothesis: Hypothesis
    experiments: list[ExperimentRecord]
    events: list[AgentEvent]

class AgentModel(ABC):
    @abstractmethod
    async def run(
        self,
        context: AgentContext,
        tools: list[Callable],
    ) -> AgentResult: ...
```

```python
# app/schemas/bug_context.py
class BugContext(BaseModel):
    issue_id: str
    symptom: str
    expected: str
    actual: str
    affected_area: str
    error_type: str           # "deterministic" | "intermittent" | "regression"
    known_evidence: list[str]
    stack_trace: str | None = None
    repository: str
    base_branch: str
```

```python
# app/schemas/hypothesis.py
class Hypothesis(BaseModel):
    hypothesis_id: str
    agent_type: Literal["code_path", "git_history", "test_behavior"]
    summary: str
    suspected_files: list[str]
    reasoning_summary: str
    reproduction_plan: list[str]
    candidate_fix: str
    confidence: Literal["low", "medium", "high"]   # informational only
    status: HypothesisStatus = HypothesisStatus.PROPOSED
```

### LLM provider

- Implement `OpenAIAgentModel` (or `WatsonxAgentModel`) behind `AgentModel`.
- Model name injected from `Settings.LLM_MODEL`.
- System prompt + tool definitions built in the concrete agent class.
- API key read from env — never appears in logged prompts.

### Code-Path Agent (first agent to implement)

- System prompt: instructs the model to act as a code-path investigator.
- Available tools: `search_code`, `read_file`, `git_diff`.
- Must emit structured `Hypothesis` JSON via function-calling / structured output.

### Tasks

1. `app/schemas/` — `BugContext`, `Hypothesis`, `HypothesisStatus`, `InvestigationStatus`.
2. `app/agents/base.py` — abstract interfaces.
3. `app/agents/llm/openai_model.py` — concrete `AgentModel` using OpenAI chat completions with tool calling.
4. `app/agents/code_path_agent.py` — prompt, tool list, output parser.
5. `app/cli.py` — `python -m tracelab.cli investigate` command using `click`.
6. `tests/test_code_path_agent.py` — mock `AgentModel.run`, assert hypothesis schema valid.

### Acceptance criteria

- `pytest tests/test_code_path_agent.py` passes with mocked LLM.
- `Hypothesis` Pydantic model validates correctly.
- CLI runs end-to-end with real LLM (manual check; not required for CI).

---

## CP-06 — Parallel Investigation (3 Agents via `asyncio.gather`)

**Goal:** All three investigation agents run concurrently; each produces an independent hypothesis.  
**Deployable state:** `POST /api/investigations` → creates 3 hypotheses concurrently; status visible via API.

### Agents to add

| Agent              | Focus                                                        | Primary tools                        |
|--------------------|--------------------------------------------------------------|--------------------------------------|
| `GitHistoryAgent`  | Inspect recent commits, blame, diff around affected files    | `git_log`, `git_blame`, `git_diff`   |
| `TestBehaviorAgent`| Find existing tests, run them, identify missing coverage     | `run_test`, `search_code`, `read_file`|

### Orchestrator

```python
# app/orchestrator.py
async def run_investigation(investigation_id: str) -> None:
    ...
    results = await asyncio.gather(
        code_path_agent.run(context),
        git_history_agent.run(context),
        test_behavior_agent.run(context),
        return_exceptions=True,
    )
    # persist each Hypothesis to DB
    # update investigation status → INVESTIGATING → VERIFYING
```

### Investigation status flow

```
CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING
       → WAITING_FOR_REVIEW → APPROVED / REJECTED / FAILED / BLOCKED
```

### Tasks

1. `app/agents/git_history_agent.py` — prompt + tools.
2. `app/agents/test_behavior_agent.py` — prompt + tools.
3. `app/orchestrator.py` — `run_investigation`, status transitions, event emission.
4. Wire `POST /api/investigations` to spawn `asyncio.create_task(run_investigation(...))` in background.
5. `tests/test_orchestrator.py` — mock all three agents, assert all 3 hypotheses persisted, assert correct status transitions.

### Acceptance criteria

- `POST /api/investigations` returns `{ "id": "...", "status": "CREATED" }` immediately.
- Background task fires; after mock agents resolve, DB contains 3 hypotheses.
- `GET /api/investigations/{id}/hypotheses` returns all 3.
- Tests green.

---

## CP-07 — Verification Engine (Fail-Before / Pass-After)

**Goal:** Implement the full verification contract from PRD §13 for each hypothesis.  
**Deployable state:** Given a seeded bug repo, the engine correctly marks one hypothesis VERIFIED and the others REJECTED.

### Verification contract

```
reproduction = successful
AND regression_test_before_fix = FAIL
AND regression_test_after_fix  = PASS
AND relevant_existing_tests    = PASS
```

### Verification steps per hypothesis

| Step | Action                            | Expected outcome     |
|------|-----------------------------------|----------------------|
| 1    | Reproduce bug (run_test / command)| any failure observed |
| 2    | Generate regression test (LLM)    | new test file written|
| 3    | Pre-fix: run regression test      | **FAIL**             |
| 4    | Apply candidate patch             | diff applied cleanly |
| 5    | Post-fix: run regression test     | **PASS**             |
| 6    | Run existing relevant tests       | **PASS**             |
| 7    | (optional) full suite             | **PASS**             |

### Flaky bug handling (PRD §14)

```python
# If bug_context.error_type == "intermittent":
#   run regression test N times (default 50, max 100)
#   compute failure_rate_before, failure_rate_after
#   VERIFIED iff failures_before > 0 AND failures_after == 0
FLAKY_DEFAULT_RUNS = 50
FLAKY_MIN_RUNS     = 20
FLAKY_MAX_RUNS     = 100
```

### Hypothesis outcomes

`VERIFIED | REJECTED | INCONCLUSIVE | BLOCKED`

### Tasks

1. `app/verification/engine.py` — `VerificationEngine.verify(hypothesis, worktree_path, bug_context)`.
2. `app/verification/regression_test_generator.py` — LLM call to generate a regression test given hypothesis + relevant code.
3. `app/verification/flaky_runner.py` — repeated-run logic for intermittent bugs.
4. Patch-attempt limit: max 2 per hypothesis (rejects after 2 failed patches).
5. Persist `TestEvidence` and `Experiment` rows for every step.
6. `tests/test_verification_engine.py` — fixture repo with a seeded race-condition bug; assert pre-fix FAIL, post-fix PASS.

### Acceptance criteria

- Seeded deterministic bug: engine marks hypothesis VERIFIED.
- Seeded bad hypothesis (wrong file): engine marks hypothesis REJECTED.
- Pre-fix FAIL is enforced — if regression test passes before fix, hypothesis status = INCONCLUSIVE.
- All tests green.

---

## CP-08 — Arbiter Agent + Diagnosis Report

**Goal:** The Arbiter evaluates all hypotheses by evidence (not model confidence) and produces a structured `Diagnosis`.  
**Deployable state:** After verification, `GET /api/investigations/{id}` includes a `diagnosis` object.

### Arbiter inputs

- All hypotheses with their status.
- `TestEvidence` for each.
- `Experiments` (commands run, outputs).
- `Patches` (diffs, files changed).

### Arbiter selection criteria (PRD §15 — in priority order)

1. Successful reproduction.
2. Clear causal explanation.
3. Fail-before / pass-after regression evidence.
4. Minimal code change.
5. Low regression risk.
6. Existing suite passing.

**Must not select winner based on `confidence` field** (AGENTS.md constraint).

### Diagnosis schema

```python
class Diagnosis(BaseModel):
    investigation_id: str
    selected_hypothesis_id: str | None   # None if no hypothesis verified
    summary: str
    verified_cause: str
    evidence: list[str]                  # bullet points
    rejected_hypotheses: list[RejectedHypothesis]
    changed_files: list[str]
    risk: Literal["low", "medium", "high"]
    recommended_action: str
```

### Failure cases

- All hypotheses REJECTED → `summary = "No hypothesis was sufficiently supported by evidence."`, `selected_hypothesis_id = None`.
- All INCONCLUSIVE → status = `BLOCKED`, include missing information list.

### Tasks

1. `app/agents/arbiter_agent.py` — builds evidence summary, calls LLM with structured output.
2. `app/orchestrator.py` — after all verifications complete, call `arbiter.run(...)`, persist `Diagnosis`, set investigation status to `WAITING_FOR_REVIEW`.
3. `GET /api/investigations/{id}` response includes embedded `diagnosis`.
4. `tests/test_arbiter.py` — mock hypotheses with one VERIFIED, two REJECTED; assert correct `selected_hypothesis_id`, risk, evidence list.

### Acceptance criteria

- Arbiter selects the hypothesis with strongest evidence, not the one with `confidence = "high"`.
- `selected_hypothesis_id = None` when all hypotheses fail.
- Tests green.

---

## CP-09 — Full REST API

**Goal:** Expose the complete API surface from PRD §33 with request/response validation, status enforcement, and error handling.  
**Deployable state:** All API endpoints tested; `POST /pull-request` blocked unless status is `APPROVED`.

### Endpoints

| Method | Path                                      | Notes                                             |
|--------|-------------------------------------------|---------------------------------------------------|
| `POST` | `/api/investigations`                     | Start investigation; trigger background task      |
| `GET`  | `/api/investigations`                     | List all investigations (paginated)               |
| `GET`  | `/api/investigations/{id}`               | Full investigation + embedded diagnosis            |
| `GET`  | `/api/investigations/{id}/hypotheses`    | All hypotheses for investigation                  |
| `GET`  | `/api/hypotheses/{id}/evidence`          | Experiments + TestEvidence for one hypothesis     |
| `POST` | `/api/investigations/{id}/approve`       | Set status → APPROVED                             |
| `POST` | `/api/investigations/{id}/reject`        | Set status → REJECTED                             |
| `POST` | `/api/investigations/{id}/pull-request`  | **Only if status == APPROVED** (AGENTS.md rule)   |
| `GET`  | `/api/investigations/{id}/events`        | Agent event stream (for dashboard timeline)       |

### Status enforcement (AGENTS.md constraint)

```python
# route handler for POST /pull-request
if investigation.status != InvestigationStatus.APPROVED:
    raise HTTPException(status_code=409, detail="Investigation must be APPROVED before PR creation")
```

### Tasks

1. `app/routers/investigations.py` — all investigation routes.
2. `app/routers/hypotheses.py` — evidence route.
3. Pydantic request/response schemas for every endpoint.
4. `tests/test_api_investigations.py` — full CRUD cycle, approval gate test, reject test.
5. `tests/test_api_pr.py` — assert 409 when not approved, 200 when approved.

### Acceptance criteria

- `POST /pull-request` without prior `POST /approve` returns `409`.
- All endpoints return correct HTTP status codes.
- `pytest tests/` green.

---

## CP-10 — Frontend Dashboard

**Goal:** A minimal but functional Next.js dashboard showing investigations list and detail view.  
**Deployable state:** `npm run dev` serves a working UI; `npm test` (if added) passes.

**Detailed execution plan:** [CP-10 frontend dashboard](CP-10-frontend-dashboard.md).

### Routes

| Route                    | Component                                             |
|--------------------------|-------------------------------------------------------|
| `/investigations`        | Table of all investigations with status badges        |
| `/investigations/:id`    | Full detail: ticket context, hypothesis cards, evidence, diagnosis, approve/reject buttons |

### Investigation detail sections (PRD §24)

1. **Ticket context** — issue ID, symptom, expected vs actual, repository, base branch.
2. **Hypothesis timeline** — three cards, each with agent type, status badge, reasoning summary.
3. **Evidence panel** — per-hypothesis: experiments run, files inspected, test results.
4. **Patch diff viewer** — syntax-highlighted unified diff.
5. **Verification proof** — FAIL-before / PASS-after side-by-side.
6. **Diagnosis card** — selected hypothesis, evidence bullets, risk badge, rejected list.
7. **Action buttons** — Approve / Reject (only shown in `WAITING_FOR_REVIEW`). Draft PR creation belongs to CP-12; the current endpoint returns a placeholder URL.

### UX rule (PRD §40)

Show concrete evidence over model confidence:

```
Reproduced?       YES
Failed before patch?  YES
Passed after patch?   YES
Existing tests?   PASS (recorded result)
Competing hypotheses checked?  3
```

### Tasks

1. Extend the existing Next.js 16 scaffold in `frontend/`.
2. `frontend/app/investigations/page.tsx` — list view with `useSWR` polling.
3. `frontend/app/investigations/[id]/page.tsx` — detail view.
4. Shared components: `HypothesisCard`, `EvidencePanel`, `DiffViewer`, `DiagnosisCard`, `StatusBadge`.
5. `frontend/lib/api.ts` — typed fetch wrappers for all backend endpoints.
6. Use a same-origin Next.js `/api` rewrite to the configured FastAPI origin (CORS for `localhost:3000` already exists).

### Acceptance criteria

- `/investigations` renders a list populated from the backend.
- `/investigations/:id` renders all sections without runtime errors.
- Status badge reflects live investigation state (polling every 3s while active).
- Approve/Reject buttons call the correct API endpoints.

---

## CP-11 — Jira Intake (MCP / REST)

**Goal:** A Jira bug can trigger an investigation directly from the dashboard or via webhook.  
**Deployable state:** Selecting a Jira issue launches an investigation end-to-end.

### Trigger modes

1. **Manual** — dashboard "Import from Jira" dialog: enter issue ID → `POST /api/investigations`.
2. **Webhook** (optional) — Jira `issue_updated` webhook where `status = "Ready for AI Debugging"`.

### Jira client

```python
# app/integrations/jira_client.py
class JiraClient:
    async def get_issue(self, issue_id: str) -> JiraIssue: ...
    async def add_comment(self, issue_id: str, body: str) -> None: ...
    async def update_status(self, issue_id: str, status: str) -> None: ...
```

- `JIRA_BASE_URL`, `JIRA_API_TOKEN` read from env — never logged or placed in prompts (AGENTS.md).
- `JiraIssue` parsed into `BugContext` by the Intake Agent.

### Intake agent

```python
# app/agents/intake_agent.py
# Takes raw JiraIssue → produces normalized BugContext
# Uses LLM to extract symptom, expected, actual, error_type, affected_area
```

### Jira state updates (PRD §19–20)

After diagnosis ready: add comment to Jira with investigation summary + PR link.

### Tasks

1. `app/integrations/jira_client.py` — HTTP client using `httpx`.
2. `app/agents/intake_agent.py` — normalises raw Jira fields into `BugContext`.
3. `app/routers/jira.py` — `POST /api/jira/import/{issue_id}` and optional `POST /api/jira/webhook`.
4. After diagnosis persisted: fire `jira_client.add_comment(...)` with structured summary.
5. `tests/test_jira_intake.py` — mock HTTP responses, assert `BugContext` fields populated correctly.

### Acceptance criteria

- `POST /api/jira/import/PVS-421` → investigation created with normalized `BugContext`.
- `JIRA_API_TOKEN` does not appear in any log line.
- Tests green with mocked Jira HTTP.

---

## CP-12 — GitHub Draft PR Creation

**Goal:** After human approval, the winning patch is pushed and a draft PR is opened.  
**Deployable state:** `POST /api/investigations/{id}/pull-request` creates a real GitHub draft PR.

### GitHub client

```python
# app/integrations/github_client.py
class GitHubClient:
    async def push_branch(self, repo: str, branch: str, local_path: Path) -> str: ...
    async def create_draft_pr(
        self, repo: str, branch: str, base: str, title: str, body: str
    ) -> str: ...  # returns PR URL
```

- `GITHUB_TOKEN` from env — never in prompts.

### PR body template (PRD §22)

```
Fix {ISSUE_ID}: {symptom}

AI-assisted diagnosis (TraceLab)

Verified cause:
{verified_cause}

Verification:
- Regression test fails before patch
- Regression test passes after patch
- {N} existing tests pass

Alternative hypotheses investigated:
{rejected_list}

Human review required before merge.
```

### Route guard (AGENTS.md + PRD §17)

Already enforced in CP-09 (`status == APPROVED`).

### Tasks

1. `app/integrations/github_client.py` — uses GitHub REST API v3.
2. `app/routers/investigations.py` — implement `POST /pull-request` handler: push branch, create PR, persist `pr_url` on `Patch`, update Jira comment.
3. `tests/test_github_client.py` — mock GitHub API, assert correct PR body template, assert APPROVED gate.

### Acceptance criteria

- Without prior approval → `409 Conflict`.
- With approval → GitHub draft PR created with correct body.
- PR URL stored and returned in response.
- `GITHUB_TOKEN` not in any log.

---

## CP-13 — Seeded Demo Benchmark + End-to-End Smoke Test

**Goal:** A reproducible, self-contained demo benchmark that exercises the full system with the hero demo scenario from PRD §25.  
**Deployable state:** `make demo` runs end-to-end, resolves the seeded bug, produces a diagnosis, creates a mock PR.

### Seeded demo repository

```
demo_repo/
├── review_service.py       # buggy implementation (no DB-level uniqueness)
├── review_repository.py
├── tests/
│   └── test_review.py      # existing sequential tests (all pass before fix)
└── requirements.txt
```

The bug: `ReviewRun` duplicate rows on concurrent requests (idempotency race condition — PRD §25).

Three plausible hypotheses planted in fixtures:

| Agent  | Hypothesis          | Expected outcome |
|--------|---------------------|-----------------|
| Code   | Idempotency race (correct) | VERIFIED    |
| Git    | Stale cache         | REJECTED         |
| Test   | Malformed input hash | REJECTED        |

### Benchmark suite (PRD §39)

10 seeded bugs:

```
3 logic bugs
2 data/persistence bugs
2 concurrency bugs
2 regression bugs
1 flaky bug
```

Each bug has a known correct hypothesis. The benchmark measures:

- `verified_resolution_rate` — target ≥ 80%.
- `time_to_verified_diagnosis` — target ≤ 3 min per bug.
- `hypothesis_coverage` — 3 distinct per investigation.
- `regression_proof_rate` — target = 100% of VERIFIED bugs.
- `human_acceptance_rate` — tracked after manual review runs.

### End-to-end smoke test

```python
# tests/e2e/test_demo_flow.py
async def test_hero_demo():
    # 1. POST /api/investigations with demo repo + seeded bug context
    # 2. Poll GET /api/investigations/{id} until status == WAITING_FOR_REVIEW
    # 3. Assert exactly 1 hypothesis VERIFIED, 2 REJECTED
    # 4. Assert diagnosis.selected_hypothesis_id corresponds to Code agent
    # 5. POST /api/investigations/{id}/approve
    # 6. POST /api/investigations/{id}/pull-request → assert pr_url present
```

### Tasks

1. `demo_repo/` — create seeded Python package with intentional race-condition bug.
2. `scripts/seed_demo.py` — sets up demo repo git history (commits, blame trails for Git agent).
3. `tests/e2e/test_demo_flow.py` — full pipeline smoke test using real orchestrator + mocked LLM responses.
4. `Makefile` — `make demo`, `make test`, `make lint`, `make migrate`, `make dev`.
5. Update `README.md` with full demo walkthrough.

### Acceptance criteria

- `make demo` runs without manual intervention; prints diagnosis to stdout.
- `pytest tests/e2e/` green (with mocked LLM and GitHub/Jira).
- Investigation reaches `WAITING_FOR_REVIEW` with correct VERIFIED hypothesis.
- Approve → mock PR URL returned.
- No credentials appear in any log output.

---

## Summary Table

| CP  | What ships                                    | Key test signal                                  |
|-----|-----------------------------------------------|--------------------------------------------------|
| 01  | FastAPI scaffold + Docker                     | `GET /health` 200; `pytest` green                |
| 02  | Full DB schema + Alembic migrations           | `alembic upgrade head` + model round-trip test   |
| 03  | Tool layer + allowlist + resource limits      | Blocked command test + git tool test             |
| 04  | Git worktree manager                          | 3 independent worktrees coexist, destroy cleans  |
| 05  | AgentModel + CodePath agent + CLI             | Mock hypothesis validation test                  |
| 06  | 3 parallel agents via `asyncio.gather`        | All 3 hypotheses persisted; correct status flow  |
| 07  | Verification engine (fail-before/pass-after)  | Seeded bug: VERIFIED; bad hypothesis: REJECTED   |
| 08  | Arbiter + Diagnosis                           | Evidence-based selection; all-fail fallback      |
| 09  | Full REST API with approval gate              | `POST /pr` blocked without approve (409)         |
| 10  | Next.js dashboard                             | List + detail views render; approve buttons work |
| 11  | Jira intake + comment feedback                | Mock Jira import → BugContext; token not logged  |
| 12  | GitHub draft PR creation                      | Mock PR created; gate enforced                   |
| 13  | Seeded demo + E2E smoke test                  | Full hero demo flow automated                    |

---

## Cross-Cutting Constraints (enforced at every checkpoint)

| Constraint                           | Source         | Enforcement                                          |
|--------------------------------------|----------------|------------------------------------------------------|
| `asyncio.gather()` for parallelism   | AGENTS.md      | No workflow library imports allowed                  |
| All agent shell via tool layer       | AGENTS.md      | Agents receive tool callables, not raw subprocess    |
| Allowlisted commands only            | AGENTS.md      | `CommandNotPermittedError` on non-allowlist commands |
| Credentials never in prompts         | AGENTS.md      | Env-only injection; LLM call logger redacts keys     |
| `confidence` is informational only   | AGENTS.md      | Arbiter selection logic has no `confidence` branch   |
| Pre-fix regression test MUST fail    | AGENTS.md      | Verification engine rejects hypothesis if pre=PASS   |
| PR requires `APPROVED` status        | AGENTS.md      | Route guard raises 409 otherwise                     |
| Flaky detection: min 20 runs         | AGENTS.md      | `FLAKY_MIN_RUNS = 20` constant; assertion in engine  |
| Max 3 hypotheses per investigation   | AGENTS.md      | Orchestrator hard-caps at 3                          |
| Max 2 patch attempts per hypothesis  | AGENTS.md      | Verification engine tracks `attempt_count`           |
| Max 120s per command                 | AGENTS.md      | `run_command` default timeout                        |
