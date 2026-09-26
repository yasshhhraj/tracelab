"""
Tests for CP-09: Full REST API lifecycle.

Covers:
    - approve / reject endpoints and their status enforcement
    - GET /events endpoint
    - GET /api/hypotheses/{id}/evidence endpoint
    - Updated stubs that were 501 in CP-06 now return proper status codes
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import (
    AgentEvent as AgentEventORM,
)
from app.db.models import (
    Experiment as ExperimentORM,
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
from app.db.models import (
    TestEvidence as TestEvidenceORM,
)
from app.schemas.bug_context import BugContext

# ── helpers ───────────────────────────────────────────────────────────────────


async def _make_investigation(db_engine: AsyncEngine, status: str) -> str:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    bug_ctx = BugContext(
        issue_id="CP09-001",
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
            external_issue_id="CP09-001",
            repository="/tmp/repo",
            base_branch="main",
            status=status,
            bug_context=bug_ctx.model_dump(),
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        return inv.id


async def _make_hypothesis(db_engine: AsyncEngine, inv_id: str) -> str:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        hyp = HypothesisORM(
            investigation_id=inv_id,
            agent_type="code_path",
            summary="test hypothesis",
            reasoning_summary="some reasoning",
            candidate_fix="some fix",
            suspected_files=["app.py"],
            reproduction_plan=["run test"],
            confidence="medium",
            status=HypothesisStatus.PROPOSED,
        )
        session.add(hyp)
        await session.commit()
        await session.refresh(hyp)
        return hyp.id


async def _seed_evidence(db_engine: AsyncEngine, hyp_id: str, inv_id: str) -> None:
    """Seed one Experiment, one TestEvidence, one Patch, and one AgentEvent."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ExperimentORM(
                hypothesis_id=hyp_id,
                command="pytest tests/",
                working_directory="/tmp/repo",
                exit_code=1,
                stdout="FAILED",
                stderr="",
                duration_ms=500,
                timed_out=False,
            )
        )
        session.add(
            TestEvidenceORM(
                hypothesis_id=hyp_id,
                test_path="tests/test_app.py",
                pre_fix_result="FAIL",
                post_fix_result="PASS",
                existing_suite_result="PASS",
                runs=1,
                failures_before=1,
                failures_after=0,
            )
        )
        session.add(
            PatchORM(
                hypothesis_id=hyp_id,
                branch="ai-debug/CP09-001-h1",
                diff="--- a/app.py\n+++ b/app.py\n",
                files_changed=["app.py"],
            )
        )
        session.add(
            AgentEventORM(
                investigation_id=inv_id,
                agent="code_path",
                action="search_code",
                target="app.py",
                payload={"query": "race condition"},
            )
        )
        await session.commit()


# ── approve endpoint ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_transitions_status(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """POST /approve on WAITING_FOR_REVIEW → 200 with APPROVED status."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == inv_id
    assert data["status"] == InvestigationStatus.APPROVED


