# CP-08 — Arbiter Agent + Diagnosis Report

> **Principle:** Every checkpoint leaves the system in a tested, deployable, runnable state.  
> **Builds on:** CP-01–CP-07 (scaffold, DB, tool layer, worktree manager, agents, orchestrator, verification engine).  
> **Constraint (AGENTS.md):** Arbiter must **never** select a winner by `confidence` field — evidence (reproduction + test proof) determines the winner. Credentials never in prompts.

---

## Goal

After the Verification Engine has processed all hypotheses (investigation is in `ARBITRATING`
status), the Arbiter Agent evaluates the evidence behind each hypothesis and produces a
structured `Diagnosis` record. The orchestrator then persists the `Diagnosis`, transitions the
investigation to `WAITING_FOR_REVIEW`, and the full record is surfaced through the existing
`GET /api/investigations/{id}` endpoint.

No human intervention is modelled in this checkpoint — CP-08 ends at `WAITING_FOR_REVIEW`.
The approve/reject routes and PR creation belong to CP-09 and CP-12.

---

## Deliverable State at CP-08 Completion

- Calling `run_investigation(...)` on the seeded demo scenario results in a persisted
  `Diagnosis` row with `selected_hypothesis_id` pointing to the VERIFIED hypothesis.
- When no hypothesis is VERIFIED, `selected_hypothesis_id = None` and `summary` states
  that no sufficient evidence was found.
- `risk`, `evidence`, `rejected_hypotheses`, `changed_files`, and `recommended_action`
  fields are populated correctly.
- Investigation status transitions: `ARBITRATING → WAITING_FOR_REVIEW`.
- `GET /api/investigations/{id}` response includes a `diagnosis` key with the embedded
  `Diagnosis` data.
- `pytest backend/` fully green, `ruff check .` clean.

---

## What Already Exists (from CP-01–CP-07)

| Asset | Location |
|---|---|
| `Diagnosis` ORM model (all columns) | `backend/app/db/models.py` |
| `InvestigationStatus.ARBITRATING`, `WAITING_FOR_REVIEW` | `backend/app/db/models.py` |
| `HypothesisStatus` constants | `backend/app/db/models.py` |
| `Hypothesis`, `TestEvidence`, `Experiment`, `Patch` ORM models | `backend/app/db/models.py` |
| `AgentType.ARBITER` constant | `backend/app/db/models.py` |
| `_set_status()` helper | `backend/app/orchestrator.py` |
| `_run_verification()` sets status to `ARBITRATING` | `backend/app/orchestrator.py` |
| `OpenAIAgentModel._chat()` pattern (Bearer auth, no key in prompts) | `backend/app/agents/llm/openai_model.py` |
| `settings.llm_api_key`, `settings.llm_model`, `settings.llm_base_url` | `backend/app/config.py` |
| In-memory DB fixtures (`db_engine`, `db_session`, `session_factory`) | `backend/tests/conftest.py` |
| `GET /api/investigations/{id}` route (currently returns investigation without diagnosis) | `backend/app/routers/investigations.py` |

---

## New Files to Create

```
backend/
├── app/
│   └── agents/
│       └── arbiter_agent.py          # NEW — ArbiterAgent + emit_diagnosis tool
└── tests/
    ├── test_arbiter_agent.py         # NEW — unit tests with mocked LLM
    └── test_orchestrator_cp08.py     # NEW — orchestrator integration tests for CP-08
```

**Modified files:**

```
backend/app/orchestrator.py           # extend: add _run_arbitration(), call after _run_verification
backend/app/routers/investigations.py # extend: embed diagnosis in GET /api/investigations/{id}
backend/app/schemas/investigation.py  # extend: add DiagnosisSchema + embed in InvestigationDetailResponse
```

---

## Detailed Task Breakdown

---

### Task 1 — `ArbiterAgent` (`app/agents/arbiter_agent.py`)

