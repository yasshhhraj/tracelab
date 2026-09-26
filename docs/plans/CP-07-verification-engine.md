# CP-07 — Verification Engine (Fail-Before / Pass-After)

> **Principle:** Every checkpoint leaves the system in a tested, deployable, runnable state.  
> **Builds on:** CP-01–CP-06 (scaffold, DB, tool layer, worktree manager, agents, orchestrator).  
> **Constraint (AGENTS.md):** Pre-fix regression test **must** FAIL before a hypothesis can be VERIFIED. Flaky detection requires min 20 runs. Max 2 patch attempts per hypothesis. Credentials never in prompts.

---

## Goal

For each `PROPOSED` hypothesis produced by the investigation agents, the Verification Engine runs a structured 7-step proof: reproduce the bug, generate a regression test, confirm it fails before the patch, apply the patch, confirm it passes after, run existing tests to confirm no regressions. The outcome is one of `VERIFIED | REJECTED | INCONCLUSIVE | BLOCKED`. All evidence is persisted to the `test_evidence` and `experiments` tables. The orchestrator is extended to call the engine for every hypothesis after investigation completes.

---

## Deliverable State at CP-07 Completion

- Given a seeded deterministic-bug repo, the engine marks the correct hypothesis `VERIFIED` and the wrong-file hypothesis `REJECTED`.
- Pre-fix PASS (test already passing before the fix is applied) → `INCONCLUSIVE`, not `VERIFIED`.
- All `TestEvidence` and `Experiment` rows are persisted with correct result fields.
- `patch_attempts` counter on `Hypothesis` is incremented and capped at `settings.max_patch_attempts = 2`.
- Flaky bug handling: for `error_type == "intermittent"`, the engine repeats the regression test `flaky_default_runs` (50) times and uses `failures_before > 0 AND failures_after == 0` as the pass criterion.
- Investigation status transitions from `VERIFYING → ARBITRATING` after all hypotheses are processed.
- `pytest backend/` fully green, `ruff check .` clean.

---

## What Already Exists (from CP-01–CP-06)

| Asset | Location |
|---|---|
| `Hypothesis`, `TestEvidence`, `Experiment`, `Patch` ORM models | `backend/app/db/models.py` |
| `HypothesisStatus` constants (`PROPOSED`, `VERIFIED`, `REJECTED`, `INCONCLUSIVE`, `BLOCKED`) | `backend/app/db/models.py` |
| `InvestigationStatus.VERIFYING`, `ARBITRATING` | `backend/app/db/models.py` |
| `apply_patch(repo_path, diff)` → `CommandResult` | `backend/app/tools/git_tools.py` |
| `run_test(repo_path, test_path)` → `CommandResult` | `backend/app/tools/test_runner.py` |
| `write_test(root, file_path, content)` | `backend/app/tools/code_tools.py` |
| `read_file(root, file_path)` | `backend/app/tools/code_tools.py` |
| `search_code(root, pattern)` | `backend/app/tools/code_tools.py` |
| `OpenAIAgentModel` (agentic loop with tool calling) | `backend/app/agents/llm/openai_model.py` |
| `settings.max_patch_attempts`, `flaky_*_runs`, `max_command_timeout_seconds` | `backend/app/config.py` |
| `WorktreeManager` (one worktree per hypothesis, already created by orchestrator) | `backend/app/worktree/manager.py` |
| `run_investigation()` in `VERIFYING` state, ready for handoff | `backend/app/orchestrator.py` |
| In-memory DB fixtures (`db_engine`, `db_session`, `session_factory`) | `backend/tests/conftest.py` |

---

## New Files to Create

