# CP-13 — Seeded Demo Benchmark and End-to-End Smoke Test

**Status:** Implemented and verified on 2026-09-27.  
**Goal:** Make one reproducible Python bug traverse TraceLab's real API, three-agent orchestrator, verification engine, arbiter, human-approval gate, and mocked draft-PR integration. Add a ten-case benchmark that measures the same evidence contract.  
**Boundary:** The demo and CI must use local temporary repositories and databases, scripted LLM responses, and a fake GitHub client. They must not use the user's Jira, GitHub, Loreforge checkout, credentials, or live PR path. [Live CP-12 PR validation](CP-12-live-validation-follow-up.md) remains deferred until after MVP deployment.

## 1. Current-state findings that shape the implementation

- `run_investigation()` already runs three agents with `asyncio.gather()`, verifies hypotheses sequentially, calls the arbiter, and reaches `WAITING_FOR_REVIEW`.
- `VerificationEngine` enforces reproduction, fail-before, pass-after, and existing-suite pass. It currently **does not create a `Patch` row**, although the PR route requires one. CP-13 must close this handoff for a verified winner.
- `_reproduce()` passes each `reproduction_plan` entry to `run_test()` as a pytest path or node ID. The seeded hypothesis must use an executable path, not prose steps.
- The generator is Python/pytest-specific, which suits this checkpoint. The Loreforge TypeScript support gap is outside this demo.
- `POST /api/investigations` passes the imported `AsyncSessionLocal` to its background task. An in-process test that overrides only FastAPI's `get_session` would split API and orchestrator writes across databases. The demo harness must bind **both** to the same disposable engine.
- `ASGITransport` may wait for FastAPI background tasks before returning the POST response. The E2E test must assert the final state without relying on observing intermediate states over HTTP; status-transition order can be asserted from controlled instrumentation or persisted events. A separate server smoke can test immediate-response timing if needed.
- The PR route now rejects missing/unverified patches and missing GitHub credentials. The demo must inject a fake token and mocked GitHub client **after** a human-approval API call. It must never restore the old placeholder-URL behavior.

## 2. Repository layout to add

```text
demo_repo/                         # checked-in source fixture, not a live git repo
  pyproject.toml                   # pytest testpaths = ["tests"]
  review_repository.py            # SQLite persistence; buggy lookup/insert race
  review_service.py               # review-run creation facade
  tests/test_review.py            # sequential tests, green before and after
  repro/test_concurrent_review.py  # explicit failing reproduction; outside testpaths
backend/tests/fixtures/cp13/
  hero_hypotheses.json            # scripted, typed agent outputs and expected outcomes
  hero_regression_test.py.txt     # generated-test response content, kept outside demo repo
  hero_fix.patch                  # known correct unified diff, kept outside demo repo
  benchmark_manifest.json        # ten case IDs, categories, expected causes and proof
scripts/benchmark_cases.py        # source and test definitions for nine additional cases
scripts/seed_demo.py              # copy fixture into a unique temp dir and seed git history
scripts/run_demo.py               # isolated DB + scripted models + API journey + report
scripts/run_benchmark.py          # ten-case verification/arbiter benchmark and JSON report
backend/tests/e2e/test_demo_flow.py
backend/tests/e2e/test_benchmark.py
```

Keep expected fixes and model scripts **outside** `demo_repo/`, so investigation tools cannot read the answer from the target checkout. Use temporary clones of the fixture; do not initialize Git or create worktrees in the checked-in source directory. Use fixed author identity and timestamps for stable git history. All temporary worktrees, branches, and SQLite files are removed on normal exit and failure.

## 3. Hero bug and deterministic proof

Model `ReviewRun(id, input_hash)` in a small SQLite-backed repository. The buggy path performs `SELECT` for an existing hash, then inserts a new row without a uniqueness guarantee. Two concurrent requests can both observe no row and insert duplicates. Sequential requests correctly return one row, so existing sequential tests pass before the fix.

The explicit reproduction test uses two worker threads and a barrier placed after their lookups. Each worker uses its own SQLite connection to a fresh temporary database. Both lookups finish before either insert, making the race repeatable rather than timing-dependent. The assertion requires one persisted row and one logical run ID; it fails on the buggy commit. The known fix adds a database uniqueness constraint and handles the losing insert by loading the winning row. It must keep sequential behavior intact.

Set the hero `BugContext.error_type` to `deterministic`: the fixture forces a concurrency interleaving every run. This avoids applying the 50-run flaky protocol to a deterministic smoke test. The separate flaky benchmark case uses `intermittent` and the configured repeated-run protocol.