The Arbiter is a single LLM call — not an agentic loop. It receives a fully assembled
evidence summary and emits a `Diagnosis` in one shot via the `emit_diagnosis` terminal tool.

#### Class interface

```python
# app/agents/arbiter_agent.py

class ArbiterAgent:
    def __init__(self) -> None: ...   # reads settings.llm_api_key internally

    async def run(
        self,
        investigation_id: str,
        session_factory: async_sessionmaker,
    ) -> DiagnosisData:
        """
        Load all evidence for this investigation from DB, call the LLM once,
        parse the emit_diagnosis tool call, persist the Diagnosis row, and
        return the DiagnosisData.

        AGENTS.md constraint: confidence field must never influence selection.
        """
```

#### `DiagnosisData` — internal Pydantic model

```python
class RejectedHypothesisInfo(BaseModel):
    hypothesis_id: str
    agent_type: str
    summary: str
    rejection_reason: str

class DiagnosisData(BaseModel):
    selected_hypothesis_id: str | None   # None → all hypotheses failed
    summary: str
    verified_cause: str                  # empty string when no winner
    evidence: list[str]                  # bullet strings
    rejected_hypotheses: list[RejectedHypothesisInfo]
    changed_files: list[str]
    risk: Literal["low", "medium", "high", "unknown"]
    recommended_action: str
```

#### Evidence assembly (`_build_evidence_summary`)

Before calling the LLM the arbiter loads and assembles a text summary of all evidence:

```python
async def _build_evidence_summary(
    self,
    investigation_id: str,
    session_factory: async_sessionmaker,
) -> tuple[str, list[dict]]:
    """
    Returns:
        (formatted_text_summary, raw_hypothesis_dicts)

    Loads:
        - All Hypothesis rows for the investigation (status, summary,
          reasoning_summary, candidate_fix, suspected_files, agent_type).
        - For each hypothesis: its TestEvidence row(s) and Experiment count.

    AGENTS.md constraint: the summary must NOT include the confidence field
    in a position where the LLM might use it for ranking.
    """
```

The text summary format (fed to the LLM as the user message):

```
Investigation: {investigation_id}
Issue: {bug_context.issue_id}
Symptom: {bug_context.symptom}
Expected: {bug_context.expected}
Actual: {bug_context.actual}

=== Hypotheses ===

[H1] agent_type=code_path  status=VERIFIED
  Summary: ...
  Reasoning: ...
  Suspected files: review_service.py, review_repository.py
  Candidate fix: (diff or prose)
  Test evidence:
    pre_fix_result=FAIL  post_fix_result=PASS  existing_suite_result=PASS
  Experiments run: 5

[H2] agent_type=git_history  status=REJECTED
  Summary: ...
  Reasoning: ...
  Suspected files: review_service.py
  Test evidence:
    pre_fix_result=FAIL  post_fix_result=FAIL
  Experiments run: 3

[H3] agent_type=test_behavior  status=BLOCKED
  Summary: ...
  (no test evidence — agent failed)
  Experiments run: 0
```

**Important:** The word "confidence" must not appear in the assembled summary text anywhere.
The `confidence` column is loaded but silently dropped before building the prompt.

#### `emit_diagnosis` terminal tool schema