```
backend/
├── app/
│   └── verification/
│       ├── __init__.py
│       ├── engine.py                     # NEW — VerificationEngine.verify()
│       ├── regression_test_generator.py  # NEW — LLM-backed test writer
│       └── flaky_runner.py               # NEW — repeated-run logic
└── tests/
    ├── fixtures/
    │   └── demo_repo/                    # NEW — seeded Python package with a known bug
    │       ├── review_service.py         # buggy implementation
    │       ├── review_repository.py      # missing uniqueness guard
    │       ├── tests/
    │       │   └── test_review.py        # existing passing sequential tests
    │       └── pyproject.toml / setup.cfg (pytest marker)
    ├── test_verification_engine.py       # NEW — engine unit + fixture-repo tests
    ├── test_regression_test_generator.py # NEW — mocked LLM test writer
    └── test_flaky_runner.py              # NEW — flaky logic unit tests
```

**Modified files:**

```
backend/app/orchestrator.py              # extend run_investigation to call engine
```

---

## Detailed Task Breakdown

---

### Task 1 — `VerificationEngine` (`app/verification/engine.py`)

This is the core of CP-07. It takes a single hypothesis (ORM row), its worktree path, and the bug context, and runs the full 7-step proof.

#### Class interface

```python
# app/verification/engine.py

class VerificationEngine:
    def __init__(self, session_factory: async_sessionmaker) -> None: ...

    async def verify(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        bug_context: BugContext,
    ) -> HypothesisStatus:
        """
        Run the full verification proof for one hypothesis.

        Returns the final HypothesisStatus:
            VERIFIED     — all 4 conditions met
            REJECTED     — reproduction failed OR pre-fix test passed OR post-fix failed
            INCONCLUSIVE — pre-fix regression test unexpectedly passed (bug not reproduced)
            BLOCKED      — patch failed to apply after max_patch_attempts

        Persists TestEvidence and Experiment rows at each step.
        Updates hypothesis.status and hypothesis.patch_attempts in DB.
        """
```

#### Verification steps (internal)

```
Step 1  _reproduce(hypothesis, worktree_path, bug_context)
        → run the reproduction_plan commands via run_test
        → if no failure observed → status = REJECTED (cannot reproduce)
        → persist Experiment rows for each command

Step 2  _generate_regression_test(hypothesis, worktree_path, bug_context)
        → call RegressionTestGenerator.generate(...)
        → writes regression test file into worktree via write_test()
        → persist AgentEvent "regression_test_generated"

Step 3  _run_pre_fix(hypothesis, worktree_path, test_path)
        → run the regression test file BEFORE applying patch
        → result MUST be FAIL (per AGENTS.md core constraint)
        → if result is PASS → status = INCONCLUSIVE; abort
        → persist TestEvidence(pre_fix_result=...)

Step 4  _apply_patch(hypothesis, worktree_path)
        → call apply_patch(worktree_path, hypothesis.candidate_fix)
        → if fails: increment patch_attempts
        → if patch_attempts >= max_patch_attempts → status = BLOCKED; abort
        → persist Experiment row (git apply command)

Step 5  _run_post_fix(hypothesis, worktree_path, test_path)
        → run the regression test AFTER applying patch
        → result MUST be PASS
        → if FAIL → status = REJECTED
        → persist TestEvidence(post_fix_result=...)

Step 6  _run_existing_tests(hypothesis, worktree_path)
        → run the full existing test suite (run_test with no test_path)
        → result MUST be PASS
        → if FAIL → status = REJECTED (patch introduced a regression)
        → persist TestEvidence(existing_suite_result=...)

Step 7  (all conditions met) → status = VERIFIED
```

#### Flaky bug handling (intermittent `error_type`)

When `bug_context.error_type == "intermittent"`, steps 3 and 5 are replaced by repeated runs via `FlakyRunner`:

```python
# Pre-fix: run N times, record failures_before
# Post-fix: run N times, record failures_after
# VERIFIED iff failures_before > 0 AND failures_after == 0
# AGENTS.md: FLAKY_MIN_RUNS = 20 enforced (never fewer)
```

---

### Task 2 — `RegressionTestGenerator` (`app/verification/regression_test_generator.py`)

An LLM call that takes the hypothesis + relevant source code and produces a regression test file.

#### Interface

