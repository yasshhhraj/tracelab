"""
Tests for CP-09: pull-request approval gate.

Acceptance criteria:
    AC-PR-1  POST /pull-request without prior approve → 409 Conflict
    AC-PR-2  POST /pull-request after approve → 200 with pr_url
    AC-PR-3  POST /pull-request on CREATED investigation → 409
    AC-PR-4  POST /pull-request on REJECTED investigation → 409
    AC-PR-5  POST /pull-request on WAITING_FOR_REVIEW investigation → 409
    AC-PR-6  POST /pull-request on unknown id → 404
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import (
    Diagnosis as DiagnosisORM,
)
from app.db.models import (
    Hypothesis as HypothesisORM,
)
from app.db.models import (
    HypothesisStatus,
    InvestigationStatus,
)
from app.db.models import (
    Investigation as InvestigationORM,
)
from app.db.models import (
    Patch as PatchORM,
)
from app.schemas.bug_context import BugContext

# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_investigation(db_engine: AsyncEngine, status: str) -> str:
    """Create an investigation in the given status and return its id."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    bug_ctx = BugContext(
        issue_id="PR-001",
        symptom="test",
        expected="ok",
        actual="fail",
        affected_area="area",
        error_type="deterministic",
        known_evidence=[],
        repository="/tmp/repo",
        base_branch="main",
    )
    async with factory() as session:
        inv = InvestigationORM(
            external_issue_id="PR-001",
            repository="/tmp/repo",
            base_branch="main",
            status=status,
            bug_context=bug_ctx.model_dump(),
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        return inv.id


async def _attach_diagnosis_and_patch(db_engine: AsyncEngine, inv_id: str) -> tuple[str, str]:
    """Add a hypothesis, diagnosis, and patch; return (hypothesis_id, patch_branch)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        hyp = HypothesisORM(
            investigation_id=inv_id,
            agent_type="code_path",
            summary="Idempotency race",
            reasoning_summary="Concurrent inserts race.",
            candidate_fix="Add unique constraint.",
            suspected_files=["review_service.py"],
            reproduction_plan=["step 1"],
            confidence="high",
            status=HypothesisStatus.VERIFIED,
        )
        session.add(hyp)
        await session.flush()

        diagnosis = DiagnosisORM(
            investigation_id=inv_id,
            selected_hypothesis_id=hyp.id,
            summary="Race condition in review service.",
            verified_cause="Missing DB-level uniqueness constraint.",
            evidence=["pre-fix FAIL", "post-fix PASS"],
            rejected_hypotheses=[],
            changed_files=["review_service.py"],
            risk="low",
            recommended_action="Apply unique constraint migration.",
        )
        session.add(diagnosis)
        await session.flush()

        patch = PatchORM(
            hypothesis_id=hyp.id,
            branch="ai-debug/PR-001-h1",
            diff="--- a/review_service.py\n+++ b/review_service.py\n",
            files_changed=["review_service.py"],
        )
        session.add(patch)
        await session.commit()
        return hyp.id, patch.branch


# ── AC-PR-1  409 when no approve was called ───────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_without_approve_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """AC-PR-1: POST /pull-request on a WAITING_FOR_REVIEW inv → 409."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert response.status_code == 409
    assert "APPROVED" in response.json()["detail"]


# ── AC-PR-2  200 when approved ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_after_approve_returns_200(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """AC-PR-2: approve then pull-request → 200 with pr_url."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    await _attach_diagnosis_and_patch(db_engine, inv_id)

    # Approve first
    approve_resp = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == InvestigationStatus.APPROVED

    # Then create PR through the configured GitHub path.
    with (
        patch("app.routers.investigations.settings.github_token", "test-token"),
        patch(
            "app.routers.investigations._create_github_pr",
            new_callable=AsyncMock,
            return_value="https://github.com/org/repo/pull/12",
        ),
    ):
        pr_resp = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert pr_resp.status_code == 200
    data = pr_resp.json()
    assert data["investigation_id"] == inv_id
    assert data["pr_url"]
    assert data["branch"] == "ai-debug/PR-001-h1"


# ── AC-PR-3  409 on CREATED status ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_on_created_investigation_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """AC-PR-3: CREATED investigation → 409."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.CREATED)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert response.status_code == 409


# ── AC-PR-4  409 on REJECTED status ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_on_rejected_investigation_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """AC-PR-4: REJECTED investigation → 409."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.REJECTED)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert response.status_code == 409


# ── AC-PR-5  PR url is stored in patch after creation ────────────────────────


@pytest.mark.asyncio
async def test_pull_request_stores_pr_url_in_patch(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """PR creation persists the real GitHub PR URL on the Patch row."""
    from sqlalchemy import select

    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    hyp_id, branch = await _attach_diagnosis_and_patch(db_engine, inv_id)

    await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    with (
        patch("app.routers.investigations.settings.github_token", "test-token"),
        patch(
            "app.routers.investigations._create_github_pr",
            new_callable=AsyncMock,
            return_value="https://github.com/org/repo/pull/13",
        ),
    ):
        pr_resp = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert pr_resp.status_code == 200
    returned_pr_url = pr_resp.json()["pr_url"]

    # Verify it was persisted on the Patch row
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(select(PatchORM).where(PatchORM.hypothesis_id == hyp_id))
        patch_row = result.scalar_one()
    assert patch_row.pr_url == returned_pr_url


# ── AC-PR-6  404 on unknown id ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_unknown_investigation_returns_404(client_with_db: AsyncClient):
    """AC-PR-6: unknown investigation id → 404."""
    response = await client_with_db.post("/api/investigations/no-such-id/pull-request")
    assert response.status_code == 404


# ── Second approve call is idempotent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_approve_returns_409(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Approving an already-APPROVED investigation returns 409 (not WAITING_FOR_REVIEW)."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    second = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert second.status_code == 409


# ══════════════════════════════════════════════════════════════════════════════
# CP-12 tests — real GitHub integration path
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def isolate_external_integrations(monkeypatch):
    """PR route tests must not use developer credentials from backend/.env."""
    from app.integrations.jira_client import JiraClient
    from app.routers.investigations import settings

    monkeypatch.setattr(settings, "github_token", "")
    monkeypatch.setattr(JiraClient, "add_comment", AsyncMock())


# ── helpers ───────────────────────────────────────────────────────────────────


async def _seed_approved_investigation_with_patch(
    db_engine,
) -> tuple[str, str, str]:
    """
    Create an investigation in APPROVED status with a diagnosis + patch.
    Uses a github.com-format repository URL so _parse_github_repo succeeds.
    Returns (inv_id, hyp_id, patch_branch).
    """
    # Create with a GitHub-format repo so the CP-12 path doesn't 422
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    bug_ctx = BugContext(
        issue_id="PR-001",
        symptom="test symptom",
        expected="ok",
        actual="fail",
        affected_area="area",
        error_type="deterministic",
        known_evidence=[],
        repository="github.com/org/repo",
        base_branch="main",
    )
    async with factory() as session:
        inv = InvestigationORM(
            external_issue_id="PR-001",
            repository="github.com/org/repo",
            base_branch="main",
            status=InvestigationStatus.WAITING_FOR_REVIEW,
            bug_context=bug_ctx.model_dump(),
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        inv_id = inv.id

    hyp_id, branch = await _attach_diagnosis_and_patch(db_engine, inv_id)

    # Approve it
    async with factory() as session:
        inv = await session.get(InvestigationORM, inv_id)
        inv.status = InvestigationStatus.APPROVED
        await session.commit()

    return inv_id, hyp_id, branch


# ── _parse_github_repo unit tests (no DB needed) ──────────────────────────────


@pytest.mark.parametrize(
    "repo_str, expected",
    [
        ("github.com/org/repo", ("org", "repo")),
        ("https://github.com/org/repo", ("org", "repo")),
        ("https://github.com/org/repo.git", ("org", "repo")),
        ("org/repo", ("org", "repo")),
        ("org/repo.git", ("org", "repo")),
    ],
)
def test_parse_github_repo_formats(repo_str, expected):
    """Various repository string formats are parsed to (owner, repo)."""
    from app.routers.investigations import _parse_github_repo

    assert _parse_github_repo(repo_str) == expected


def test_parse_github_repo_invalid():
    """An unrecognisable string raises ValueError."""
    from app.routers.investigations import _parse_github_repo

    with pytest.raises(ValueError):
        _parse_github_repo("/not-a-repo")

    with pytest.raises(ValueError):
        _parse_github_repo("just-one-segment")

    with pytest.raises(ValueError):
        _parse_github_repo("https://gitlab.com/org/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/org/repo.git",
        "git@github.com:org/repo.git",
    ],
)
async def test_resolve_local_repository_from_github_origin(tmp_path, origin):
    """A local investigation checkout uses its GitHub origin for PR creation."""
    from types import SimpleNamespace

    from app.routers.investigations import _resolve_github_repo

    with patch("app.routers.investigations.run_command", new_callable=AsyncMock) as git:
        git.return_value = SimpleNamespace(exit_code=0, timed_out=False, stdout=origin)
        assert await _resolve_github_repo(str(tmp_path)) == ("org", "repo")
        git.assert_awaited_once_with(["git", "remote", "get-url", "origin"], cwd=tmp_path)


# ── CP-12 route integration tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_calls_github_when_token_set(client_with_db: AsyncClient, db_engine):
    """With GITHUB_TOKEN configured, GitHubClient is called and a real URL is returned."""
    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)
    expected_pr_url = "https://github.com/org/repo/pull/99"

    with (
        patch("app.routers.investigations.settings.github_token", "ghp_test_token"),
        patch("app.routers.investigations.GitHubClient") as MockGH,
        patch("app.routers.investigations.contextlib.suppress", return_value=_NullContext()),
    ):
        mock_instance = MockGH.return_value
        mock_instance.clone_apply_and_push = AsyncMock(return_value="abc123")
        mock_instance.create_draft_pr = AsyncMock(return_value=expected_pr_url)

        response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 200
    data = response.json()
    assert data["pr_url"] == expected_pr_url
    mock_instance.create_draft_pr.assert_called_once()


@pytest.mark.asyncio
async def test_pull_request_github_failure_returns_503(client_with_db: AsyncClient, db_engine):
    """GitHubClientError during PR creation → 503."""
    from app.integrations.github_client import GitHubClientError as _GHErr

    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)

    with (
        patch("app.routers.investigations.settings.github_token", "ghp_test_token"),
        patch("app.routers.investigations.GitHubClient") as MockGH,
    ):
        mock_instance = MockGH.return_value
        mock_instance.clone_apply_and_push = AsyncMock(side_effect=_GHErr("push failed"))

        response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 503
    assert "GitHub error" in response.json()["detail"]