```python
_EMIT_DIAGNOSIS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_diagnosis",
        "description": "Emit the structured diagnosis report for this investigation.",
        "parameters": {
            "type": "object",
            "required": [
                "selected_hypothesis_id",
                "summary",
                "verified_cause",
                "evidence",
                "rejected_hypotheses",
                "changed_files",
                "risk",
                "recommended_action",
            ],
            "properties": {
                "selected_hypothesis_id": {
                    "type": ["string", "null"],
                    "description": (
                        "ID of the VERIFIED hypothesis, or null if none was verified."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "2-4 sentence diagnosis summary for the engineer.",
                },
                "verified_cause": {
                    "type": "string",
                    "description": (
                        "One-sentence root-cause statement. "
                        "Empty string if no hypothesis was verified."
                    ),
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Bullet-point evidence list, e.g. "
                        "'Bug reproduced with concurrent requests'."
                    ),
                },
                "rejected_hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["hypothesis_id", "agent_type", "summary", "rejection_reason"],
                        "properties": {
                            "hypothesis_id": {"type": "string"},
                            "agent_type": {"type": "string"},
                            "summary": {"type": "string"},
                            "rejection_reason": {"type": "string"},
                        },
                    },
                    "description": "All non-VERIFIED hypotheses with rejection reasons.",
                },
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files that the winning patch modifies.",
                },
                "risk": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "unknown"],
                    "description": "Regression risk level of the proposed fix.",
                },
                "recommended_action": {
                    "type": "string",
                    "description": (
                        "One-sentence recommendation, e.g. "
                        "'Review candidate patch and create draft PR.'"
                    ),
                },
            },
        },
    },
}
```

#### System prompt

```
You are an evidence-based arbiter for TraceLab, an AI debugging platform.

Your job:
1. Review all hypotheses and their test evidence.
2. Select the hypothesis with the strongest reproducible evidence.
3. The ONLY valid winner is a hypothesis with status=VERIFIED —
   meaning: reproduction succeeded, regression test FAILED before the patch,
   PASSED after, and existing tests also PASSED.
4. If no hypothesis is VERIFIED, set selected_hypothesis_id to null.
5. Do NOT rank by confidence scores — rank by concrete test evidence only.
6. Call emit_diagnosis once with your structured assessment.

Selection criteria in priority order:
  1. Successful reproduction
  2. Clear causal explanation  
  3. Fail-before / pass-after regression evidence
  4. Minimal code change (fewer files changed = lower risk)
  5. Low regression risk (existing suite passing)
```

#### LLM call pattern

Single call (not a loop) — no tool-use iteration needed:

```python
async def _chat(self, messages, tools) -> dict:
    # Same pattern as RegressionTestGenerator._chat and OpenAIAgentModel._chat
    # Bearer token from settings.llm_api_key — never in messages
    ...
```

After the single call, parse `emit_diagnosis` from `tool_calls`. If the model does not
call `emit_diagnosis` in one shot, retry once with a follow-up message:
`"Please call emit_diagnosis now with your assessment."`. If still no call, fall back to a
no-winner diagnosis with `summary = "Arbiter could not produce a structured diagnosis."`

#### Fallback: all hypotheses without VERIFIED status

```python
# Pre-check before calling LLM:
# If there are no VERIFIED hypotheses at all, skip the LLM call entirely.
# Emit a deterministic "no winner" diagnosis:
DiagnosisData(
    selected_hypothesis_id=None,
    summary="No hypothesis was sufficiently supported by evidence.",
    verified_cause="",
    evidence=[],
    rejected_hypotheses=[...],  # all hypotheses with their status as rejection reason
    changed_files=[],
    risk="unknown",
    recommended_action=(
        "Review the investigation logs. Consider supplying more context "
        "or re-running the investigation."
    ),
)
```

This keeps the deterministic path fast and avoids an LLM call when the outcome is certain.

#### DB persistence

After `DiagnosisData` is produced (from LLM or fallback), persist it:

```python
async def _persist_diagnosis(
    self,
    investigation_id: str,
    data: DiagnosisData,
    session_factory: async_sessionmaker,
) -> Diagnosis:
    """Insert a Diagnosis row. Upsert-safe: if one already exists, update it."""
```

---

### Task 2 — Extend Orchestrator (`app/orchestrator.py`)

Add `_run_arbitration` and call it from `run_investigation` after `_run_verification`.

#### Extended status flow

```
CREATED → CONTEXT_LOADING → INVESTIGATING → VERIFYING → ARBITRATING → WAITING_FOR_REVIEW
```

#### New function