```python
class RegressionTestGenerator:
    def __init__(self) -> None: ...

    async def generate(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        bug_context: BugContext,
        candidate_fix: str,
        suspected_files: list[str],
        reproduction_plan: list[str],
    ) -> tuple[str, str]:
        """
        Generate a regression test via LLM.

        Returns:
            (test_file_path, test_content)
            test_file_path is relative to worktree_path.

        The LLM is instructed to:
        - Write a single pytest test that reproduces the described bug.
        - The test MUST FAIL on the original code.
        - The test MUST PASS after the candidate fix is applied.
        - Use only stdlib + any packages already present in the worktree.
        - Do NOT import the fix — test the existing code as-is.
        """
```

#### LLM prompt design

System prompt instructs the model to act as a test engineer writing a targeted regression test. The prompt includes:
- The bug symptom, expected vs actual
- The `reproduction_plan` steps
- The `candidate_fix` description (for context — not to be imported)
- The content of `suspected_files` (read via `read_file`)
- Instruction to use `emit_test` (a terminal tool call) to emit the test content

The model response is parsed from the `emit_test` tool call argument (same pattern as `emit_hypothesis` in `OpenAIAgentModel`).

#### Security constraint
API key is read from `settings.llm_api_key` inside `_chat` — never placed in the prompt or logged.

---

### Task 3 — `FlakyRunner` (`app/verification/flaky_runner.py`)

Handles the repeated-run loop for intermittent bugs.

#### Interface

```python
class FlakyRunResult(BaseModel):
    runs: int
    failures: int
    failure_rate: float   # failures / runs
    output_samples: list[str]  # last 3 stdout snippets (for evidence)

async def run_flaky(
    worktree_path: Path,
    test_path: str,
    n_runs: int,
) -> FlakyRunResult:
    """
    Run `test_path` exactly `n_runs` times, counting failures.

    n_runs is clamped to [FLAKY_MIN_RUNS, FLAKY_MAX_RUNS] (AGENTS.md constraint).
    Each run is a separate `run_test()` call.
    Runs are sequential (not concurrent) to avoid masking the race condition.
    """
```

#### AGENTS.md constraint enforcement

```python
FLAKY_MIN_RUNS = settings.flaky_min_runs   # 20
FLAKY_MAX_RUNS = settings.flaky_max_runs   # 100

n_runs = max(FLAKY_MIN_RUNS, min(n_runs, FLAKY_MAX_RUNS))
```

This must be an assertion-level enforcement, not just a clamp — raise `ValueError` if the caller passes fewer than `FLAKY_MIN_RUNS` in a way that bypasses the clamp (i.e., the configured minimum is the floor).

---

### Task 4 — Extend Orchestrator (`app/orchestrator.py`)

Add a `_run_verification` phase to `run_investigation` that runs after `INVESTIGATING` and before `ARBITRATING`.

#### Extended status flow

```
CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING
```

The orchestrator currently transitions to `VERIFYING` and stops. CP-07 extends it to:

1. Query all `PROPOSED` hypotheses for the investigation.
2. For each hypothesis that has a real worktree path and a non-empty `candidate_fix`, call `VerificationEngine.verify(hypothesis_id, worktree_path, bug_context)`.
3. Hypotheses with `BLOCKED` status (from crashed agents in CP-06) skip verification — they stay `BLOCKED`.
4. After all hypotheses are verified, transition investigation status to `ARBITRATING` (handoff to CP-08).
5. If `VerificationEngine.verify` itself raises, mark that hypothesis `BLOCKED` and continue with remaining hypotheses.

#### Verification is sequential (not concurrent)

Each hypothesis needs its own isolated worktree. Since worktrees were already created in CP-06 (one per agent), each `verify()` call operates in its own directory. However, because the engine applies a patch and runs the test suite (which can have global side effects), **verification runs sequentially** — not via `asyncio.gather`. This is intentional: concurrent test runs on the same machine can interfere with each other (port conflicts, file locks, etc.).

#### Worktree path availability

The orchestrator currently destroys worktrees in its `finally` block after the investigation phase. For CP-07, worktrees must **remain alive through the verification phase**. Adjust the cleanup point: destroy worktrees only after `_run_verification` completes.

