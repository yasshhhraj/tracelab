# CP-06 — Parallel Investigation (3 Agents via `asyncio.gather`)

> **Principle:** Every checkpoint leaves the system in a tested, deployable, runnable state.  
> **Builds on:** CP-01 through CP-05 (scaffold, DB, tool layer, worktree manager, AgentModel + CodePathAgent).  
> **Constraint (AGENTS.md):** `asyncio.gather()` for agent parallelism — no LangGraph / Prefect. All agent shell access through the allowlisted tool layer only. Credentials never in prompts.

---

## Goal

`POST /api/investigations` triggers three investigation agents concurrently — `CodePathAgent`, `GitHistoryAgent`, `TestBehaviorAgent` — each producing an independent `Hypothesis`. All three hypotheses are persisted to the database. Investigation status transitions are enforced (`CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING`). The full lifecycle is observable via `GET /api/investigations/{id}/hypotheses`.

---

## Deliverable State at CP-06 Completion

- `POST /api/investigations` returns `{ "id": "...", "status": "CREATED" }` immediately.
- Background task fires; after (mocked) agents resolve, DB contains exactly 3 hypotheses.
- `GET /api/investigations/{id}` reflects the current investigation status.
- `GET /api/investigations/{id}/hypotheses` returns all 3 hypotheses with correct `agent_type` values.
- `pytest backend/` is fully green with mocked LLM calls.
- `ruff check .` is clean.

---

## What Already Exists (from CP-01–CP-05)

| Asset | Location | Status |
|---|---|---|
| FastAPI app factory + CORS | `backend/app/main.py` | ✅ |
| `Settings` (env-driven, secrets safe) | `backend/app/config.py` | ✅ |
| All 7 DB models (including `Investigation`, `Hypothesis`, `Experiment`, `AgentEvent`) | `backend/app/db/models.py` | ✅ |
| `InvestigationStatus`, `HypothesisStatus`, `AgentType` constants | `backend/app/db/models.py` | ✅ |
| `AsyncSession` / `get_session` dependency | `backend/app/db/session.py` | ✅ |
| `AgentModel` ABC, `AgentContext`, `AgentResult`, `ExperimentRecord`, `AgentEvent` | `backend/app/agents/base.py` | ✅ |
| `OpenAIAgentModel` (agentic loop, tool calling, emit_hypothesis) | `backend/app/agents/llm/openai_model.py` | ✅ |
| `make_code_path_agent()` / `run_code_path_agent()` | `backend/app/agents/code_path_agent.py` | ✅ |
| `BugContext`, `Hypothesis` Pydantic schemas | `backend/app/schemas/` | ✅ |
| `WorktreeManager` + `make_branch_name()` | `backend/app/worktree/manager.py` | ✅ |
| Tool layer: `search_code`, `read_file`, `git_log`, `git_blame`, `git_diff`, `run_test`, `apply_patch` | `backend/app/tools/` | ✅ |
| `health` router | `backend/app/routers/health.py` | ✅ |
| `conftest.py` with `AsyncClient` fixture | `backend/tests/conftest.py` | ✅ |

---

## New Files to Create

```
backend/
├── app/
│   ├── agents/
│   │   ├── git_history_agent.py       # NEW — GitHistoryAgent
│   │   └── test_behavior_agent.py     # NEW — TestBehaviorAgent
│   ├── orchestrator.py                # NEW — run_investigation(), status transitions
│   └── routers/
│       └── investigations.py          # NEW — POST /api/investigations, GET routes
│           
└── tests/
    ├── test_git_history_agent.py      # NEW — mirrors test_code_path_agent.py pattern
    ├── test_test_behavior_agent.py    # NEW
    └── test_orchestrator.py           # NEW — core CP-06 tests
```

**Modified files:**

```
backend/app/main.py                    # mount investigations router
backend/tests/conftest.py              # add DB session fixture for integration tests
```

---

## Detailed Task Breakdown

---

### Task 1 — `GitHistoryAgent` (`app/agents/git_history_agent.py`)

**Pattern:** Identical structure to `code_path_agent.py`. The only differences are the system prompt and the tool set.

**System prompt focus:** Instruct the model to investigate *when* the bug was introduced — look at recent commits, blame, and diffs around the affected files. The agent must attribute the root cause to a specific commit or change rather than speculating about code structure.

**Tools:**
- `git_log` — recent commits on affected files (`git log --oneline -n N -- <path>`)
- `git_blame` — who/when each line was last changed
- `git_diff` — diff between two refs, filtered to a path
- `read_file` — read a file to understand context around a blame line

