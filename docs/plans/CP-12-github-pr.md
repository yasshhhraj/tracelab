# CP-12 — GitHub Draft PR Creation

> **Current status (2026-09-26):** Implementation and mocked tests are in place. Live draft-PR creation is deferred until after MVP deployment; see [CP-12 live validation follow-up](CP-12-live-validation-follow-up.md). The historical stub behavior described below has since been removed: missing verified patches return `409`, and a missing GitHub token returns `503`.

**Goal:** After human approval, the winning patch branch is pushed to GitHub
and a draft pull request is opened via the GitHub REST API v3.
`POST /api/investigations/{id}/pull-request` transitions from returning a stub
URL to creating a real GitHub draft PR.

**Prerequisite:** CP-09 is complete (approval gate enforced). CP-11 is
complete (Jira comment hook exists). `settings.github_token` already exists
in `config.py` and `.env.example`.

---

## 1. Deliverables

| # | Artifact | Description |
|---|----------|-------------|
| 1 | `app/integrations/github_client.py` | Async GitHub REST API v3 client |
| 2 | `app/integrations/pr_body.py` | PR body template renderer |
| 3 | `app/routers/investigations.py` (edit) | Replace stub with real GitHub push + PR creation |
| 4 | `tests/test_github_client.py` | Unit tests — all HTTP mocked |
| 5 | `tests/test_pr_body.py` | Unit tests — template rendering |
| 6 | `tests/test_api_pr.py` (edit) | Add CP-12 acceptance tests alongside existing CP-09 tests |

---

## 2. What the stub currently does (CP-09 baseline)

`POST /api/investigations/{id}/pull-request` in
[`app/routers/investigations.py`](../app/routers/investigations.py:238):

1. Checks `status == APPROVED` → 409 otherwise.
2. Loads the winning `Patch` row for `diagnosis.selected_hypothesis_id`.
3. If `patch.pr_url` is already set, returns it (idempotent re-call).
4. Otherwise generates a **placeholder URL**:
   `https://github.com/placeholder/{repo_name}/pull/new/{branch}`
5. Persists that URL to `patch.pr_url`.

CP-12 replaces step 4 with real GitHub operations.

---

## 3. New Files and Their Full Interfaces

### 3.1 `app/integrations/github_client.py`

```python
# ── Custom exceptions ─────────────────────────────────────────────────────────

class GitHubClientError(Exception):
    """Base class for all GitHub client errors."""

class GitHubNotFoundError(GitHubClientError):
    """Raised when the repository or ref is not found (HTTP 404)."""

class GitHubAuthError(GitHubClientError):
    """Raised when credentials are rejected (HTTP 401 / 403)."""

class GitHubConflictError(GitHubClientError):
    """Raised when a branch or ref already exists (HTTP 422 / 409)."""


# ── Client ────────────────────────────────────────────────────────────────────

class GitHubClient:
    """
    Async GitHub REST API v3 client.

    The token is read from settings at construction time and NEVER placed in
    any prompt, log line, or AgentContext (AGENTS.md).

    Usage:
        client = GitHubClient()
        sha    = await client.push_branch(
                     owner="org", repo="myapp",
                     branch="ai-debug/PVS-421-h2",
                     local_repo=Path("/tmp/worktree"),
                 )
        pr_url = await client.create_draft_pr(
                     owner="org", repo="myapp",
                     branch="ai-debug/PVS-421-h2",
                     base="main",
                     title="Fix PVS-421: duplicate ReviewRuns",
                     body="...",
                 )
    """

    def __init__(self, token: str | None = None) -> None:
        """
        token — GitHub personal access token or fine-grained PAT.
                Read from settings.github_token if not supplied.
                NEVER logged or placed in messages.
        """

    async def push_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        local_repo: Path,
        force: bool = False,
    ) -> str:
        """
        Push a local branch to GitHub.

        Uses `git push` via the allowlisted tool layer (run_command) with
        the token injected into the remote URL — it is NOT passed as a
        CLI argument (would appear in process listing).

        Remote URL format:
            https://{token}@github.com/{owner}/{repo}.git

        Raises:
            GitHubAuthError      — HTTP 401/403 or git exit code indicating auth failure
            GitHubNotFoundError  — repository not found
            GitHubClientError    — any other push failure

        Returns:
            The HEAD commit SHA on the pushed branch (from git rev-parse HEAD).
        """

    async def get_or_create_ref(
        self,
        owner: str,
        repo: str,
        branch: str,
        sha: str,
    ) -> str:
        """
        Ensure a branch ref exists on GitHub at the given SHA.

        Uses GitHub REST API:
          - Try GET /repos/{owner}/{repo}/git/ref/heads/{branch}
          - If 404: POST /repos/{owner}/{repo}/git/refs (create)
          - If exists: PATCH /repos/{owner}/{repo}/git/refs/heads/{branch} (update)

        Returns the SHA the ref points to after the operation.
        """

    async def create_draft_pr(
        self,
        owner: str,
        repo: str,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        """
        Create a draft pull request via POST /repos/{owner}/{repo}/pulls.

        Payload:
          {
            "title": title,
            "body":  body,
            "head":  branch,
            "base":  base,
            "draft": true
          }

        Returns the HTML URL of the created PR (response["html_url"]).

        Raises:
            GitHubConflictError  — PR already exists for this branch
            GitHubAuthError      — bad token
            GitHubNotFoundError  — repo not found
            GitHubClientError    — any other API error
        """
```