Configure `demo_repo/pyproject.toml` so a plain `pytest` runs only `tests/`. The first reproduction step is the explicit node ID in `repro/test_concurrent_review.py`, which fails before the patch. The scripted regression generator writes a focused new test under `tests/`; it fails before the patch and passes afterward. The existing suite then runs with the new test included and passes. Before implementing the pipeline, verify these fixture properties directly in the seeded temporary repository.

## 4. Three hypotheses and real verification

Script the LLM transport at the `_chat` boundary, not the orchestrator or verifier. Keep real tool dispatch, worktree creation, test execution, patch application, DB writes, and arbiter eligibility checks. Use stable tool-call IDs and valid `emit_hypothesis`/`emit_test` payloads.

| Agent | Scripted hypothesis | Candidate patch | Expected verification |
|---|---|---|---|
| Code-path | Lookup/insert idempotency race | Correct unique-key and conflict-handling diff | `VERIFIED` |
| Git-history | Stale cache caused duplicate creation | Plausible, applicable cache-only change | `REJECTED`: regression still fails |
| Test-behavior | Input-hash normalization is wrong | Plausible, applicable normalization-only change | `REJECTED`: regression still fails |

All three may use the same explicit failing reproduction node, because the observed symptom is real; their patches differ. Decoy diffs must apply cleanly and fail for **causal** reasons, not syntax errors, invalid paths, or missing dependencies. Do not pre-set statuses in the database. The arbiter must select the code-path hypothesis from stored evidence even if a decoy declares higher model confidence.

## 5. Close the verified-patch handoff

Add a small persistence step to `VerificationEngine` after all four verification conditions pass:

1. Identify the worktree branch and gather the exact source-file diff plus the newly generated regression test. Stage only known changed source files and the generated test; reject unexpected changed files. The PR diff must include the test, not just the source fix.
2. Store a single `Patch` row keyed to the verified hypothesis, containing branch, portable unified diff, and changed-file list. Make retries idempotent by replacing/updating the same logical patch rather than creating duplicates.
3. Verify that `git apply --check` accepts the stored diff against a fresh copy of the base commit. Do this before considering the patch publishable.
4. Keep the patch in the database before the worktree is destroyed. Do not persist a patch for rejected, inconclusive, or blocked hypotheses.

The current GitHub clone path uses `git commit -am`, which omits new regression-test files. Update that path to stage the specific files represented by `Patch.diff` before committing, and test that the draft-PR branch would contain the generated test. The E2E demo mocks the GitHub network/push boundary, but must exercise the same patch data the real path consumes.

## 6. Isolated demo harness and API journey

`scripts/seed_demo.py` copies the fixture into a unique temporary directory, creates a deterministic `main` history with a plausible bug-introducing commit, and returns the local repo path. It must be repeatable without changing user repositories. The temp repo has a demo-only GitHub origin so the PR route can parse owner/repository; the fake GitHub client prevents any network or push to that origin.

`scripts/run_demo.py` creates a disposable SQLite database with the current schema and binds both API request sessions and the investigation background task to it. It temporarily replaces LLM calls with scripted responses, disables Jira, and substitutes a fake `GitHubClient` whose returned URL is clearly local/demo (for example `https://github.test/demo/review/pull/1`). It checks that no HTTP request or `git push` occurs. Restore all patched settings and dependencies in `finally` blocks.

Run the following public journey against the FastAPI app:

1. `POST /api/investigations` with issue `DEMO-421`, the temporary repository path, `baseBranch=main`, and the precise idempotency bug context.
2. Read the final investigation, hypotheses, experiments, test evidence, patch, and diagnosis. Assert one `VERIFIED`, two `REJECTED`, and the four evidence conditions on the winner. Assert the diagnosis points to that winner and reaches `WAITING_FOR_REVIEW`.
3. Call `POST /pull-request` **before** approval and assert `409`; the fake GitHub client has not been called.
4. Call `POST /approve`, then `POST /pull-request`. Assert the fake client receives the winning diff, base branch, title, and evidence-rich body; assert the URL is returned and saved on `Patch`. Keep a separate GitHub-client unit assertion that its real API payload sets `draft=True`.
5. Call `POST /pull-request` again and assert the cached URL is returned with no second PR creation.

`make demo` runs this script using the backend virtual environment, prints the diagnosis, proof summary, timings, and mock PR URL, and exits nonzero on any unmet assertion. It requires no configured LLM, database server, Jira, or GitHub key. Keep the existing `make test`, `make lint`, `make migrate`, and `make dev` targets; add only the missing demo target and any documented prerequisites.