The orchestrator needs to pass the worktree paths (created in the investigation phase) through to the verification phase. Use a `dict[str, Path]` mapping `agent_type → worktree_path` held in the `run_investigation` scope.

---

### Task 5 — Seeded Fixture Repository (`tests/fixtures/demo_repo/`)

A minimal self-contained Python package with a known idempotency race condition bug that the engine can reproduce and fix.

#### Bug description (mirrors PRD §25 hero demo)

`ReviewService.submit_review()` checks for an existing row with the same idempotency key, then inserts if none found. Under concurrent requests, two threads can both find no existing row and both insert — producing a duplicate.

```python
# review_service.py  (buggy)
def submit_review(self, idempotency_key: str, data: dict) -> int:
    existing = self.repo.find_by_key(idempotency_key)
    if existing:
        return existing["id"]
    return self.repo.insert(idempotency_key, data)  # race: no DB-level lock

# review_repository.py  (in-memory dict, no atomic check-and-set)
```

#### Fix description

Add an atomic check-and-set in `ReviewRepository.insert` using a threading lock, or rely on a `unique` constraint that raises `IntegrityError` on the second insert. The `candidate_fix` field in the test hypothesis points to this exact change.

#### Existing tests

`tests/test_review.py` has sequential tests that all pass before and after the fix. They do NOT test concurrent behaviour — that is the regression test the engine must generate.

#### What the fixture provides for tests

| Fixture item | Used by |
|---|---|
| A git-init'd repo with one commit | `VerificationEngine` (needs `git apply` to work) |
| `review_service.py` + `review_repository.py` | Hypothesis `suspected_files` |
| Existing passing tests at `tests/test_review.py` | Step 6 existing-suite check |
| A pre-written regression test file (`tests/test_concurrent_review.py`) | Supplied as the "generated" test in mocked tests |
| A correct unified diff patch | Supplied as `candidate_fix` in mocked hypothesis |

---

### Task 6 — Tests (`tests/test_verification_engine.py`)

The main tests for CP-07. Use the `db_engine`/`session_factory` fixtures from `conftest.py`.

#### Test: deterministic bug — correct hypothesis → VERIFIED

```python
async def test_verify_correct_hypothesis_is_verified(session_factory, demo_repo_path):
    # Seed hypothesis with:
    #   candidate_fix = the correct patch
    #   reproduction_plan = commands that produce a failure
    #   suspected_files = ["review_service.py"]
    # Mock RegressionTestGenerator.generate to return a pre-written test
    # that fails on the original code and passes after the patch
    # Assert: hypothesis.status == VERIFIED
    # Assert: TestEvidence has pre_fix_result=FAIL, post_fix_result=PASS
    # Assert: existing_suite_result=PASS
```

#### Test: wrong hypothesis → REJECTED (reproduction fails)

```python
async def test_verify_wrong_hypothesis_is_rejected(session_factory, demo_repo_path):
    # Seed hypothesis with:
    #   candidate_fix = a patch to the wrong file (review_repository.py with no real fix)
    #   reproduction_plan = commands that produce a failure
    # The post-fix run still fails because the real bug is untouched
    # Assert: hypothesis.status == REJECTED
```

#### Test: pre-fix PASS → INCONCLUSIVE (AGENTS.md core constraint)

```python
async def test_verify_inconclusive_when_pre_fix_passes(session_factory, demo_repo_path):
    # Seed a hypothesis where the regression test already passes on the original code
    # (i.e., the test is too weak or tests the wrong thing)
    # Assert: hypothesis.status == INCONCLUSIVE
    # Assert: NO patch was applied (engine aborted after pre-fix check)
```

#### Test: patch fails to apply → BLOCKED after max_patch_attempts

```python
async def test_verify_blocked_after_max_patch_attempts(session_factory, demo_repo_path):
    # Seed a hypothesis with an invalid diff (won't apply cleanly)
    # Assert: after max_patch_attempts attempts, status == BLOCKED
    # Assert: hypothesis.patch_attempts == settings.max_patch_attempts
```

#### Test: existing suite fails after patch → REJECTED