```python
async def _run_arbitration(
    session_factory: async_sessionmaker,
    investigation_id: str,
    bug_context: BugContext,
) -> None:
    """
    Call ArbiterAgent.run() to produce and persist a Diagnosis.
    After completion, transition investigation to WAITING_FOR_REVIEW.

    Exceptions from ArbiterAgent are caught: on failure the investigation
    moves to FAILED (not left in ARBITRATING).
    """
    try:
        agent = ArbiterAgent()
        await agent.run(
            investigation_id=investigation_id,
            session_factory=session_factory,
        )
        await _set_status(session_factory, investigation_id, InvestigationStatus.WAITING_FOR_REVIEW)
    except Exception as exc:
        logger.exception(
            "ArbiterAgent raised for investigation %s: %s", investigation_id, exc
        )
        await _set_status(session_factory, investigation_id, InvestigationStatus.FAILED)
```

#### Call site in `run_investigation`

After the existing `await _run_verification(...)` call, add:

```python
# ── ARBITRATING → WAITING_FOR_REVIEW ─────────────────────────────────────
await _run_arbitration(
    session_factory=session_factory,
    investigation_id=investigation_id,
    bug_context=bug_context,
)
```

The investigation already has status `ARBITRATING` at this point (set by `_run_verification`).
`_run_arbitration` is responsible for moving it to `WAITING_FOR_REVIEW` (or `FAILED`).

---

### Task 3 — Embed Diagnosis in API Response

#### `app/schemas/investigation.py` — add `DiagnosisSchema`

```python
class RejectedHypothesisSchema(BaseModel):
    hypothesis_id: str
    agent_type: str
    summary: str
    rejection_reason: str

class DiagnosisSchema(BaseModel):
    id: str
    investigation_id: str
    selected_hypothesis_id: str | None
    summary: str
    verified_cause: str
    evidence: list[str]
    rejected_hypotheses: list[RejectedHypothesisSchema]
    changed_files: list[str]
    risk: str
    recommended_action: str
    created_at: datetime
```

Extend the existing `InvestigationDetailResponse` (or equivalent response model) to include:

```python
class InvestigationDetailResponse(BaseModel):
    ...  # all existing fields
    diagnosis: DiagnosisSchema | None = None
```

#### `app/routers/investigations.py` — load and embed diagnosis

In `GET /api/investigations/{id}`, after loading the `Investigation` row, also load the
`Diagnosis` row (if it exists) via the relationship or a separate query, and include it in
the response.

```python
# In the route handler:
async with session.begin():
    inv = await session.get(Investigation, investigation_id, options=[selectinload(Investigation.diagnosis)])
    if inv is None:
        raise HTTPException(404, ...)
    ...
    diagnosis_out = DiagnosisSchema.model_validate(inv.diagnosis) if inv.diagnosis else None
return InvestigationDetailResponse(..., diagnosis=diagnosis_out)
```

---

### Task 4 — Tests (`tests/test_arbiter_agent.py`)

All LLM calls mocked. Use the `session_factory` fixture from `conftest.py`.

#### Test: one VERIFIED hypothesis → selected as winner

```python
async def test_arbiter_selects_verified_hypothesis(session_factory):
    # Seed: one VERIFIED hypothesis with full TestEvidence, two REJECTED
    # Mock _chat to return emit_diagnosis pointing to the VERIFIED hypothesis
    # Assert: Diagnosis.selected_hypothesis_id == verified_hyp_id
    # Assert: Diagnosis.risk in {"low", "medium", "high"}
    # Assert: len(Diagnosis.evidence) >= 1
    # Assert: len(Diagnosis.rejected_hypotheses) == 2
```

#### Test: no VERIFIED hypothesis → no-winner diagnosis (no LLM call)

```python
async def test_arbiter_no_winner_when_all_rejected(session_factory):
    # Seed: three REJECTED hypotheses
    # DO NOT mock _chat (should not be called)
    # Assert: Diagnosis.selected_hypothesis_id is None
    # Assert: "No hypothesis" in Diagnosis.summary
    # Assert: _chat was never called (use patch to confirm)
```

#### Test: all BLOCKED → no-winner diagnosis (no LLM call)