**Tool wrappers:** Same pattern as `_wrap_search_code()` / `_wrap_read_file()` in `code_path_agent.py`:
- Each wrapper is a named async function with a `__tool_schema__` attribute.
- Bridge parameter names to actual signatures in `app/tools/git_tools.py`.

**Factory function:**
```python
def make_git_history_agent() -> tuple[AgentModel, list[Callable]]: ...
async def run_git_history_agent(context: AgentContext) -> AgentResult: ...
```

**Key note:** `agent_type` passed to `OpenAIAgentModel` must be `"git_history"` — this is stored in `Hypothesis.agent_type` and validated against the `Literal["code_path", "git_history", "test_behavior"]` in `app/schemas/hypothesis.py`.

---

### Task 2 — `TestBehaviorAgent` (`app/agents/test_behavior_agent.py`)

**System prompt focus:** Instruct the model to find and run existing tests, identify what test coverage exists for the affected area, locate the specific failing assertion, and identify any missing regression coverage. It should surface whether the bug is detectable by existing tests.

**Tools:**
- `run_test` — run a pytest/npm test path and capture pass/fail output
- `search_code` — find test files and test functions
- `read_file` — read test file contents

**Factory function:**
```python
def make_test_behavior_agent() -> tuple[AgentModel, list[Callable]]: ...
async def run_test_behavior_agent(context: AgentContext) -> AgentResult: ...
```

**Key note:** `agent_type` must be `"test_behavior"`.

---

### Task 3 — Orchestrator (`app/orchestrator.py`)

This is the heart of CP-06. The orchestrator:

1. Loads the `Investigation` from the DB.
2. Creates one `WorktreeManager` (shared across all three agents for this investigation).
3. Creates three worktrees (`h1`, `h2`, `h3`) using `make_branch_name(issue_id, index)`.
4. Builds an `AgentContext` for each agent.
5. Runs all three agents **concurrently** with `asyncio.gather(..., return_exceptions=True)`.
6. For each result (or exception), persists the `Hypothesis`, `Experiment`, and `AgentEvent` rows.
7. Transitions investigation status: `INVESTIGATING → VERIFYING` (VERIFYING is the handoff to CP-07).
8. On any unrecoverable error: sets status to `FAILED`.

**Status transition sequence:**

```
CREATED
  ↓  (orchestrator starts, loads bug context)
CONTEXT_LOADING
  ↓  (all 3 agents launched)
INVESTIGATING
  ↓  (all 3 asyncio.gather results received, hypotheses persisted)
VERIFYING          ← CP-06 ends here; CP-07 picks up from VERIFYING
```

**Interface:**

```python
# app/orchestrator.py

async def run_investigation(
    investigation_id: str,
    session_factory: async_sessionmaker,
) -> None:
    """
    Full investigation pipeline for one Investigation record.

    Designed to be spawned as a background task:
        asyncio.create_task(run_investigation(inv_id, AsyncSessionLocal))

    Status flow: CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING
    On failure:  any status → FAILED
    """
```

**Agent exception handling:**

`asyncio.gather(return_exceptions=True)` means a crashing agent returns an `Exception` object rather than raising. The orchestrator must:
- Check each result: if `isinstance(result, Exception)`, create a `BLOCKED` hypothesis with the error message as `reasoning_summary`.
- Still persist the blocking hypothesis — the Arbiter (CP-08) needs all 3 slots filled.
- Only set investigation to `FAILED` if all 3 agents fail.

**Resource cap enforcement:**
- `settings.max_hypotheses = 3` — hard cap; orchestrator never launches more than 3 agents.
- Worktree creation failures should be caught per-hypothesis and not abort sibling agents.

**DB write pattern:**

The orchestrator uses its own session (not a request-scoped one) since it runs in a background task. Use `AsyncSessionLocal()` directly:

```python
async with session_factory() as session:
    investigation = await session.get(Investigation, investigation_id)
    investigation.status = InvestigationStatus.INVESTIGATING
    await session.commit()
```

Write to DB immediately after each status transition — don't batch status updates.

**Persisting agent results:**