**Authentication:** GitHub REST API uses Bearer token:
`Authorization: Bearer {token}` (also accepted as `token {token}`).
The token is built into a private `_auth_header` attribute at construction;
never logged, never serialised.

**`push_branch` implementation detail — token in remote URL:**

```python
# Safe: token in URL string, not as a CLI arg that appears in ps output.
# The URL is constructed inside the function and not stored anywhere.
remote_url = f"https://{self._token}@github.com/{owner}/{repo}.git"
result = await run_command(
    ["git", "push", remote_url, f"{branch}:{branch}"],
    cwd=local_repo,
)
# After push, read HEAD SHA
sha_result = await run_command(["git", "rev-parse", "HEAD"], cwd=local_repo)
return sha_result.stdout.strip()
```

> **Security note:** `run_command` must NOT log the full command list when
> the command contains a URL with a token. The executor already does not log
> the command; only the result exit code and duration are logged.

**`push_branch` — no-worktree fallback (for repos without a pushed worktree):**

When the local worktree path does not have the branch checked out (e.g. the
worktree was cleaned up), `push_branch` pushes from the diff stored in the
`Patch.diff` field:

1. Create a temp directory with `git init`.
2. Clone the repo URL (HTTPS, credentials in URL).
3. `git checkout -b {branch}`.
4. Apply `Patch.diff` via `git apply`.
5. `git commit -am "TraceLab: {title}"`.
6. Push.

This fallback is used by the PR route when `worktree_path` is `None` (which
is the normal case — worktrees are cleaned up after verification).

---

### 3.2 `app/integrations/pr_body.py`

```python
def build_pr_body(
    issue_id: str,
    symptom: str,
    verified_cause: str,
    evidence: list[str],
    rejected_hypotheses: list[dict],   # [{summary, rejection_reason}, ...]
    changed_files: list[str],
    risk: str,
) -> str:
    """
    Render the PR description from the diagnosis data.

    Template (PRD §22):
    ─────────────────────────────────────────────────────────────────
    Fix {issue_id}: {symptom}

    AI-assisted diagnosis (TraceLab)

    **Verified cause:**
    {verified_cause}

    **Verification:**
    {evidence bullet list}

    **Alternative hypotheses investigated:**
    {rejected bullet list, or "None" if empty}

    **Changed files:**
    {changed_files bullet list}

    **Risk:** {risk}

    Human review required before merge.
    ─────────────────────────────────────────────────────────────────

    This function is pure (no I/O, no side effects).
    """

def build_pr_title(issue_id: str, symptom: str) -> str:
    """
    Render the PR title.

    Format: "Fix {issue_id}: {symptom}"
    Truncated to 255 characters (GitHub limit).
    """
```

---

## 4. Route Handler Replacement

The existing stub in
[`app/routers/investigations.py`](../app/routers/investigations.py:238)
is replaced with a real implementation. The approval gate (`status == APPROVED`
→ 409) and the idempotency check (`patch.pr_url` already set → return cached
URL) are **kept unchanged** — only the stub URL generation is replaced.

### New handler logic (replacing the stub block)

```python
@router.post("/{investigation_id}/pull-request", response_model=PullRequestResponse)
async def create_pull_request(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> PullRequestResponse:
    """
    Create a GitHub draft pull request for an approved investigation.

    AGENTS.md constraint: status MUST be APPROVED — raises 409 otherwise.

    Steps:
      1. Load investigation + diagnosis (assert APPROVED).
      2. Load the winning Patch row.
      3. If patch.pr_url already set → return cached (idempotent).
      4. If GITHUB_TOKEN not configured → return stub URL (dev / demo mode).
      5. Parse owner/repo from investigation.repository.
      6. Push the branch to GitHub (worktree-less: apply diff on fresh clone).
      7. Create draft PR via GitHubClient.create_draft_pr().
      8. Persist pr_url on Patch row.
      9. Update Jira comment with PR link (if Jira configured).
      10. Return PullRequestResponse.
    """
```

