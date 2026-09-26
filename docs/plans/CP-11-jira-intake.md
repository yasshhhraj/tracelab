# CP-11 — Jira Intake (MCP / REST)

**Goal:** A Jira bug can trigger an investigation directly — either by the
dashboard "Import from Jira" dialog or via a Jira webhook. The system fetches
the full Jira issue, normalises it into a `BugContext` using an LLM-backed
Intake Agent, creates the Investigation row, and fires the existing orchestrator
pipeline. On completion it writes a structured comment back to the Jira issue.

**Prerequisite:** CP-09 is complete. `settings.jira_base_url` and
`settings.jira_api_token` already exist in `config.py` and `.env.example`.

---

## 1. Deliverables

| # | Artifact | Description |
|---|----------|-------------|
| 1 | `app/integrations/jira_client.py` | Async HTTP client for the Jira REST API v3 |
| 2 | `app/agents/intake_agent.py` | LLM-backed normaliser: `JiraIssue → BugContext` |
| 3 | `app/routers/jira.py` | Two endpoints: import + webhook |
| 4 | Hook in orchestrator | Post-diagnosis Jira comment |
| 5 | `tests/test_jira_client.py` | Unit tests — all HTTP mocked |
| 6 | `tests/test_intake_agent.py` | Unit tests — LLM call mocked |
| 7 | `tests/test_jira_router.py` | API-level integration tests |

---

## 2. New Files and Their Full Interface

### 2.1 `app/integrations/jira_client.py`

```python
# ── Data model returned by the client ─────────────────────────────────────────

class JiraIssue(BaseModel):
    """Raw Jira issue fields, parsed from the REST response.
    No credentials appear in this model (AGENTS.md).
    """
    issue_id: str           # e.g. "PVS-421"
    summary: str            # issue title
    description: str        # Jira "description" field (ADF → plain text)
    issue_type: str         # "Bug", "Task", etc.
    priority: str           # "High", "Medium", etc.
    status: str             # Jira workflow status label
    components: list[str]   # affected component names
    labels: list[str]
    reporter: str           # display name
    assignee: str | None
    comments: list[str]     # body of each comment, plain text
    attachments: list[str]  # filenames only (content not fetched in prototype)
    created_at: str         # ISO-8601 string
    updated_at: str


# ── Client ────────────────────────────────────────────────────────────────────

class JiraClient:
    """
    Async Jira REST API v3 client.

    Credentials are injected from settings at construction time and NEVER
    placed in any prompt, log line, or AgentContext (AGENTS.md).

    Usage:
        client = JiraClient()          # reads from settings
        issue = await client.get_issue("PVS-421")
        await client.add_comment("PVS-421", "AI analysis complete: ...")
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        jira_user_email: str | None = None,
    ) -> None:
        """
        base_url     — e.g. "https://your-org.atlassian.net" (settings.jira_base_url)
        api_token    — Jira personal API token (settings.jira_api_token)
        jira_user_email — required for Basic auth with Jira Cloud
                          read from settings.jira_user_email (new field)
        """

    async def get_issue(self, issue_id: str) -> JiraIssue:
        """
        Fetch a Jira issue and map its fields to JiraIssue.

        GET /rest/api/3/issue/{issue_id}?fields=summary,description,issuetype,
            priority,status,components,labels,reporter,assignee,comment,attachment,
            created,updated

        Raises:
            JiraNotFoundError  — HTTP 404
            JiraAuthError      — HTTP 401 / 403
            JiraClientError    — any other HTTP error
        """

    async def add_comment(self, issue_id: str, body: str) -> None:
        """
        POST /rest/api/3/issue/{issue_id}/comment

        body is plain text; wrapped in Atlassian Document Format (ADF) for
        the request payload.

        SECURITY: Never include raw credentials or tokens in `body`.
        """

    async def transition_status(self, issue_id: str, status_name: str) -> None:
        """
        Transition the issue to a named workflow status.

        Fetches available transitions first (GET /rest/api/3/issue/{id}/transitions),
        finds the one whose name matches status_name (case-insensitive), then
        POST /rest/api/3/issue/{id}/transitions  {transitionId: "..."}

        Raises JiraTransitionNotFoundError if no matching transition exists.
        No-op silently if the issue is already in the target status.
        """
```

