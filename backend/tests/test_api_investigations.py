"""
Tests for CP-06: Investigations REST API.

Uses client_with_db fixture (from conftest.py) which overrides the get_session
dependency with an in-memory SQLite database. The background task (run_investigation)
is patched to a no-op so tests remain fast and deterministic.

Acceptance criteria verified here:
  AC-1  POST returns 201 with id and status=CREATED immediately
  AC-9  POST with missing fields returns 422
  AC-10 GET unknown id returns 404
  + list empty, list returns items, detail, hypotheses endpoints
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

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
from app.schemas.bug_context import BugContext

# ── fixtures / helpers ────────────────────────────────────────────────────────

_VALID_PAYLOAD = {
    "jira_issue_id": "PVS-001",
    "repository": "/tmp/test-repo",
    "base_branch": "main",
    "symptom": "Duplicate rows on concurrent requests",
    "expected": "One row per key",
    "actual": "Two rows inserted",
    "affected_area": "review_service",
    "error_type": "deterministic",
    "known_evidence": ["Reproduced in staging"],
}


async def _seed_investigation(db_engine: AsyncEngine) -> str:
    """Directly insert a known Investigation row; return its id."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    bug_ctx = BugContext(
        issue_id="SEED-001",
        symptom="test symptom",
        expected="expected",
        actual="actual",
        affected_area="area",
        error_type="deterministic",
        known_evidence=[],
        repository="/tmp/repo",
        base_branch="main",
    )
    async with factory() as session:
        inv = InvestigationORM(
            external_issue_id="SEED-001",
            repository="/tmp/repo",
            base_branch="main",
            status=InvestigationStatus.CREATED,
            bug_context=bug_ctx.model_dump(),
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        return inv.id


async def _seed_hypotheses(db_engine: AsyncEngine, investigation_id: str) -> list[str]:
    """Insert 3 Hypothesis rows for an investigation; return their ids."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    agent_types = ["code_path", "git_history", "test_behavior"]
    ids: list[str] = []
    async with factory() as session:
        for at in agent_types:
            h = HypothesisORM(
                investigation_id=investigation_id,
                agent_type=at,
                summary=f"Hypothesis from {at}",
                reasoning_summary="reasoning",
                candidate_fix="fix",
                suspected_files=["file.py"],
                reproduction_plan=["step 1"],
                confidence="medium",
                status=HypothesisStatus.PROPOSED,
            )
            session.add(h)
        await session.commit()
        # Reload to get ids
        result = await session.execute(
            select(HypothesisORM).where(HypothesisORM.investigation_id == investigation_id)
        )
        ids = [h.id for h in result.scalars().all()]
    return ids


# ── POST /api/investigations ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_investigation_returns_201(client_with_db: AsyncClient):
    """AC-1: POST returns 201, id, and status=CREATED immediately."""
    with patch("app.routers.investigations.run_investigation", new=AsyncMock()):
        response = await client_with_db.post("/api/investigations", json=_VALID_PAYLOAD)

    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["status"] == "CREATED"
    assert data["external_issue_id"] == "PVS-001"
    assert data["repository"] == "/tmp/test-repo"
    assert data["base_branch"] == "main"


@pytest.mark.asyncio
async def test_create_investigation_uses_configured_repository(client_with_db: AsyncClient):
    payload = dict(_VALID_PAYLOAD)
    del payload["repository"]
    with (
        patch("app.routers.investigations.settings.target_repository", "/home/yashraj/loreforge"),
        patch("app.routers.investigations.run_investigation", new=AsyncMock()),
    ):
        response = await client_with_db.post("/api/investigations", json=payload)
    assert response.status_code == 201
    assert response.json()["repository"] == "/home/yashraj/loreforge"


@pytest.mark.asyncio
async def test_create_investigation_missing_required_field_returns_422(
    client_with_db: AsyncClient,
):
    """AC-9: Missing required field yields 422 Unprocessable Entity."""
    payload = dict(_VALID_PAYLOAD)
    del payload["symptom"]  # remove a required field

    response = await client_with_db.post("/api/investigations", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_investigation_persists_bug_context(client_with_db: AsyncClient):
    """The bug_context JSON blob is stored in the DB and returned in the response."""
    with patch("app.routers.investigations.run_investigation", new=AsyncMock()):
        response = await client_with_db.post("/api/investigations", json=_VALID_PAYLOAD)

    assert response.status_code == 201
    data = response.json()
    assert data["bug_context"] is not None
    assert data["bug_context"]["issue_id"] == "PVS-001"
    assert data["bug_context"]["symptom"] == "Duplicate rows on concurrent requests"


# ── GET /api/investigations ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_investigations_empty(client_with_db: AsyncClient):
    """Empty DB returns empty items list."""
    response = await client_with_db.get("/api/investigations")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_investigations_returns_created(client_with_db: AsyncClient, db_engine):
    """After creating an investigation, it appears in the list."""
    inv_id = await _seed_investigation(db_engine)

    response = await client_with_db.get("/api/investigations")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == inv_id


@pytest.mark.asyncio
async def test_list_investigations_pagination(client_with_db: AsyncClient, db_engine):
    """limit and offset query params are respected."""
    # Seed 3 investigations
    for _ in range(3):
        await _seed_investigation(db_engine)

    r1 = await client_with_db.get("/api/investigations?limit=2&offset=0")
    r2 = await client_with_db.get("/api/investigations?limit=2&offset=2")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(r1.json()["items"]) == 2
    assert len(r2.json()["items"]) == 1
    assert r1.json()["total"] == 3
    assert r2.json()["total"] == 3


# ── GET /api/investigations/{id} ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_investigation_detail(client_with_db: AsyncClient, db_engine):
    """GET by id returns the correct investigation."""
    inv_id = await _seed_investigation(db_engine)

    response = await client_with_db.get(f"/api/investigations/{inv_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == inv_id
    assert data["external_issue_id"] == "SEED-001"
    assert data["status"] == InvestigationStatus.CREATED


@pytest.mark.asyncio
async def test_get_investigation_not_found_returns_404(client_with_db: AsyncClient):
    """AC-10: unknown id returns 404."""
    response = await client_with_db.get("/api/investigations/does-not-exist")
    assert response.status_code == 404


# ── GET /api/investigations/{id}/hypotheses ───────────────────────────────────


@pytest.mark.asyncio
async def test_list_hypotheses_empty(client_with_db: AsyncClient, db_engine):
    """Before agents run, hypothesis list is empty."""
    inv_id = await _seed_investigation(db_engine)

    response = await client_with_db.get(f"/api/investigations/{inv_id}/hypotheses")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_hypotheses_after_seeding(client_with_db: AsyncClient, db_engine):
    """AC-8: after seeding 3 hypotheses, endpoint returns all 3."""
    inv_id = await _seed_investigation(db_engine)
    await _seed_hypotheses(db_engine, inv_id)

    response = await client_with_db.get(f"/api/investigations/{inv_id}/hypotheses")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    agent_types = {h["agent_type"] for h in data}
    assert agent_types == {"code_path", "git_history", "test_behavior"}


@pytest.mark.asyncio
async def test_list_hypotheses_for_unknown_investigation_returns_404(
    client_with_db: AsyncClient,
):
    """Hypotheses endpoint returns 404 for unknown investigation id."""
    response = await client_with_db.get("/api/investigations/no-such-id/hypotheses")
    assert response.status_code == 404


# ── CP-09 endpoints are now live (no longer stubs) ───────────────────────────
# These endpoints were 501 in CP-06; they are fully implemented in CP-09.
# Detailed acceptance tests live in test_api_cp09.py and test_api_pr.py.
# Quick smoke-checks here to ensure the CP-06 suite still passes cleanly.


@pytest.mark.asyncio
async def test_approve_requires_waiting_for_review_status(
    client_with_db: AsyncClient, db_engine
):
    """approve on a CREATED investigation returns 409 (not 501)."""
    inv_id = await _seed_investigation(db_engine)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/approve")
    assert response.status_code == 409  # CREATED is not approvable


@pytest.mark.asyncio
async def test_reject_requires_waiting_for_review_status(
    client_with_db: AsyncClient, db_engine
):
    """reject on a CREATED investigation returns 409 (not 501)."""
    inv_id = await _seed_investigation(db_engine)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/reject")
    assert response.status_code == 409  # CREATED is not rejectable


@pytest.mark.asyncio
async def test_pull_request_requires_approved_status(client_with_db: AsyncClient, db_engine):
    """pull-request enforces APPROVED gate — CREATED returns 409 (not 501)."""
    inv_id = await _seed_investigation(db_engine)
    response = await client_with_db.post(f"/api/investigations/{inv_id}/pull-request")
    assert response.status_code == 409  # CREATED is not APPROVED


@pytest.mark.asyncio
async def test_events_returns_empty_list_for_new_investigation(
    client_with_db: AsyncClient, db_engine
):
    """events endpoint is live — returns empty list for a fresh investigation."""
    inv_id = await _seed_investigation(db_engine)
    response = await client_with_db.get(f"/api/investigations/{inv_id}/events")
    assert response.status_code == 200
    assert response.json() == []