### `_parse_github_repo(repository: str) -> tuple[str, str]`

New private helper (module-level in `investigations.py`):

```python
def _parse_github_repo(repository: str) -> tuple[str, str]:
    """
    Extract (owner, repo) from a GitHub repository string.

    Accepts:
        "github.com/org/repo"
        "https://github.com/org/repo"
        "https://github.com/org/repo.git"
        "org/repo"          (bare owner/repo)

    Returns (owner, repo).
    Raises ValueError if the string cannot be parsed.
    """
```

### Step 6 detail — worktree-less push

Since worktrees are destroyed after verification, the PR route applies the
patch diff onto a fresh clone in a temp directory:

```
temp_dir/
  └── repo/           ← git clone {repository}
        ↓  git checkout -b {branch}
        ↓  git apply   {patch.diff}  (via temp file)
        ↓  git commit  -am "TraceLab fix: {issue_id}"
        ↓  git push    {token_url} {branch}:{branch}
```

The temp directory is cleaned up in a `finally` block.

### Step 9 — Jira PR link update (optional)

After the PR is created, if Jira is configured and the investigation has an
`external_issue_id`, call `JiraClient().add_comment(issue_id, f"Draft PR created: {pr_url}")`.
Wrapped in `contextlib.suppress(Exception)` — Jira outage must never fail the
PR creation.

---

## 5. Settings Changes

No new settings fields are required. `github_token` already exists:

```python
# config.py — already present:
github_token: str = ""
```

New optional setting for configuring the GitHub API base URL (for GitHub
Enterprise):

```python
github_api_url: str = "https://api.github.com"   # override for GHE
```

Add to `.env.example`:
```
GITHUB_API_URL=https://api.github.com   # change for GitHub Enterprise
```

---

## 6. Task Breakdown

### Task 1 — `app/integrations/github_client.py`

1. Implement `GitHubClientError`, `GitHubNotFoundError`, `GitHubAuthError`,
   `GitHubConflictError` exception hierarchy.
2. Implement `GitHubClient.__init__`:
   - `_token = token or settings.github_token` — store as private attr.
   - `_auth_header = f"Bearer {_token}"` — built at construction, stored
     privately, NEVER logged.
   - `_api_url = settings.github_api_url` (new field) or `"https://api.github.com"`.
3. Implement `_headers() -> dict` and `_raise_for_status(response)` helpers.
4. Implement `push_branch(owner, repo, branch, local_repo, force)`:
   - Build token-authenticated remote URL (token in URL, not CLI arg).
   - Run `git push <remote_url> <branch>:<branch>` via `run_command`.
   - On non-zero exit: inspect stderr for "401"/"authentication" → raise
     `GitHubAuthError`; "not found"/"repository" → `GitHubNotFoundError`;
     anything else → `GitHubClientError`.
   - Run `git rev-parse HEAD` via `run_command` to get the commit SHA.
   - Return the SHA.
5. Implement `get_or_create_ref(owner, repo, branch, sha)`:
   - `GET /repos/{owner}/{repo}/git/ref/heads/{branch}` → if 200, PATCH to
     update; if 404, POST to create.
   - Return the ref SHA.
6. Implement `create_draft_pr(owner, repo, branch, base, title, body)`:
   - `POST /repos/{owner}/{repo}/pulls` with `draft: true`.
   - On 422 with "already exists" message → raise `GitHubConflictError`.
   - Return `response["html_url"]`.
7. Implement `_apply_diff_and_push` private helper (worktree-less path):
   - Uses `tempfile.TemporaryDirectory`.
   - `git clone`, `git checkout -b`, `git apply`, `git commit`, `git push` —
     all via `run_command`.
   - Called from the route handler, not from `push_branch` directly.
8. Add module-level `_LOGGER`. Log at INFO: operation names (no token values).

**Estimated size:** ~250 lines.

---

### Task 2 — `app/integrations/pr_body.py`

1. Implement `build_pr_title(issue_id, symptom) -> str`:
   - `f"Fix {issue_id}: {symptom}"` truncated to 255 chars.
2. Implement `build_pr_body(issue_id, symptom, verified_cause, evidence, rejected_hypotheses, changed_files, risk) -> str`:
   - Uses a plain f-string template (no external templating library).
   - Evidence items rendered as Markdown checklist: `- ✓ {item}`.
   - Rejected hypotheses rendered as: `- {summary} — {rejection_reason}`.
   - `changed_files` as a backtick-fenced list.
   - Ends with `> Human review required before merge.`.