**Authentication:** Jira Cloud uses HTTP Basic auth:
`Authorization: Basic base64(email:token)`.
The email is stored in a new `settings.jira_user_email` field (see §6).

**ADF wrapper** for `add_comment`:
```python
def _plain_text_to_adf(text: str) -> dict:
    """Wrap a plain-text string in the minimal Atlassian Document Format body."""
    return {
        "body": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    }
```

**Custom exceptions** (all in the same file):
```python
class JiraClientError(Exception): ...
class JiraNotFoundError(JiraClientError): ...
class JiraAuthError(JiraClientError): ...
class JiraTransitionNotFoundError(JiraClientError): ...
```

---

### 2.2 `app/agents/intake_agent.py`

The Intake Agent converts a raw `JiraIssue` into a typed `BugContext` using the
same `OpenAIAgentModel` infrastructure already used by investigation agents.

```python
# ── System prompt ──────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """
You are TraceLab's intake agent. You receive a raw Jira issue and must extract
a normalised bug context by calling emit_bug_context once.

Rules:
- symptom: one sentence describing the observed failure (not the Jira title).
- expected: what should have happened.
- actual: what actually happened (do not repeat the symptom verbatim).
- error_type: MUST be one of "deterministic", "intermittent", "regression".
  - "intermittent" if the issue mentions flaky / race / timing / sometimes.
  - "regression" if the issue mentions "used to work" or references a breaking commit.
  - "deterministic" otherwise.
- known_evidence: extract concrete facts (log lines, metrics, stack traces,
  reproduction steps) as individual list items. Max 10.
- stack_trace: copy the full stack trace if one is present, else null.
- affected_area: the component or service name — derive from components field,
  or infer from summary/description.
NEVER invent information that is not present in the Jira fields.
"""

# ── Terminal tool schema ───────────────────────────────────────────────────────
_EMIT_BUG_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_bug_context",
        "description": "Emit the normalised bug context. Call exactly once.",
        "parameters": {
            "type": "object",
            "required": [
                "symptom", "expected", "actual",
                "affected_area", "error_type", "known_evidence",
            ],
            "properties": {
                "symptom":        {"type": "string"},
                "expected":       {"type": "string"},
                "actual":         {"type": "string"},
                "affected_area":  {"type": "string"},
                "error_type":     {"type": "string", "enum": ["deterministic", "intermittent", "regression"]},
                "known_evidence": {"type": "array", "items": {"type": "string"}},
                "stack_trace":    {"type": "string", "nullable": True},
            },
        },
    },
}

# ── Agent ──────────────────────────────────────────────────────────────────────

class IntakeAgent:
    """
    Converts a JiraIssue into a BugContext using an LLM.

    Does NOT perform any file-system or git operations.
    No credentials appear in the prompt (AGENTS.md).
    """

    async def run(self, issue: JiraIssue, repository: str, base_branch: str) -> BugContext:
        """
        Send the Jira issue fields to the LLM, parse the emit_bug_context call,
        return a typed BugContext.

        Falls back to a heuristic (title = symptom, description = actual)
        if the LLM does not call emit_bug_context within 3 iterations.
        """
```

The agent builds a single user message from the `JiraIssue` fields (no
function-calling loop needed — just one round-trip with `tool_choice: "required"`
pointing at `emit_bug_context`). It re-uses `settings.llm_api_key`,
`settings.llm_model`, and `settings.llm_base_url` — same pattern as
`OpenAIAgentModel._chat()`.

---

### 2.3 `app/routers/jira.py`