@pytest.mark.asyncio
async def test_approve_persists_in_db(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Approved status is visible in subsequent GET."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    await client_with_db.post(f"/api/investigations/{inv_id}/approve")

    get_resp = await client_with_db.get(f"/api/investigations/{inv_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == InvestigationStatus.APPROVED


@pytest.mark.asyncio
async def test_approve_unknown_investigation_returns_404(client_with_db: AsyncClient):
    resp = await client_with_db.post("/api/investigations/not-real/approve")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_wrong_status_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """Approving a CREATED investigation returns 409."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.CREATED)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert resp.status_code == 409
    assert "WAITING_FOR_REVIEW" in resp.json()["detail"]


# ── reject endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_transitions_status(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """POST /reject on WAITING_FOR_REVIEW → 200 with REJECTED status."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/reject")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == inv_id
    assert data["status"] == InvestigationStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_persists_in_db(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Rejected status is visible in subsequent GET."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    await client_with_db.post(f"/api/investigations/{inv_id}/reject")

    get_resp = await client_with_db.get(f"/api/investigations/{inv_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == InvestigationStatus.REJECTED


@pytest.mark.asyncio
async def test_reject_unknown_investigation_returns_404(client_with_db: AsyncClient):
    resp = await client_with_db.post("/api/investigations/not-real/reject")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reject_wrong_status_returns_409(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """Rejecting a CREATED investigation returns 409."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.CREATED)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/reject")
    assert resp.status_code == 409
    assert "WAITING_FOR_REVIEW" in resp.json()["detail"]


# ── events endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_empty(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Events endpoint returns empty list when no events exist."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.CREATED)
    resp = await client_with_db.get(f"/api/investigations/{inv_id}/events")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_events_returns_events(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Events endpoint returns seeded AgentEvent rows."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.INVESTIGATING)
    hyp_id = await _make_hypothesis(db_engine, inv_id)
    await _seed_evidence(db_engine, hyp_id, inv_id)

    resp = await client_with_db.get(f"/api/investigations/{inv_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["agent"] == "code_path"
    assert events[0]["action"] == "search_code"
    assert events[0]["target"] == "app.py"
    assert events[0]["payload"] == {"query": "race condition"}


@pytest.mark.asyncio
async def test_get_events_unknown_investigation_returns_404(client_with_db: AsyncClient):
    resp = await client_with_db.get("/api/investigations/no-such-id/events")
    assert resp.status_code == 404


# ── evidence endpoint (GET /api/hypotheses/{id}/evidence) ────────────────────


@pytest.mark.asyncio
async def test_get_evidence_returns_all_components(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """GET /api/hypotheses/{id}/evidence returns experiments, test_evidence, patches."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.VERIFYING)
    hyp_id = await _make_hypothesis(db_engine, inv_id)
    await _seed_evidence(db_engine, hyp_id, inv_id)

    resp = await client_with_db.get(f"/api/hypotheses/{hyp_id}/evidence")
    assert resp.status_code == 200
    data = resp.json()

    # hypothesis embedded
    assert data["hypothesis"]["id"] == hyp_id
    assert data["hypothesis"]["agent_type"] == "code_path"

    # experiments
    assert len(data["experiments"]) == 1
    assert data["experiments"][0]["command"] == "pytest tests/"
    assert data["experiments"][0]["exit_code"] == 1

    # test_evidence
    assert len(data["test_evidence"]) == 1
    te = data["test_evidence"][0]
    assert te["pre_fix_result"] == "FAIL"
    assert te["post_fix_result"] == "PASS"
    assert te["existing_suite_result"] == "PASS"

    # patches
    assert len(data["patches"]) == 1
    assert data["patches"][0]["branch"] == "ai-debug/CP09-001-h1"


@pytest.mark.asyncio
async def test_get_evidence_unknown_hypothesis_returns_404(client_with_db: AsyncClient):
    resp = await client_with_db.get("/api/hypotheses/no-such-id/evidence")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_evidence_empty_hypothesis_returns_empty_lists(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """A hypothesis with no experiments/evidence/patches returns empty lists."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.INVESTIGATING)
    hyp_id = await _make_hypothesis(db_engine, inv_id)

    resp = await client_with_db.get(f"/api/hypotheses/{hyp_id}/evidence")
    assert resp.status_code == 200
    data = resp.json()
    assert data["experiments"] == []
    assert data["test_evidence"] == []
    assert data["patches"] == []


# ── old stub tests updated: 501 → real codes ─────────────────────────────────


@pytest.mark.asyncio
async def test_approve_stub_no_longer_returns_501(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """approve endpoint is now implemented (CP-09) — 501 is gone."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert resp.status_code != 501


@pytest.mark.asyncio
async def test_reject_stub_no_longer_returns_501(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """reject endpoint is now implemented (CP-09) — 501 is gone."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/reject")
    assert resp.status_code != 501


@pytest.mark.asyncio
async def test_pull_request_stub_no_longer_returns_501(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """pull-request endpoint is now implemented (CP-09) — 501 is gone."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.WAITING_FOR_REVIEW)
    # Without approval it should be 409, not 501
    resp = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_events_stub_no_longer_returns_501(
    client_with_db: AsyncClient, db_engine: AsyncEngine
):
    """events endpoint is now implemented (CP-09) — 501 is gone."""
    inv_id = await _make_investigation(db_engine, InvestigationStatus.CREATED)
    resp = await client_with_db.get(f"/api/investigations/{inv_id}/events")
    assert resp.status_code == 200