3. Both functions are pure — no I/O, no imports from `app.*` except standard
   library.

**Estimated size:** ~60 lines.

---

### Task 3 — Route handler replacement (`app/routers/investigations.py`)

1. Add imports: `GitHubClient`, `GitHubClientError`, `build_pr_body`,
   `build_pr_title`, `JiraClient`, `JiraClientError`.
2. Add `_parse_github_repo(repository: str) -> tuple[str, str]` helper.
3. Add `_apply_diff_and_push(patch, inv, client) -> str` helper (returns the
   PR URL). This is the worktree-less push + draft PR creation sequence.
   Wrapped to keep the route handler readable.
4. Edit `create_pull_request()`:
   - Keep: approval gate (409), idempotency check, load patch logic.
   - Replace: the stub URL block with:
     ```python
     if not settings.github_token:
         # Dev / demo mode — return stub as before
         pr_url = f"https://github.com/placeholder/..."
     else:
         pr_url = await _apply_diff_and_push(patch, inv, GitHubClient())
     ```
   - Persist `pr_url` on `patch`, commit.
   - Fire Jira PR link comment (suppress exceptions).
5. The existing CP-09 tests (`test_api_pr.py`) must continue to pass — the
   stub path is still active when `GITHUB_TOKEN` is empty (the default in
   tests).

**Edited lines:** ~60 added, ~20 replaced.

---

### Task 4 — `tests/test_github_client.py`

All HTTP calls and `run_command` mocked.

| Test | What it asserts |
|------|----------------|
| `test_push_branch_ok` | `run_command` called with `git push <url> branch:branch`; token NOT in returned value |
| `test_push_branch_auth_failure` | stderr containing "401" → `GitHubAuthError` |
| `test_push_branch_repo_not_found` | stderr containing "not found" → `GitHubNotFoundError` |
| `test_push_branch_other_failure` | non-zero exit, other stderr → `GitHubClientError` |
| `test_push_branch_token_not_in_log` | caplog never contains the token string |
| `test_create_draft_pr_ok` | POST called with correct payload (`draft=True`); returns `html_url` |
| `test_create_draft_pr_conflict` | 422 "already exists" → `GitHubConflictError` |
| `test_create_draft_pr_auth` | 401 → `GitHubAuthError` |
| `test_create_draft_pr_not_found` | 404 → `GitHubNotFoundError` |
| `test_get_or_create_ref_creates_new` | 404 on GET → POST called |
| `test_get_or_create_ref_updates_existing` | 200 on GET → PATCH called |
| `test_token_not_in_log` | caplog at DEBUG never contains token |

---

### Task 5 — `tests/test_pr_body.py`

Pure unit tests, no mocking needed.

| Test | What it asserts |
|------|----------------|
| `test_build_pr_title_basic` | Contains issue_id and symptom |
| `test_build_pr_title_truncation` | Very long symptom truncated to ≤255 chars |
| `test_build_pr_body_contains_verified_cause` | verified_cause in output |
| `test_build_pr_body_evidence_list` | Each evidence item appears |
| `test_build_pr_body_rejected_hypotheses` | Rejected summary + reason appear |
| `test_build_pr_body_no_rejected_hypotheses` | "None" or equivalent when list is empty |
| `test_build_pr_body_changed_files` | File names appear |
| `test_build_pr_body_human_review_notice` | "Human review required" in output |
| `test_build_pr_body_all_fields_present` | Template has no unfilled placeholders |

---

### Task 6 — `tests/test_api_pr.py` additions

New tests added to the existing file. Existing CP-09 tests are **not
modified** — they continue to exercise the stub path (no `GITHUB_TOKEN` set).

| Test | What it asserts |
|------|----------------|
| `test_pull_request_calls_github_when_token_set` | With `GITHUB_TOKEN` patched, `GitHubClient` is called; real `pr_url` returned |
| `test_pull_request_github_failure_returns_503` | `GitHubClientError` → HTTP 503 |
| `test_pull_request_github_auth_failure_returns_503` | `GitHubAuthError` → HTTP 503 |
| `test_pull_request_idempotent_with_real_url` | Second call returns same cached URL without re-calling GitHub |
| `test_pull_request_token_not_in_response_body` | Token string absent from JSON response |
| `test_parse_github_repo_formats` | Various repo URL formats parsed to correct (owner, repo) |
| `test_parse_github_repo_invalid` | Unrecognisable string raises `ValueError` |

---

## 7. Data Flow