## 7. Ten-case benchmark

Define exactly ten independent seeded cases in `benchmark_manifest.json`: three logic, two data/persistence, two concurrency (including the hero), two regression, and one flaky. The manifest records IDs, categories, expected causes, and source files; `scripts/benchmark_cases.py` holds the buggy/fixed sources and tests used to derive patches and reproduction nodes. Each runs from a fresh temp checkout and database; no case can inherit another's branch, DB state, or generated test.

Execute the benchmark with the same real verification and arbiter path and scripted model transport. Report both per-case evidence and aggregate metrics:

| Metric | Definition | CP-13 target |
|---|---|---|
| Verified resolution rate | Correct verified winner / all 10 cases | At least 8/10 |
| Time to verified diagnosis | Monotonic time from investigation start to `WAITING_FOR_REVIEW`, reported per case and aggregate | Target ≤ 3 minutes per case; report misses |
| Hypothesis coverage | Distinct code/git/test hypotheses persisted per case | Exactly 3/3 |
| Regression-proof rate | VERIFIED cases with fail-before, pass-after, reproduction, and existing-suite pass / VERIFIED cases | 100% |
| Human acceptance rate | Approved diagnoses / diagnoses actually reviewed by humans | `not measured` in scripted CI |

For the flaky case, use the configured default 50 runs before and after; never reduce below 20 or declare fixed after one pass. Save run counts and failure counts. Treat timeouts, infrastructure errors, missing evidence, and wrong winners as failures in the ten-case denominator; never silently exclude them. Write a machine-readable JSON report and a concise console table. Redact tokens and trim oversized command output. A scripted-model benchmark measures **pipeline correctness**, not real LLM diagnostic accuracy; label it accordingly and keep any later live-model results separate.

## 8. Implementation sequence

1. **Fixture contract:** Build the hero source, sequential suite, explicit failing reproduction, correct fix, and two applicable decoy diffs. Prove fail-before/pass-after in a temporary seeded Git checkout.
2. **Patch handoff:** Persist a complete verified `Patch` including the generated test, validate it applies to a fresh base, and make GitHub staging include new files. Add focused unit/integration tests.
3. **Scripted model transport:** Add deterministic responses for the three agents, regression generator, and arbiter. Keep the real orchestration, tool layer, verifier, and evidence checks.
4. **E2E journey:** Add the isolated DB/repo harness and API smoke test, including approval gate, fake draft PR, persistence, and idempotency.
5. **CLI:** Add `make demo` and a README walkthrough with expected output and failure diagnostics.
6. **Benchmark:** Add the remaining nine cases, manifest validator, metrics report, and benchmark test. Run each case in isolation.
7. **Quality gate:** Run the E2E suite, complete backend suite, `make demo`, and Ruff checks. Resolve the repository's existing test-file Ruff findings if `make lint` is to serve as a whole-repo gate; do not hide them by excluding CP-13 tests.

## 9. Acceptance criteria

- `make demo` succeeds twice consecutively on a clean local checkout with no external credentials or services, and prints a verified diagnosis and mock draft-PR URL.
- The hero case has exactly one `VERIFIED` code-path hypothesis, two evidence-based `REJECTED` decoys, a selected diagnosis, a persisted portable `Patch` containing source fix **and** regression test, and all four verification conditions recorded.
- PR creation is blocked before approval, succeeds with the fake client after approval, persists the mock URL, and is idempotent. No real GitHub branch or PR is created.
- `backend/tests/e2e/` and the complete backend suite pass. The ten-case report contains every required category and metric, including explicit failures and flaky run counts. New/modified CP-13 code passes Ruff.
- The demo leaves the user's working tree, existing `output/` contents, local services, and external Jira/GitHub state untouched. No credential value appears in logs, prompts, reports, or fixtures.

## 10. Known boundaries after CP-13

This checkpoint proves the pipeline with deterministic Python fixtures and mocked external integrations. It does **not** validate real-model diagnostic quality, Loreforge/TypeScript support, production concurrency, or the deferred live GitHub draft-PR acceptance test. Track the latter in [CP-12 live validation follow-up](CP-12-live-validation-follow-up.md).

Implementation verification: `make demo` passed twice consecutively; `make test` passed 276 tests. The scripted benchmark resolved 10/10 cases with 10/10 hypothesis coverage and regression proof, with no case over three minutes; the flaky winner recorded 50 runs and 10 failures before the fix, then zero failures after it. `make lint` still reports 23 earlier Ruff findings in unrelated test files; all new and modified CP-13 Python files pass Ruff check and format validation.
