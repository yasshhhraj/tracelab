"""CP-08 integration checks for the final status and diagnosis response."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agents.arbiter_agent import ArbiterAgent
from app.db.models import Diagnosis, Investigation, InvestigationStatus
from app.orchestrator import _run_arbitration


async def _seed(session_factory, tmp_path: Path) -> str:
    async with session_factory() as session:
        investigation = Investigation(
            external_issue_id="BUG-2",
            repository=str(tmp_path),
            base_branch="main",
            status=InvestigationStatus.ARBITRATING,
            bug_context={"issue_id": "BUG-2", "symptom": "A bug"},
        )
        session.add(investigation)
        await session.commit()
        return investigation.id


@pytest.mark.asyncio
async def test_arbitration_finishes_and_persists_no_winner(session_factory, tmp_path):
    investigation_id = await _seed(session_factory, tmp_path)
    await _run_arbitration(session_factory, investigation_id)
    async with session_factory() as session:
        investigation = await session.get(Investigation, investigation_id)
        assert investigation.status == InvestigationStatus.WAITING_FOR_REVIEW
        assert investigation.completed_at is not None
        diagnosis = (
            await session.execute(
                select(Diagnosis).where(Diagnosis.investigation_id == investigation_id)
            )
        ).scalar_one()
        assert diagnosis.selected_hypothesis_id is None


@pytest.mark.asyncio
async def test_arbitration_failure_marks_investigation_failed(session_factory, tmp_path):
    investigation_id = await _seed(session_factory, tmp_path)
    with patch.object(ArbiterAgent, "run", new=AsyncMock(side_effect=RuntimeError("model failed"))):
        await _run_arbitration(session_factory, investigation_id)
    async with session_factory() as session:
        investigation = await session.get(Investigation, investigation_id)
        assert investigation.status == InvestigationStatus.FAILED


@pytest.mark.asyncio
async def test_get_investigation_embeds_diagnosis(client_with_db, session_factory, tmp_path):
    investigation_id = await _seed(session_factory, tmp_path)
    await ArbiterAgent().run(investigation_id, session_factory)
    response = await client_with_db.get(f"/api/investigations/{investigation_id}")
    assert response.status_code == 200
    report = response.json()["diagnosis"]
    assert report["selected_hypothesis_id"] is None
    assert "No hypothesis" in report["summary"]
    assert "confidence" not in report