```python
async def test_verify_rejected_when_existing_tests_fail(session_factory, demo_repo_path):
    # The candidate_fix breaks an existing test (introduces a regression)
    # pre_fix = FAIL (correct), post_fix = PASS, existing = FAIL
    # Assert: status == REJECTED
```

#### Test: all TestEvidence rows persisted

```python
async def test_verify_persists_test_evidence(session_factory, demo_repo_path):
    # After a VERIFIED run, assert TestEvidence row exists with:
    #   pre_fix_result = "FAIL"
    #   post_fix_result = "PASS"
    #   existing_suite_result = "PASS"
```

#### Test: all Experiment rows persisted

```python
async def test_verify_persists_experiments(session_factory, demo_repo_path):
    # Assert Experiment rows exist for each command run (reproduce, pre, post, git apply)
```

---

### Task 7 — Tests (`tests/test_regression_test_generator.py`)

All LLM calls mocked.

- `test_generator_returns_test_path_and_content` — mock `_chat`, assert `(str, str)` returned.
- `test_generator_writes_file_to_worktree` — assert the file actually lands in the worktree.
- `test_generator_emit_test_call_parsed_correctly` — assert tool call args correctly extracted.
- `test_generator_api_key_not_in_prompt` — assert `settings.llm_api_key` does not appear in any message passed to `_chat`.

---

### Task 8 — Tests (`tests/test_flaky_runner.py`)

- `test_flaky_runner_clamps_below_min_runs` — n_runs < 20 → clamped to 20.
- `test_flaky_runner_clamps_above_max_runs` — n_runs > 100 → clamped to 100.
- `test_flaky_runner_counts_failures_correctly` — mock `run_test` to fail on the first 5 of 20 runs; assert `failures == 5`.
- `test_flaky_runner_all_pass_returns_zero_failures` — all runs pass.
- `test_flaky_runner_all_fail_returns_n_failures` — all runs fail.

---

### Task 9 — Orchestrator Integration Tests (extend `tests/test_orchestrator.py`)

Add tests that verify the orchestrator now proceeds to `ARBITRATING` after verification:

- `test_orchestrator_reaches_arbitrating_after_verification` — mock agents + mock `VerificationEngine.verify` to return `VERIFIED`; assert investigation reaches `ARBITRATING`.
- `test_orchestrator_skips_blocked_hypotheses_in_verification` — mock one agent crash (→ `BLOCKED`); mock `VerificationEngine.verify` for the other two; assert only 2 verify calls made.

---

## File-by-File Specification

### `backend/app/verification/__init__.py`

Empty — marks the package.

### `backend/app/verification/engine.py`

```
class VerificationEngine:
    __init__(session_factory)

    # public
    async verify(hypothesis_id, worktree_path, bug_context) → HypothesisStatus

    # private steps
    async _reproduce(hyp, worktree_path, bug_context) → bool
    async _generate_regression_test(hyp, worktree_path, bug_context) → tuple[str, str]
    async _run_pre_fix(hyp, worktree_path, test_path) → str   # "PASS"/"FAIL"/"ERROR"
    async _apply_patch(hyp, worktree_path) → bool             # True = applied cleanly
    async _run_post_fix(hyp, worktree_path, test_path) → str
    async _run_existing_tests(hyp, worktree_path) → str

    # flaky variant of pre/post (used when error_type == "intermittent")
    async _run_flaky_pre(hyp, worktree_path, test_path) → FlakyRunResult
    async _run_flaky_post(hyp, worktree_path, test_path) → FlakyRunResult

    # DB helpers
    async _load_hypothesis(hypothesis_id) → Hypothesis
    async _save_status(hypothesis_id, status) → None
    async _save_test_evidence(hypothesis_id, **fields) → None
    async _append_experiment(hypothesis_id, result, step_label) → None
```

**Important:** The engine loads the hypothesis from DB at the start of `verify()` and re-saves it at the end. It does NOT hold an open session across the full verification run (which may take minutes) — it opens short-lived sessions for each DB write.

### `backend/app/verification/regression_test_generator.py`