```python
router = APIRouter(prefix="/api/jira", tags=["jira"])

# ── POST /api/jira/import/{issue_id} ──────────────────────────────────────────

class JiraImportRequest(BaseModel):
    """Optional overrides for an imported Jira issue."""
    repository: str | None = None   # override settings.target_repository
    base_branch: str = "main"

class JiraImportResponse(BaseModel):
    investigation_id: str
    issue_id: str
    status: str                     # always "CREATED" at return time
    bug_context: dict               # the normalised BugContext as a dict

@router.post("/import/{issue_id}", response_model=JiraImportResponse, status_code=201)
async def import_jira_issue(
    issue_id: str,
    body: JiraImportRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> JiraImportResponse:
    """
    1. Fetch the Jira issue via JiraClient.
    2. Normalise it via IntakeAgent → BugContext.
    3. Create Investigation row (same as POST /api/investigations).
    4. Fire run_investigation as a background task.
    5. Return investigation_id + normalised bug_context immediately.

    Raises:
        404 — Jira issue not found (JiraNotFoundError)
        503 — Jira unreachable / auth failure (JiraClientError)
        422 — repository not configured anywhere
    """

# ── POST /api/jira/webhook ─────────────────────────────────────────────────────

class JiraWebhookPayload(BaseModel):
    """
    Minimal parse of the Jira issue_updated webhook body.
    Jira sends a large JSON blob; we only need a subset.
    """
    issue_id: str = Field(alias="issue.id")           # from payload.issue.id
    issue_key: str = Field(alias="issue.key")         # e.g. "PVS-421"
    changelog_status: str | None = None               # new status after transition

    model_config = {"populate_by_name": True}

@router.post("/webhook", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Accepts Jira `issue_updated` webhook events.

    Fires an investigation only when the new status label contains
    "Ready for AI Debugging" (case-insensitive).
    Returns 202 immediately; processing is async.

    Webhook secret validation: if JIRA_WEBHOOK_SECRET is set in env,
    compare it against the `X-Hub-Signature` header (HMAC-SHA256).
    If the header is missing but the secret is set → 403.
    If the secret is not set → accept all requests (dev mode).
    """
```

---

### 2.4 Orchestrator hook — post-diagnosis Jira comment

After `_run_arbitration()` succeeds and status transitions to
`WAITING_FOR_REVIEW`, the orchestrator calls a new private helper:

```python
async def _post_jira_comment(
    session_factory: async_sessionmaker,
    investigation_id: str,
) -> None:
    """
    Fire-and-forget: add a structured comment to the originating Jira issue.

    Skipped silently if:
    - settings.jira_base_url or settings.jira_api_token is empty (not configured)
    - The investigation has no diagnosis
    - Any JiraClientError (do not fail the investigation over a comment)

    Comment format:
        AI Debugging Analysis Complete

        3 root-cause hypotheses investigated.
        Verified: <verified_cause>

        Evidence:
        - <evidence item 1>
        - ...

        Risk: <risk>
        Recommended action: <recommended_action>

        TraceLab investigation: <investigation_id>
    """
```

This helper is called at the end of `run_investigation()`, after the
`WAITING_FOR_REVIEW` transition, wrapped in a `try/except` so a Jira outage
never blocks or fails an investigation.

---

## 3. Settings Changes

Add two fields to `app/config.py` `Settings`:

```python
# Jira — already present:
jira_base_url: str = ""
jira_api_token: str = ""

# New fields:
jira_user_email: str = ""           # required for Jira Cloud Basic auth
jira_webhook_secret: str = ""       # optional HMAC secret for webhook validation
```

Add to `.env.example`:
```
JIRA_USER_EMAIL=you@your-org.com
JIRA_WEBHOOK_SECRET=                # leave blank in dev
```

---

## 4. Router Registration

Add to `app/main.py`:

```python
from app.routers.jira import router as jira_router
...
app.include_router(jira_router)
```

---

## 5. Data Flow Diagram