For each `AgentResult`:
```python
# Create Hypothesis ORM row from AgentResult.hypothesis schema
hyp_row = Hypothesis(
    investigation_id=investigation_id,
    agent_type=result.hypothesis.agent_type,
    summary=result.hypothesis.summary,
    reasoning_summary=result.hypothesis.reasoning_summary,
    candidate_fix=result.hypothesis.candidate_fix,
    suspected_files=result.hypothesis.suspected_files,
    reproduction_plan=result.hypothesis.reproduction_plan,
    confidence=result.hypothesis.confidence,
    status=HypothesisStatus.PROPOSED,
)
session.add(hyp_row)
await session.flush()  # get hyp_row.id before writing children

# Create Experiment rows (one per ExperimentRecord)
for exp in result.experiments:
    session.add(Experiment(
        hypothesis_id=hyp_row.id,
        command=" ".join(exp.command),
        working_directory=exp.cwd,
        exit_code=exp.exit_code,
        stdout=exp.stdout,
        stderr=exp.stderr,
        duration_ms=exp.duration_ms,
        timed_out=exp.timed_out,
    ))

# Create AgentEvent rows
for event in result.events:
    session.add(AgentEvent(
        investigation_id=investigation_id,
        agent=event.agent,
        action=event.action,
        target=event.target,
        payload=event.payload,
    ))

await session.commit()
```

---

### Task 4 — Investigations Router (`app/routers/investigations.py`)

**Endpoints for CP-06:**

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/investigations` | Create + trigger background task |
| `GET` | `/api/investigations` | List all (paginated, limit/offset) |
| `GET` | `/api/investigations/{id}` | Full investigation detail |
| `GET` | `/api/investigations/{id}/hypotheses` | All hypotheses for investigation |

The remaining endpoints (`/approve`, `/reject`, `/pull-request`, `/events`) are stubbed as `501 Not Implemented` — they are completed in CP-09.

**Request / Response schemas** (define in `app/schemas/investigation.py`, new file):

```python
class InvestigationCreate(BaseModel):
    jira_issue_id: str
    repository: str
    base_branch: str = "main"
    # Inline bug context fields (avoids nested JSON in the request body)
    symptom: str
    expected: str
    actual: str
    affected_area: str
    error_type: str          # "deterministic" | "intermittent" | "regression"
    known_evidence: list[str] = []
    stack_trace: str | None = None

class InvestigationResponse(BaseModel):
    id: str
    external_issue_id: str
    repository: str
    base_branch: str
    status: str
    bug_context: dict | None
    created_at: datetime
    updated_at: datetime

class HypothesisResponse(BaseModel):
    id: str
    investigation_id: str
    agent_type: str
    summary: str
    reasoning_summary: str
    candidate_fix: str
    suspected_files: list[str]
    reproduction_plan: list[str]
    confidence: str
    status: str
    created_at: datetime