@pytest.mark.asyncio
async def test_pull_request_github_auth_failure_returns_503(client_with_db: AsyncClient, db_engine):
    """GitHubAuthError → 503."""
    from app.integrations.github_client import GitHubAuthError as _GHAuthErr

    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)

    with (
        patch("app.routers.investigations.settings.github_token", "bad_token"),
        patch("app.routers.investigations.GitHubClient") as MockGH,
    ):
        mock_instance = MockGH.return_value
        mock_instance.clone_apply_and_push = AsyncMock(side_effect=_GHAuthErr("auth failed"))

        response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_pull_request_idempotent_with_real_url(client_with_db: AsyncClient, db_engine):
    """Second call with a real cached pr_url returns it without re-calling GitHub."""
    real_url = "https://github.com/org/repo/pull/77"
    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)

    # Manually inject the pr_url to simulate a previously created PR
    from sqlalchemy import select as _select

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            _select(PatchORM).where(PatchORM.branch == "ai-debug/PR-001-h1")
        )
        patch_row = result.scalar_one()
        patch_row.pr_url = real_url
        await session.commit()

    with (
        patch("app.routers.investigations.settings.github_token", "ghp_test_token"),
        patch("app.routers.investigations.GitHubClient") as MockGH,
    ):
        response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 200
    assert response.json()["pr_url"] == real_url
    # GitHub client must not have been instantiated for a cached URL
    MockGH.assert_not_called()