```
POST /api/jira/import/{issue_id}
        │
        ▼
  JiraClient.get_issue()
  GET /rest/api/3/issue/{id}
        │
        ▼
     JiraIssue
        │
        ▼
  IntakeAgent.run()
  LLM: JiraIssue → emit_bug_context()
        │
        ▼
     BugContext
        │
        ▼
  INSERT investigations (status=CREATED)
        │
        ├──── return JiraImportResponse (201)
        │
        ▼  [background task]
  run_investigation()
  (existing CP-06–08 pipeline)
        │
        ▼
  WAITING_FOR_REVIEW
        │
        ▼
  _post_jira_comment()
  POST /rest/api/3/issue/{id}/comment
```

---

## 6. Task Breakdown

### Task 1 — Jira client (`app/integrations/jira_client.py`)

1. Create `app/integrations/__init__.py` (empty).
2. Implement `JiraIssue` Pydantic model.
3. Implement `JiraClient.__init__` — reads from `settings`, stores as instance
   vars, builds Base64 `Authorization` header at construction time. Header is
   stored in a private attribute; never logged.
4. Implement `get_issue()`:
   - `GET /rest/api/3/issue/{issue_id}` with `fields` query param.
   - Parse `fields.description` — Jira Cloud returns Atlassian Document Format
     (ADF); walk the ADF tree to extract plain text (recursive helper
     `_adf_to_text(node: dict) -> str`).
   - Parse `fields.comment.comments[]` — extract `body` (also ADF) for each.
   - Map `fields.issuetype.name`, `fields.priority.name`, `fields.status.name`,
     `fields.components[].name`, `fields.labels[]`.
   - Raise typed exceptions on non-2xx.
5. Implement `add_comment()` — wrap plain text in `_plain_text_to_adf()` and
   POST to `/rest/api/3/issue/{id}/comment`.
6. Implement `transition_status()` — fetch transitions, find by name, POST.
7. Add module-level `_LOGGER = logging.getLogger(__name__)`. Log at INFO level:
   `"Fetching Jira issue %s"`, `"Adding comment to %s"`. Never log the token.

**Estimated size:** ~200 lines.

---

### Task 2 — Intake Agent (`app/agents/intake_agent.py`)

1. Define `_SYSTEM_PROMPT` (see §2.2).
2. Define `_EMIT_BUG_CONTEXT_SCHEMA` tool schema.
3. Implement `IntakeAgent.run(issue, repository, base_branch) -> BugContext`:
   - Build a user message string from all `JiraIssue` fields:
     ```
     Issue: {issue_id}  ({issue_type} · {priority})
     Summary: {summary}
     Status: {status}
     Components: {', '.join(components) or 'none'}
     Labels: {', '.join(labels) or 'none'}
     Reporter: {reporter}

     Description:
     {description}

     Comments ({len(comments)}):
     {chr(10).join(f'- {c}' for c in comments[:5])}

     Attachments: {', '.join(attachments) or 'none'}
     ```
   - POST to `{llm_base_url}/chat/completions` with:
     - `messages: [system, user]`
     - `tools: [_EMIT_BUG_CONTEXT_SCHEMA]`
     - `tool_choice: {"type": "function", "function": {"name": "emit_bug_context"}}`
   - Parse the single tool call response → construct `BugContext`.
   - Fallback if parse fails: use `issue.summary` as `symptom`, `"See Jira description"` as `actual`.
4. Log at INFO: `"IntakeAgent normalising issue %s"`. Never log `settings.llm_api_key`.

**Estimated size:** ~120 lines.

---

### Task 3 — Jira router (`app/routers/jira.py`)

1. Implement `JiraImportRequest` and `JiraImportResponse` Pydantic models.
2. Implement `import_jira_issue`:
   - Instantiate `JiraClient()` and `IntakeAgent()`.
   - Call `client.get_issue(issue_id)` → catch `JiraNotFoundError` → 404,
     `JiraAuthError` → 503, `JiraClientError` → 503.
   - Call `agent.run(issue, repository, base_branch)` → `BugContext`.
   - Create `InvestigationORM` and commit (same pattern as
     `create_investigation()` in `investigations.py`).
   - `background_tasks.add_task(run_investigation, inv.id, AsyncSessionLocal)`.
   - Return `JiraImportResponse`.