```
POST /api/investigations/{id}/pull-request
        │
        ▼
 Load investigation (assert APPROVED)
        │
        ▼
 Load winning Patch (from diagnosis.selected_hypothesis_id)
        │
        ├─ patch.pr_url already set? → return cached (idempotent)
        │
        ├─ settings.github_token empty?
        │    └─ return stub URL   (dev / demo mode — no real GitHub call)
        │
        ▼
 _parse_github_repo(investigation.repository) → (owner, repo)
        │
        ▼
 _apply_diff_and_push(patch, inv, GitHubClient())
    ├─ tempfile.TemporaryDirectory()
    ├─ git clone https://{token}@github.com/{owner}/{repo}.git  ./repo
    ├─ git checkout -b {branch}
    ├─ git apply  <patch.diff written to temp file>
    ├─ git commit -am "TraceLab fix: {issue_id}"
    ├─ git push   https://{token}@github.com/{owner}/{repo}.git  {branch}
    ├─ git rev-parse HEAD  → commit SHA
    └─ GitHubClient.create_draft_pr(owner, repo, branch, base, title, body)
            POST /repos/{owner}/{repo}/pulls  { draft: true, ... }
            → html_url
        │
        ▼
 Persist patch.pr_url = html_url
        │
        ▼
 Jira comment: "Draft PR created: {html_url}"  (suppress exceptions)
        │
        ▼
 Return PullRequestResponse(investigation_id, pr_url, branch)
```

---

## 8. Acceptance Criteria

| # | Criterion |
|---|-----------|
| AC-1 | Without prior approval → 409 Conflict (unchanged from CP-09). |
| AC-2 | With approval + `GITHUB_TOKEN` set → real GitHub draft PR created; `pr_url` is a `github.com/…/pull/…` URL. |
| AC-3 | With approval + `GITHUB_TOKEN` empty → stub URL returned (dev mode, no regression). |
| AC-4 | `pr_url` persisted on `Patch` row and returned in response. |
| AC-5 | Second call with same approved investigation returns the cached `pr_url` without re-calling GitHub. |
| AC-6 | `GitHubClientError` (push or PR creation failure) → HTTP 503 with descriptive error. |
| AC-7 | `GITHUB_TOKEN` never appears in any log line or response body. |
| AC-8 | `pytest backend/tests/test_github_client.py backend/tests/test_pr_body.py` all green. |
| AC-9 | Existing `test_api_pr.py` tests (CP-09) still green — no regressions. |
| AC-10 | `ruff check backend/app` passes with no new violations. |

---

## 9. Security Checklist

- [ ] `settings.github_token` read only in `GitHubClient.__init__` — stored as
      `_token` private attribute; never passed to agent prompts, never logged.
- [ ] Token injected into remote URL string inside `push_branch` — the URL is
      never stored in a DB column, never logged, never returned in any response.
- [ ] `run_command` does not log its `command` list (already the case); the
      function's debug log line must not include the full argv when it contains
      a credential URL.
- [ ] Temp directories used for worktree-less push are deleted in `finally`
      blocks — leaked credentials on disk are a security risk.
- [ ] The `PullRequestResponse` contains `pr_url` (a public GitHub URL) but
      NOT the token or any secret.
- [ ] Jira comment added after PR creation contains only the PR URL, not the
      token.

---

## 10. Edge Cases and Error Handling

| Scenario | Handling |
|----------|----------|
| `investigation.repository` is a local path (no GitHub host) | `_parse_github_repo` raises `ValueError` → `GitHubClientError` → 503 |
| `Patch.diff` is empty | `git apply` fails (no changes) → `GitHubClientError` → 503 |
| Branch already exists on GitHub | `git push` fails; `force=False` → `GitHubConflictError` → 503 with "branch already exists" detail |
| `diagnosis` is `None` (no winner) | No `patch` to push → return 422 with "No verified hypothesis" detail |
| `git clone` times out (120s limit) | `run_command` returns `timed_out=True` → `GitHubClientError` |
| GitHub rate limit (HTTP 429) | `GitHubClientError` with rate-limit detail |

---

## 11. File Change Summary

```
backend/
  app/
    config.py                           EDIT — add github_api_url
    integrations/
      github_client.py                  NEW  (~250 lines)
      pr_body.py                        NEW  (~60 lines)
    routers/
      investigations.py                 EDIT — replace stub, add helpers (~60 new lines)
  tests/
    test_github_client.py               NEW  (~200 lines)
    test_pr_body.py                     NEW  (~80 lines)
    test_api_pr.py                      EDIT — add 7 new tests (~120 lines)
.env.example                            EDIT — add GITHUB_API_URL
```

**Total new lines:** ~590. **Edited lines:** ~200.