```
class RegressionTestGenerator:
    __init__()                # reads settings.llm_api_key internally

    async generate(
        hypothesis_id, worktree_path, bug_context,
        candidate_fix, suspected_files, reproduction_plan
    ) → tuple[str, str]       # (relative_test_path, test_content)

    # private
    async _chat(messages, tools) → dict     # same pattern as OpenAIAgentModel._chat
    def _build_prompt(bug_context, candidate_fix, suspected_files, file_contents) → str
```

`emit_test` terminal tool schema:

```python
_EMIT_TEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "emit_test",
        "description": "Emit the regression test file content.",
        "parameters": {
            "type": "object",
            "required": ["test_file_path", "test_content"],
            "properties": {
                "test_file_path": {
                    "type": "string",
                    "description": "Relative path for the new test file (e.g. 'tests/test_regression_foo.py').",
                },
                "test_content": {
                    "type": "string",
                    "description": "Full Python content of the regression test file.",
                },
            },
        },
    },
}
```

### `backend/app/verification/flaky_runner.py`

```
class FlakyRunResult(BaseModel):
    runs: int
    failures: int
    failure_rate: float
    output_samples: list[str]   # up to 3 stdout snippets from failing runs

async run_flaky(worktree_path, test_path, n_runs) → FlakyRunResult
    # n_runs clamped to [FLAKY_MIN_RUNS, FLAKY_MAX_RUNS]
    # sequential runs via run_test()
    # collects up to 3 output samples from failures
```

### `backend/app/orchestrator.py` (modified)

Add `_run_verification` function and call it from `run_investigation`:

```python
async def _run_verification(
    session_factory: async_sessionmaker,
    investigation_id: str,
    hypothesis_worktree_map: dict[str, Path],  # hypothesis_id → worktree_path
    bug_context: BugContext,
) -> None:
    """
    Run VerificationEngine.verify() for each non-BLOCKED hypothesis.
    Sequential — not concurrent (test runners can interfere).
    """
    engine = VerificationEngine(session_factory)
    # Load all PROPOSED hypotheses for this investigation
    # For each: call engine.verify(); catch exceptions → BLOCKED
    # After all: _set_status → ARBITRATING
```

The `run_investigation` function needs to:
1. Capture worktree paths keyed by hypothesis ORM id (not agent_type) after persisting the hypothesis rows.
2. Delay `worktree_manager.destroy_all()` to after `_run_verification` completes.
3. Call `_run_verification` between the `VERIFYING` status set and the `finally` cleanup.

---

## Acceptance Criteria

| # | Criterion | How verified |
|---|---|---|
| 1 | Correct hypothesis on seeded bug repo → `VERIFIED` | `test_verify_correct_hypothesis_is_verified` |
| 2 | Wrong-file hypothesis → `REJECTED` | `test_verify_wrong_hypothesis_is_rejected` |
| 3 | Pre-fix test passes before patch → `INCONCLUSIVE` (AGENTS.md core rule) | `test_verify_inconclusive_when_pre_fix_passes` |
| 4 | Invalid patch fails twice → `BLOCKED`; `patch_attempts == 2` | `test_verify_blocked_after_max_patch_attempts` |
| 5 | Patch breaks existing tests → `REJECTED` | `test_verify_rejected_when_existing_tests_fail` |
| 6 | `TestEvidence` row has `pre_fix=FAIL`, `post_fix=PASS`, `existing=PASS` for VERIFIED | `test_verify_persists_test_evidence` |
| 7 | `Experiment` rows persisted for each verification step | `test_verify_persists_experiments` |
| 8 | Flaky runner clamps to `[20, 100]` runs | `test_flaky_runner_clamps_*` |
| 9 | Orchestrator transitions to `ARBITRATING` after verification | `test_orchestrator_reaches_arbitrating_after_verification` |
| 10 | `BLOCKED` hypotheses are skipped during verification | `test_orchestrator_skips_blocked_hypotheses_in_verification` |
| 11 | `pytest backend/` all green | CI |
| 12 | `ruff check .` no errors | CI |

---

## Cross-Cutting Constraints Enforced in This CP