3. Implement `JiraWebhookPayload` model and `jira_webhook`:
   - Read raw request body for HMAC validation (if `settings.jira_webhook_secret`
     is set).
   - Parse `request.json()` manually — Jira's webhook shape is not a flat object;
     extract `webhookEvent`, `issue.key`, and transition status from
     `changelog.items` where `field == "status"`.
   - Only proceed if `toString` (new status) matches `"ready for ai debugging"`
     (case-insensitive).
   - Re-use `import_jira_issue` logic inline (or call a shared helper).
   - Return `{"accepted": True}` with 202.

**Estimated size:** ~160 lines.

---

### Task 4 — Orchestrator post-diagnosis hook

In `app/orchestrator.py`, inside `run_investigation()`:

After the block that sets `WAITING_FOR_REVIEW` in `_run_arbitration()`, add a
call (wrapped in try/except so it never bubbles up):

```python
# Fire Jira comment — CP-11.  Skipped silently if Jira not configured.
with contextlib.suppress(Exception):
    await _post_jira_comment(session_factory, investigation_id)
```

Implement `_post_jira_comment()` as a module-level private coroutine
(not inside `run_investigation`).

The function:
1. Returns immediately if `settings.jira_base_url` or `settings.jira_api_token`
   is empty.
2. Loads the `Investigation` and its `diagnosis` from the DB.
3. If no diagnosis, returns.
4. Formats the comment string (see §2.4).
5. Calls `JiraClient().add_comment(investigation.external_issue_id, comment)`.
6. Logs success/failure at INFO — never logs the token.

**Estimated size:** ~40 lines added to `orchestrator.py`.

---

### Task 5 — Register the router

In `app/main.py`, add:
```python
from app.routers.jira import router as jira_router
...
app.include_router(jira_router)
```

---

### Task 6 — Tests: `tests/test_jira_client.py`

All HTTP calls mocked with `unittest.mock.patch` / `respx` (or
`unittest.mock.AsyncMock` on `httpx.AsyncClient.get` / `.post`).

| Test | What it asserts |
|------|----------------|
| `test_get_issue_ok` | Successful response → `JiraIssue` fields populated correctly |
| `test_get_issue_adf_description` | ADF block-level content (paragraph, text) extracted as plain text |
| `test_get_issue_404` | `JiraNotFoundError` raised |
| `test_get_issue_401` | `JiraAuthError` raised |
| `test_get_issue_500` | `JiraClientError` raised |
| `test_add_comment_ok` | POST called with correct ADF body; no token in payload |
| `test_add_comment_error` | `JiraClientError` raised on non-2xx |
| `test_token_not_in_log` | caplog at DEBUG level never contains the token string |

---

### Task 7 — Tests: `tests/test_intake_agent.py`

LLM HTTP call mocked.

| Test | What it asserts |
|------|----------------|
| `test_run_deterministic_issue` | Normal issue → `BugContext.error_type == "deterministic"` |
| `test_run_intermittent_issue` | Issue with "race condition" → `error_type == "intermittent"` |
| `test_run_regression_issue` | Issue mentioning "used to work" → `error_type == "regression"` |
| `test_run_extracts_stack_trace` | Stack trace in description → `bug_context.stack_trace` populated |
| `test_run_llm_fallback` | LLM returns no tool call → fallback BugContext constructed |
| `test_api_key_not_in_message` | Asserts `settings.llm_api_key` not present in any message dict |

---

### Task 8 — Tests: `tests/test_jira_router.py`

Uses `client_with_db` fixture from `conftest.py`. Patches `JiraClient` and
`IntakeAgent` with `AsyncMock`.