@pytest.mark.asyncio
async def test_pull_request_token_not_in_response_body(client_with_db: AsyncClient, db_engine):
    """The GitHub token must not appear anywhere in the JSON response."""
    secret_token = "ghp_VERY_SECRET_TOKEN_MUST_NOT_APPEAR"
    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)

    with (
        patch("app.routers.investigations.settings.github_token", secret_token),
        patch("app.routers.investigations.GitHubClient") as MockGH,
        patch("app.routers.investigations.contextlib.suppress", return_value=_NullContext()),
    ):
        mock_instance = MockGH.return_value
        mock_instance.clone_apply_and_push = AsyncMock(return_value="sha123")
        mock_instance.create_draft_pr = AsyncMock(return_value="https://github.com/org/repo/pull/1")

        response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    import json as _json

    body_str = _json.dumps(response.json())
    assert secret_token not in body_str


@pytest.mark.asyncio
async def test_approved_investigation_without_verified_patch_returns_409(
    client_with_db: AsyncClient, db_engine
):
    """Approval alone cannot produce a placeholder PR URL."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.APPROVED)

    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 409
    assert "verified hypothesis" in response.json()["detail"]


@pytest.mark.asyncio
async def test_verified_patch_without_github_token_returns_503(
    client_with_db: AsyncClient, db_engine
):
    """A real PR requires a configured GitHub token."""
    inv_id, _, _ = await _seed_approved_investigation_with_patch(db_engine)

    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")

    assert response.status_code == 503
    assert "GITHUB_TOKEN" in response.json()["detail"]


# ── helper context manager for suppressing contextlib.suppress in tests ───────


class _NullContext:
    """A context manager that does nothing (replaces contextlib.suppress in tests)."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return True  # suppress all exceptions, like contextlib.suppress