```

**`POST /api/investigations` logic:**

```python
@router.post("/api/investigations", response_model=InvestigationResponse, status_code=201)
async def create_investigation(
    body: InvestigationCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> InvestigationResponse:
    # 1. Build BugContext from request body
    bug_context = BugContext(
        issue_id=body.jira_issue_id,
        symptom=body.symptom,
        ...
    )
    # 2. Create Investigation ORM row
    investigation = Investigation(
        external_issue_id=body.jira_issue_id,
        repository=body.repository,
        base_branch=body.base_branch,
        status=InvestigationStatus.CREATED,
        bug_context=bug_context.model_dump(),
    )
    session.add(investigation)
    await session.commit()
    await session.refresh(investigation)

    # 3. Fire background task — DO NOT await
    background_tasks.add_task(
        run_investigation,
        investigation.id,
        AsyncSessionLocal,
    )

    return InvestigationResponse.model_validate(investigation)
```

**Important:** Use FastAPI's `BackgroundTasks` (not `asyncio.create_task`) for the background trigger. `BackgroundTasks` is the idiomatic FastAPI pattern and works correctly with the test client.

**`GET /api/investigations/{id}/hypotheses`:**

```python
@router.get("/api/investigations/{id}/hypotheses", response_model=list[HypothesisResponse])
async def list_hypotheses(id: str, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(HypothesisORM).where(HypothesisORM.investigation_id == id)
    )
    return [HypothesisResponse.model_validate(h) for h in result.scalars().all()]
```

**Mount in `main.py`:**

```python
from app.routers.investigations import router as investigations_router
app.include_router(investigations_router)
```

---

### Task 5 — DB Session Fixture (`tests/conftest.py`)

Tests for the orchestrator need a real (SQLite in-memory) database. Add to `conftest.py`:

```python
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.db.base import Base

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()

@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    AsyncTestSession = async_sessionmaker(db_engine, expire_on_commit=False)
    async with AsyncTestSession() as session:
        yield session

@pytest_asyncio.fixture
def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)
```

---

### Task 6 — `GitHistoryAgent` Tests (`tests/test_git_history_agent.py`)

Mirror the pattern in `test_code_path_agent.py`. Use the same `_emit_response()` / `_tool_then_emit()` helpers (extract to a shared test utility or duplicate the helpers).

**Tests to include:**
- `test_make_git_history_agent_returns_correct_types` — assert returns `(OpenAIAgentModel, list)`.
- `test_git_history_agent_tool_names` — tools are `{"git_log", "git_blame", "git_diff", "read_file"}`.
- `test_git_history_agent_tools_have_schemas` — each tool has `__tool_schema__`.
- `test_git_history_agent_returns_valid_result` — mock `_chat`, assert `AgentResult` with `agent_type == "git_history"`.
- `test_run_git_history_agent_returns_agent_result` — convenience function test.

---

### Task 7 — `TestBehaviorAgent` Tests (`tests/test_test_behavior_agent.py`)

Same pattern.

**Tests to include:**
- `test_make_test_behavior_agent_returns_correct_types`
- `test_test_behavior_agent_tool_names` — tools are `{"run_test", "search_code", "read_file"}`.
- `test_test_behavior_agent_tools_have_schemas`
- `test_test_behavior_agent_returns_valid_result`
- `test_run_test_behavior_agent_returns_agent_result`

---

### Task 8 — Orchestrator Tests (`tests/test_orchestrator.py`)

These are the core CP-06 tests. All LLM calls are mocked via `patch.object(OpenAIAgentModel, "_chat")`.

**Test: all 3 hypotheses persisted**

```python
@pytest.mark.asyncio
async def test_run_investigation_persists_three_hypotheses(
    db_engine, session_factory, tmp_path
):
    # 1. Seed an Investigation row
    # 2. Mock OpenAIAgentModel._chat to emit immediately for any agent
    # 3. Call run_investigation(investigation_id, session_factory)
    # 4. Query DB: assert len(hypotheses) == 3
    # 5. Assert agent_type values are {"code_path", "git_history", "test_behavior"}
```

**Test: status transitions are correct**

```python
@pytest.mark.asyncio
async def test_run_investigation_status_transitions(db_engine, session_factory, tmp_path):
    # After run_investigation completes:
    # assert investigation.status == InvestigationStatus.VERIFYING
```

**Test: partial failure (1 agent crashes)**

```python
@pytest.mark.asyncio
async def test_run_investigation_partial_agent_failure(db_engine, session_factory, tmp_path):
    # Mock: code_path raises Exception, git_history and test_behavior succeed
    # Assert: 3 hypotheses persisted
    # Assert: failed agent hypothesis has status BLOCKED
    # Assert: investigation.status == InvestigationStatus.VERIFYING (not FAILED)
    #         because at least one agent succeeded
```

**Test: all agents fail → investigation FAILED**

```python
@pytest.mark.asyncio
async def test_run_investigation_all_agents_fail_sets_failed_status(
    db_engine, session_factory, tmp_path
):
    # Mock: all 3 agents raise Exception
    # Assert: investigation.status == InvestigationStatus.FAILED
```

**Test: experiment and event rows are persisted**

```python
@pytest.mark.asyncio
async def test_run_investigation_persists_experiments_and_events(
    db_engine, session_factory, tmp_path
):
    # Mock: emit a hypothesis that also carries 1 ExperimentRecord + 1 AgentEvent
    # Assert: Experiment row exists in DB linked to the hypothesis
    # Assert: AgentEvent row exists in DB linked to the investigation
```

**Test: hypothesis count cap**

The orchestrator must never exceed 3 agents. This is implicitly enforced by launching exactly 3; test that the resulting DB hypothesis count is exactly 3 (not more, not fewer on success).

---

### Task 9 — API Integration Tests (`tests/test_api_investigations.py`)

Use the `httpx.AsyncClient` fixture from `conftest.py` with an in-memory DB (override `get_session` dependency).

**Tests:**

- `test_create_investigation_returns_201` — `POST /api/investigations` returns 201 with `id` and `status = "CREATED"`.
- `test_create_investigation_missing_fields_returns_422` — missing required fields returns 422.
- `test_get_investigation_not_found_returns_404` — `GET /api/investigations/nonexistent` returns 404.
- `test_list_investigations_empty` — `GET /api/investigations` returns `[]` on empty DB.
- `test_list_investigations_returns_created` — after POST, GET list returns it.
- `test_get_investigation_detail` — `GET /api/investigations/{id}` returns correct fields.
- `test_list_hypotheses_empty` — `GET /api/investigations/{id}/hypotheses` returns `[]` before agents run.
- `test_list_hypotheses_after_background_task` — after seeding 3 `Hypothesis` rows directly, endpoint returns all 3.

**DB override pattern for tests (FastAPI dependency override):**

```python
@pytest_asyncio.fixture
async def client_with_db(db_engine):
    from app.db.session import get_session
    from app.main import app

    AsyncTestSession = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_session():
        async with AsyncTestSession() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