| Test | What it asserts |
|------|----------------|
| `test_import_creates_investigation` | `POST /api/jira/import/PVS-421` → 201, `investigation_id` in response |
| `test_import_investigation_in_db` | Investigation row exists in DB with correct `external_issue_id` |
| `test_import_bug_context_populated` | `bug_context` in response matches IntakeAgent output |
| `test_import_not_found` | `JiraNotFoundError` → 404 |
| `test_import_auth_error` | `JiraAuthError` → 503 |
| `test_import_no_repository` | Neither body nor settings → 422 |
| `test_webhook_ready_for_ai` | Webhook with correct status → 202, investigation created |
| `test_webhook_other_status` | Webhook with other status → 202, no investigation created |
| `test_webhook_hmac_invalid` | Secret set, wrong signature → 403 |
| `test_webhook_hmac_missing` | Secret set, no header → 403 |
| `test_webhook_no_secret` | Secret not set → accepts all (dev mode) |

---

## 7. Acceptance Criteria

| # | Criterion |
|---|-----------|
| AC-1 | `POST /api/jira/import/PVS-421` returns 201 with `investigation_id` and fully populated `bug_context`. |
| AC-2 | `JIRA_API_TOKEN` and `JIRA_USER_EMAIL` do not appear in any log line (checked by `test_token_not_in_log`). |
| AC-3 | After investigation reaches `WAITING_FOR_REVIEW`, a comment is added to the Jira issue (verified with mocked client). |
| AC-4 | Jira outage (500) on comment → investigation status remains `WAITING_FOR_REVIEW`; error is logged but not re-raised. |
| AC-5 | Webhook fires an investigation only when new status matches `"Ready for AI Debugging"`. |
| AC-6 | Webhook with invalid HMAC secret → 403. |
| AC-7 | `pytest backend/tests/test_jira_client.py backend/tests/test_intake_agent.py backend/tests/test_jira_router.py` all green. |
| AC-8 | `pytest backend/tests/` (full suite) still green — no regressions. |
| AC-9 | `ruff check backend/app` passes with no new violations. |

---

## 8. Security Checklist

- [ ] `settings.jira_api_token` read only in `JiraClient.__init__` — stored as
      instance variable, never passed to `IntakeAgent` or placed in any message.
- [ ] `JiraClient._auth_header` is a private attribute; not serialised, not
      logged, not returned in any API response.
- [ ] `IntakeAgent` user message is built from `JiraIssue` fields only — no env
      vars injected.
- [ ] `JIRA_WEBHOOK_SECRET` validated before use; missing config → dev-mode
      warning logged, not silently ignored.
- [ ] `add_comment` body is plain text from the diagnosis — no raw DB values or
      repo credentials included.

---

## 9. Open Questions / Decisions

| # | Question | Recommended default |
|---|----------|---------------------|
| Q1 | Should `transition_status` be called to move the Jira issue to `AI_ANALYZING` when the investigation starts? | Yes — call it in `run_investigation` at `CONTEXT_LOADING`, silently skip if transition not found. |
| Q2 | Should the Jira comment include the stub PR URL? | Yes — include it when available (`investigation.diagnosis` has `selected_hypothesis_id` → look up `patch.pr_url`). |
| Q3 | Should the webhook endpoint be protected by an API key in addition to HMAC? | Out of scope for CP-11; note as CP-12 concern. |
| Q4 | ADF parsing depth: nested lists, bullet points, code blocks? | Parse only `paragraph/text` and `codeBlock/text` nodes for MVP. Mark TODO for richer ADF support. |

---

## 10. File Change Summary

```
backend/
  app/
    config.py                    EDIT — add jira_user_email, jira_webhook_secret
    main.py                      EDIT — include jira_router
    integrations/
      __init__.py                NEW  (empty)
      jira_client.py             NEW  (~200 lines)
    agents/
      intake_agent.py            NEW  (~120 lines)
    routers/
      jira.py                    NEW  (~160 lines)
    orchestrator.py              EDIT — add _post_jira_comment(), call after WAITING_FOR_REVIEW
  tests/
    test_jira_client.py          NEW  (~140 lines)
    test_intake_agent.py         NEW  (~100 lines)
    test_jira_router.py          NEW  (~150 lines)
.env.example                     EDIT — add JIRA_USER_EMAIL, JIRA_WEBHOOK_SECRET
```

**Total new lines:** ~870. **Edited lines:** ~30.