| Constraint | AGENTS.md source | Enforcement in CP-07 |
|---|---|---|
| Pre-fix regression test MUST FAIL | AGENTS.md §verification | Engine aborts with `INCONCLUSIVE` if pre-fix result is PASS |
| Max 2 patch attempts per hypothesis | AGENTS.md §resource | `patch_attempts` counter; `BLOCKED` after reaching `settings.max_patch_attempts` |
| Flaky detection: min 20 runs | AGENTS.md §flaky | `FlakyRunner` clamps `n_runs ≥ FLAKY_MIN_RUNS` |
| Credentials never in prompts | AGENTS.md §credentials | `RegressionTestGenerator._chat` reads key from `settings` only |
| `confidence` is informational only | AGENTS.md §arbiter | Engine never reads `hypothesis.confidence` |
| Max 120s per command | AGENTS.md §resource | `run_test` / `apply_patch` use `settings.max_command_timeout_seconds` |
| Agents use tool layer, not raw subprocess | AGENTS.md §shell | Engine uses `run_test`, `apply_patch`, `write_test` from tool layer |

---

## Build Order Within CP-07

```
1. app/verification/__init__.py               (trivial, no deps)
2. app/verification/flaky_runner.py           (depends on tools/test_runner.py)
3. tests/test_flaky_runner.py                 (parallel with step 2)
4. app/verification/regression_test_generator.py   (depends on config, tools/code_tools.py)
5. tests/test_regression_test_generator.py   (parallel with step 4)
6. tests/fixtures/demo_repo/                  (needed before engine tests)
7. app/verification/engine.py                 (depends on steps 2+4+6)
8. tests/test_verification_engine.py          (depends on steps 6+7)
9. app/orchestrator.py  (extend with _run_verification)
10. tests/test_orchestrator.py  (add ARBITRATING + skip-BLOCKED tests)
11. ruff / pytest — verify all green
```

---

## Open Questions / Decisions for Implementation

1. **Worktree lifetime across phases.** The orchestrator currently creates and destroys worktrees within `run_investigation`. For CP-07, worktrees must survive until after `_run_verification`. The design: remove `destroy_all()` from the investigation phase; pass a `worktree_path_map` (hypothesis_id → Path) from the investigation phase into `_run_verification`; destroy all in the same `finally` block after `_run_verification` returns.

2. **Hypothesis ID vs agent_type in worktree map.** The investigation phase creates worktrees keyed by `"h1"`, `"h2"`, `"h3"` (the `hypothesis_id` argument to `WorktreeManager.create`). After persisting the hypothesis ORM rows, the orchestrator must build a map from `hypothesis_orm_id → worktree_path` for the verification phase. This requires querying the DB for the newly-created hypothesis IDs, matched by `agent_type` order.

3. **Candidate fix format.** The `candidate_fix` field from the agent is either a unified diff string or prose description. The engine should first attempt `git apply --check` (dry run) to test if it's a valid diff. If the check fails, the engine should increment `patch_attempts` and mark the hypothesis `BLOCKED` after `max_patch_attempts`. If `candidate_fix` is prose (not a diff), the engine cannot apply it — treat as `INCONCLUSIVE` with a note.

4. **Regression test file naming.** The generator should use `tests/test_regression_{hypothesis_id[:8]}.py` as the default file path to avoid collision with existing test files.

5. **Reproduction step (Step 1).** "Reproduce the bug" means running one of the commands from `reproduction_plan`. For the verification engine, this is: run `run_test(worktree_path)` (full suite or a specific test matching the affected_area). A failure (exit code ≠ 0) counts as "reproduction successful". If all reproduction commands pass, the bug is not observable — mark `REJECTED` with reason "could not reproduce".

6. **Test isolation in the fixture repo.** The seeded demo repo in `tests/fixtures/demo_repo/` must be a proper git repo (with at least one commit) so `git apply` works. Use a pytest fixture (not a global directory) that copies the fixture into `tmp_path` and runs `git init` + initial commit — this guarantees test isolation even when multiple tests run in parallel.