```python
async def test_arbiter_no_winner_when_all_blocked(session_factory):
    # Seed: three BLOCKED hypotheses
    # Assert: Diagnosis.selected_hypothesis_id is None
    # Assert: _chat never called
```

#### Test: confidence field absent from LLM prompt (AGENTS.md)

```python
async def test_arbiter_confidence_not_in_prompt(session_factory):
    # Seed hypotheses with confidence="high" on the VERIFIED one
    # Capture messages passed to _chat
    # Assert: the word "confidence" does not appear in any message content
```

#### Test: API key not in prompt (AGENTS.md)

```python
async def test_arbiter_api_key_not_in_prompt(session_factory):
    # Set settings.llm_api_key = "sk-test-ARBITER-SECRET"
    # Capture messages passed to _chat
    # Assert: "sk-test-ARBITER-SECRET" not in any message content
```

#### Test: Diagnosis row persisted in DB

```python
async def test_arbiter_persists_diagnosis_row(session_factory):
    # Seed: one VERIFIED hypothesis
    # Run ArbiterAgent.run(...)
    # Query Diagnosis table
    # Assert: one row exists with correct investigation_id and selected_hypothesis_id
```

#### Test: LLM fallback when emit_diagnosis not called first time

```python
async def test_arbiter_retries_when_no_tool_call(session_factory):
    # Mock _chat to return plain text on first call, emit_diagnosis on second
    # Assert: Diagnosis is still correctly produced
    # Assert: _chat was called exactly 2 times
```

#### Test: rejected_hypotheses list populated

```python
async def test_arbiter_rejected_hypotheses_populated(session_factory):
    # Seed: one VERIFIED, two REJECTED
    # Assert: Diagnosis.rejected_hypotheses has 2 entries
    # Assert: each entry has hypothesis_id, agent_type, summary, rejection_reason
```

---

### Task 5 — Tests (`tests/test_orchestrator_cp08.py`)

Add orchestrator-level tests for the `ARBITRATING → WAITING_FOR_REVIEW` transition:

#### Test: investigation reaches WAITING_FOR_REVIEW after full pipeline

```python
async def test_orchestrator_reaches_waiting_for_review(session_factory, tmp_path):
    # Mock agents (emit_hypothesis), mock VerificationEngine.verify (VERIFIED),
    # mock ArbiterAgent.run (returns DiagnosisData with a selected winner)
    # Run run_investigation(...)
    # Assert: investigation.status == WAITING_FOR_REVIEW
    # Assert: Diagnosis row exists
```

#### Test: arbiter failure → investigation FAILED

```python
async def test_orchestrator_failed_when_arbiter_raises(session_factory, tmp_path):
    # Mock agents, mock VerificationEngine.verify (returns VERIFIED)
    # Mock ArbiterAgent.run to raise RuntimeError
    # Assert: investigation.status == FAILED
```

#### Test: API endpoint returns embedded diagnosis

```python
async def test_get_investigation_includes_diagnosis(client, session_factory):
    # Seed investigation + Diagnosis row directly
    # GET /api/investigations/{id}
    # Assert: response JSON has a "diagnosis" key with the expected fields
    # Assert: response JSON does not expose the confidence field
```

---

## File-by-File Specification

### `backend/app/agents/arbiter_agent.py`

```
class RejectedHypothesisInfo(BaseModel)
class DiagnosisData(BaseModel)

class ArbiterAgent:
    __init__()                    # reads settings.llm_api_key internally

    # public
    async run(investigation_id, session_factory) → DiagnosisData

    # private
    async _build_evidence_summary(investigation_id, session_factory)
        → tuple[str, list[dict]]   # (prompt_text, raw_hyp_list)

    async _call_llm(prompt_text) → DiagnosisData
        # one LLM call + one retry if no emit_diagnosis
        # falls back to error diagnosis on second failure

    def _no_winner_diagnosis(hyp_dicts) → DiagnosisData
        # deterministic, no LLM

    async _persist_diagnosis(investigation_id, data, session_factory) → Diagnosis

    async _chat(messages, tools) → dict
        # same pattern as OpenAIAgentModel._chat — key from settings only
```