```

---

## File-by-File Specification

### `backend/app/agents/git_history_agent.py`

```
_SYSTEM_PROMPT      — git-history investigator persona, references git_log/blame/diff tools
_wrap_git_log()     — wraps app.tools.git_tools.git_log
_wrap_git_blame()   — wraps app.tools.git_tools.git_blame
_wrap_git_diff()    — already exists in code_path_agent, can be duplicated here
_wrap_read_file()   — wraps app.tools.code_tools.read_file
make_git_history_agent() → tuple[AgentModel, list[Callable]]
run_git_history_agent(context) → AgentResult
```

### `backend/app/agents/test_behavior_agent.py`

```
_SYSTEM_PROMPT       — test-behavior investigator persona, references run_test/search/read tools
_wrap_run_test()     — wraps app.tools.test_runner.run_test
_wrap_search_code()  — same pattern as in code_path_agent (duplicate wrapper)
_wrap_read_file()    — same pattern
make_test_behavior_agent() → tuple[AgentModel, list[Callable]]
run_test_behavior_agent(context) → AgentResult
```

### `backend/app/orchestrator.py`

```
_persist_hypothesis(session, investigation_id, result_or_exception, agent_type) → str  # returns hypothesis id
_set_status(session, investigation_id, status) → None

async run_investigation(investigation_id, session_factory) → None
  1. _set_status → CONTEXT_LOADING
  2. Load investigation from DB; parse bug_context → BugContext
  3. Create WorktreeManager(base_dir=tmp dir per investigation)
  4. Create 3 worktrees: make_branch_name(issue_id, 1/2/3)
  5. Build 3 AgentContexts (one per worktree)
  6. _set_status → INVESTIGATING
  7. results = await asyncio.gather(
         run_code_path_agent(ctx1),
         run_git_history_agent(ctx2),
         run_test_behavior_agent(ctx3),
         return_exceptions=True,
     )
  8. For each result: _persist_hypothesis(...)
  9. If all failed: _set_status → FAILED; return
 10. _set_status → VERIFYING
 11. Cleanup: worktree_manager.destroy_all()
