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

    # Then create PR
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
    """PR creation persists the stub pr_url on the Patch row."""
    from sqlalchemy import select

    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    hyp_id, branch = await _attach_diagnosis_and_patch(db_engine, inv_id)

    await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    pr_resp = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert pr_resp.status_code == 200
    returned_pr_url = pr_resp.json()["pr_url"]

    # Verify it was persisted on the Patch row
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            select(PatchORM).where(PatchORM.hypothesis_id == hyp_id)
        )
        patch = result.scalar_one()
    assert patch.pr_url == returned_pr_url


# ── AC-PR-6  404 on unknown id ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_request_unknown_investigation_returns_404(client_with_db: AsyncClient):
    """AC-PR-6: unknown investigation id → 404."""
    response = await client_with_db.post("/api/investigations/no-such-id/pull-request")
    assert response.status_code == 404


# ── Second approve call is idempotent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_second_approve_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """Approving an already-APPROVED investigation returns 409 (not WAITING_FOR_REVIEW)."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    second = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert second.status_code == 409