**Key constraint:** `_build_evidence_summary` loads the `confidence` column but silently
drops it from the assembled text before the string is passed to `_call_llm`. This is the
single enforcement point for the AGENTS.md "confidence is informational only" rule.

### `backend/app/orchestrator.py` (modified)

Add:
- `from app.agents.arbiter_agent import ArbiterAgent` import
- `async def _run_arbitration(session_factory, investigation_id, bug_context) → None`
- One `await _run_arbitration(...)` call after `await _run_verification(...)` in `run_investigation`

### `backend/app/schemas/investigation.py` (modified)

Add:
- `RejectedHypothesisSchema`
- `DiagnosisSchema`
- `diagnosis: DiagnosisSchema | None = None` field on `InvestigationDetailResponse`
  (or equivalent response model — check the existing schema file for the exact class name)

### `backend/app/routers/investigations.py` (modified)

In `GET /api/investigations/{id}`:
- Add `selectinload(Investigation.diagnosis)` to the query
- Serialize `inv.diagnosis` into `DiagnosisSchema` if present
- Include in response

---

## Acceptance Criteria

| # | Criterion | How verified |
|---|---|---|
| 1 | VERIFIED hypothesis selected as winner | `test_arbiter_selects_verified_hypothesis` |
| 2 | All-rejected → `selected_hypothesis_id = None` | `test_arbiter_no_winner_when_all_rejected` |
| 3 | All-blocked → `selected_hypothesis_id = None` | `test_arbiter_no_winner_when_all_blocked` |
| 4 | `confidence` absent from LLM prompt (AGENTS.md) | `test_arbiter_confidence_not_in_prompt` |
| 5 | API key absent from LLM prompt (AGENTS.md) | `test_arbiter_api_key_not_in_prompt` |
| 6 | `Diagnosis` row persisted with correct fields | `test_arbiter_persists_diagnosis_row` |
| 7 | LLM retry when no `emit_diagnosis` first call | `test_arbiter_retries_when_no_tool_call` |
| 8 | `rejected_hypotheses` list populated | `test_arbiter_rejected_hypotheses_populated` |
| 9 | Investigation reaches `WAITING_FOR_REVIEW` | `test_orchestrator_reaches_waiting_for_review` |
| 10 | Arbiter exception → investigation `FAILED` | `test_orchestrator_failed_when_arbiter_raises` |
| 11 | `GET /api/investigations/{id}` embeds diagnosis | `test_get_investigation_includes_diagnosis` |
| 12 | `pytest backend/` all green | CI |
| 13 | `ruff check .` no errors | CI |

---

## Cross-Cutting Constraints Enforced in This CP

| Constraint | AGENTS.md source | Enforcement in CP-08 |
|---|---|---|
| `confidence` is informational only | AGENTS.md §arbiter | `_build_evidence_summary` drops confidence column; test asserts the word never appears in prompts |
| Credentials never in prompts | AGENTS.md §credentials | `ArbiterAgent._chat` reads key from `settings` only; `test_arbiter_api_key_not_in_prompt` verifies |
| No workflow framework | AGENTS.md §orchestration | ArbiterAgent is a plain `async def run()` — no LangGraph, no Prefect |
| PR requires `APPROVED` status | AGENTS.md §pr | Not enforced here — CP-09 handles the route guard; CP-08 only reaches `WAITING_FOR_REVIEW` |

---

## Build Order Within CP-08

```
1. app/agents/arbiter_agent.py            (depends on DB models, config, httpx)
2. tests/test_arbiter_agent.py            (parallel with step 1 — can be written first as spec)
3. app/orchestrator.py  (add _run_arbitration + wire)
4. app/schemas/investigation.py  (add DiagnosisSchema + embed in response)
5. app/routers/investigations.py  (embed diagnosis in GET handler)
6. tests/test_orchestrator_cp08.py        (depends on steps 3–5)
7. ruff / pytest — verify all green
```