```

### `backend/app/schemas/investigation.py`  *(new)*

```
InvestigationCreate   — request body for POST /api/investigations
InvestigationResponse — response model
HypothesisResponse    — response model for hypothesis list
```

### `backend/app/routers/investigations.py`  *(new)*

```
POST  /api/investigations         → create_investigation
GET   /api/investigations         → list_investigations  (limit/offset query params)
GET   /api/investigations/{id}    → get_investigation
GET   /api/investigations/{id}/hypotheses → list_hypotheses
POST  /api/investigations/{id}/approve        → 501 stub
POST  /api/investigations/{id}/reject         → 501 stub
POST  /api/investigations/{id}/pull-request   → 501 stub
GET   /api/investigations/{id}/events         → 501 stub
```

---

## Acceptance Criteria

| # | Criterion | How verified |
|---|---|---|
| 1 | `POST /api/investigations` returns `201` with `id` and `status = "CREATED"` immediately | `test_create_investigation_returns_201` |
| 2 | Background task persists exactly 3 `Hypothesis` rows in DB | `test_run_investigation_persists_three_hypotheses` |
| 3 | All 3 hypotheses have distinct `agent_type` values (`code_path`, `git_history`, `test_behavior`) | `test_run_investigation_persists_three_hypotheses` |
| 4 | Investigation status transitions: `CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING` | `test_run_investigation_status_transitions` |
| 5 | One agent crashing leaves investigation `VERIFYING` (not `FAILED`); crashed agent's hypothesis has status `BLOCKED` | `test_run_investigation_partial_agent_failure` |
| 6 | All 3 agents crashing sets investigation status to `FAILED` | `test_run_investigation_all_agents_fail_sets_failed_status` |
| 7 | `ExperimentRecord` and `AgentEvent` from agent results are persisted to DB | `test_run_investigation_persists_experiments_and_events` |
| 8 | `GET /api/investigations/{id}/hypotheses` returns all 3 hypotheses | `test_list_hypotheses_after_background_task` |
| 9 | `POST /api/investigations` missing required field → `422` | `test_create_investigation_missing_fields_returns_422` |
| 10 | `GET /api/investigations/{unknown}` → `404` | `test_get_investigation_not_found_returns_404` |
| 11 | Orchestrator respects `max_hypotheses = 3` (never launches more) | Implicit — exactly 3 agents hardcoded |
| 12 | `pytest backend/` all green | CI |
| 13 | `ruff check .` no errors | CI |

---

## Cross-Cutting Constraints Enforced in This CP

| Constraint | AGENTS.md source | Enforcement in CP-06 |
|---|---|---|
| `asyncio.gather()` for parallelism | AGENTS.md §parallelism | `orchestrator.py` uses `asyncio.gather()` exclusively |
| No LangGraph / Prefect | AGENTS.md §parallelism | No new workflow framework imports anywhere |
| Agents use tool layer, not raw subprocess | AGENTS.md §shell | Agents receive tool callables from `make_*_agent()` factories |
| Credentials never in prompts | AGENTS.md §credentials | `AgentContext` has no tokens; `OpenAIAgentModel._chat` injects key from `settings` |
| `confidence` is informational only | AGENTS.md §arbiter | `Hypothesis.confidence` stored but orchestrator never branches on it |
| Max 3 hypotheses per investigation | AGENTS.md §resource | Orchestrator hardcodes 3-agent `asyncio.gather` |
| Pre-fix regression test must FAIL | AGENTS.md §verification | Not applicable in CP-06 (CP-07); orchestrator ends at `VERIFYING` |
| PR requires APPROVED status | AGENTS.md §pr | PR endpoint stubbed `501`; gate enforced in CP-09 |

---

## Build Order Within CP-06

The tasks have a partial dependency order. Suggested implementation sequence:

```
1. app/schemas/investigation.py           (no deps beyond existing schemas)
2. app/agents/git_history_agent.py        (depends on tools/git_tools.py — already exists)
3. app/agents/test_behavior_agent.py      (depends on tools/test_runner.py — already exists)
4. tests/test_git_history_agent.py        (can be written in parallel with step 2)
5. tests/test_test_behavior_agent.py      (can be written in parallel with step 3)
6. app/orchestrator.py                    (depends on steps 2+3; uses WorktreeManager + DB)
7. tests/conftest.py  (add DB fixtures)   (needed before orchestrator + API tests)
8. tests/test_orchestrator.py             (depends on steps 6+7)
9. app/routers/investigations.py          (depends on orchestrator + schemas)
10. app/main.py  (mount router)           (depends on step 9)
11. tests/test_api_investigations.py      (depends on steps 9+10)
12. ruff / pytest — verify all green
```

---

## Open Questions / Decisions for Implementation

1. **Worktree base dir in tests:** Orchestrator creates worktrees on disk. Tests that call `run_investigation` directly need a real (or mocked) git repo. Recommend: mock the `WorktreeManager.create` and `WorktreeManager.destroy_all` calls in unit tests; reserve real-worktree tests for the CP-04 layer. The orchestrator tests should mock worktree creation to avoid needing a real git repo.

2. **`BackgroundTasks` vs `asyncio.create_task`:** FastAPI's `BackgroundTasks` is used in the router for testability with `TestClient`. In production, `asyncio.create_task` would also work, but `BackgroundTasks` integrates better with the ASGI lifecycle. Use `BackgroundTasks` here; note that `BackgroundTasks` runs the task *after* the response is sent (not concurrently during the request), which is correct behaviour.

3. **Session isolation for background task:** The orchestrator must create its own DB sessions via `session_factory` (not the request-scoped `get_session`). Pass `AsyncSessionLocal` as the `session_factory` argument when firing from the router.

4. **Worktree cleanup on failure:** If the orchestrator raises mid-execution (after creating some worktrees), `destroy_all()` must run in a `finally` block to prevent directory leaks.

5. **`BugContext` reconstruction:** The `Investigation` DB row stores `bug_context` as a JSON blob. The orchestrator reads it back with `BugContext(**investigation.bug_context)`.