---

## Open Questions / Decisions for Implementation

1. **Single LLM call vs. short agentic loop.** The Arbiter does not need tools (it only reads
   pre-assembled evidence text) so a single call + one retry is sufficient. No tool-call loop
   required. This avoids over-engineering and keeps the path fast and deterministic.

2. **LLM call when there is a clear winner.** Even when exactly one VERIFIED hypothesis exists,
   the LLM call is still made (unless the no-winner fast-path applies). The LLM produces the
   narrative: `summary`, `verified_cause`, `evidence` bullets, `risk`, `recommended_action`, and
   `rejection_reason` for each rejected hypothesis. These fields are too nuanced to generate
   deterministically without the LLM.

3. **No-winner fast-path.** If zero hypotheses have status `VERIFIED`, skip the LLM entirely.
   This saves tokens, avoids a potential hallucination of a winner, and is always correct.
   The deterministic fallback populates all fields with the "no evidence" message.

4. **INCONCLUSIVE hypotheses.** They are treated the same as REJECTED by the Arbiter — they
   do not qualify as winners. They appear in `rejected_hypotheses` with
   `rejection_reason = "Pre-fix regression test passed unexpectedly — bug not reproducible via this approach."`.

5. **Multiple VERIFIED hypotheses.** The Arbiter's selection criteria break ties: prefer the
   hypothesis with the most passing existing tests (from `TestEvidence.existing_suite_result`),
   then fewest `changed_files`, then lowest `patch_attempts`. The LLM is instructed to follow
   these criteria in its prompt.

6. **`changed_files` source.** Populated from `Hypothesis.suspected_files` for the selected
   winner (the actual committed `Patch` object is created in CP-12, not CP-08). Until then,
   `suspected_files` is the best available proxy.

7. **Upsert semantics on `Diagnosis`.** The `diagnoses` table has a `UNIQUE` constraint on
   `investigation_id`. If the arbiter is re-run (e.g., after a FAILED status is manually
   reset), `_persist_diagnosis` should update the existing row rather than insert a duplicate.
   Use `INSERT OR REPLACE` semantics or a `SELECT` + `UPDATE` pattern.

8. **`completed_at` timestamp.** When transitioning to `WAITING_FOR_REVIEW`, also set
   `investigation.completed_at = datetime.now(UTC)` — this is the logical end of the
   automated pipeline.

---

## Example Diagnosis Output (for seeded demo)

```json
{
  "id": "diag_abc123",
  "investigation_id": "inv_xyz",
  "selected_hypothesis_id": "hyp_001",
  "summary": "A race condition between the idempotency check and the database insert allows two concurrent requests to both observe an empty state and both insert, producing duplicate ReviewRun rows.",
  "verified_cause": "Race condition between idempotency lookup and persistence in ReviewService.submit_review().",
  "evidence": [
    "Bug reproduced with controlled concurrent interleaving",
    "Regression test fails on original code (2 rows inserted, expected 1)",
    "Regression test passes after threading.Lock patch (1 row, idempotent)",
    "4 existing sequential tests pass after patch"
  ],
  "rejected_hypotheses": [
    {
      "hypothesis_id": "hyp_002",
      "agent_type": "git_history",
      "summary": "Recent refactor moved uniqueness check outside transaction.",
      "rejection_reason": "Post-fix regression test still failed — patch did not address the observed failure."
    },
    {
      "hypothesis_id": "hyp_003",
      "agent_type": "test_behavior",
      "summary": "Malformed idempotency key hashing produces collisions.",
      "rejection_reason": "Agent failed during investigation (BLOCKED)."
    }
  ],
  "changed_files": ["review_repository.py"],
  "risk": "low",
  "recommended_action": "Review the threading.Lock patch in review_repository.py and create a draft PR.",
  "created_at": "2025-09-26T10:14:00Z"
}
```
