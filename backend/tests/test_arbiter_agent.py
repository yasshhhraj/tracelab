"""Evidence and persistence behavior for the CP-08 arbiter."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agents.arbiter_agent import ArbiterAgent
from app.db.models import (
    Diagnosis,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    InvestigationStatus,
)
from app.db.models import (
    TestEvidence as EvidenceORM,
)


async def _seed(session_factory, statuses: list[str]) -> tuple[str, list[str]]:
    async with session_factory() as session:
        investigation = Investigation(
            external_issue_id="BUG-1",
            repository="/tmp/repo",
            base_branch="main",
            status=InvestigationStatus.ARBITRATING,
            bug_context={"issue_id": "BUG-1", "symptom": "duplicate writes"},
        )
        session.add(investigation)
        await session.flush()
        hypotheses = []
        for index, status in enumerate(statuses):
            hyp = Hypothesis(
                investigation_id=investigation.id,
                agent_type=["code_path", "git_history", "test_behavior"][index],
                summary=f"Cause {index}",
                reasoning_summary="causal explanation",
                candidate_fix="diff",
                suspected_files=[f"file{index}.py"],
                reproduction_plan=["tests/test_bug.py"],
                confidence="low" if status == HypothesisStatus.VERIFIED else "high",
                status=status,
            )
            session.add(hyp)
            hypotheses.append(hyp)
        await session.flush()
        for hyp in hypotheses:
            session.add(
                EvidenceORM(
                    hypothesis_id=hyp.id,
                    test_path="tests/test_bug.py",
                    pre_fix_result="FAIL",
                    post_fix_result="PASS",
                    existing_suite_result="PASS",
                )
            )
        await session.commit()
        return investigation.id, [hyp.id for hyp in hypotheses]


def _response(selected_id: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "emit_diagnosis",
                                "arguments": json.dumps(
                                    {
                                        "selected_hypothesis_id": selected_id,
                                        "summary": "The selected cause explains the bug.",
                                        "verified_cause": "Atomicity defect",
                                        "evidence": [
                                            "Regression fails before and passes after the patch"
                                        ],
                                        "rejected_hypotheses": [],
                                        "changed_files": ["invented.py"],
                                        "risk": "low",
                                        "recommended_action": "Review candidate patch.",
                                    }
                                ),
                            }
                        }
                    ]
                }
            }
        ]
    }


@pytest.mark.asyncio
async def test_verified_winner_uses_proof_and_persists_report(session_factory):
    investigation_id, ids = await _seed(
        session_factory, [HypothesisStatus.VERIFIED, HypothesisStatus.REJECTED]
    )
    arbiter = ArbiterAgent()
    captured = []

    async def chat(messages, tools):
        captured.extend(messages)
        return _response(ids[0])

    with (
        patch.object(arbiter, "_chat", side_effect=chat) as mock_chat,
        patch("app.agents.arbiter_agent.settings.llm_api_key", "sk-test-ARBITER-SECRET"),
    ):
        report = await arbiter.run(investigation_id, session_factory)
    assert mock_chat.call_count == 1
    assert report.selected_hypothesis_id == ids[0]
    assert report.changed_files == ["file0.py"]
    assert [r.hypothesis_id for r in report.rejected_hypotheses] == [ids[1]]
    assert "confidence" not in json.dumps(captured).lower()
    assert "sk-test-ARBITER-SECRET" not in json.dumps(captured)

    async with session_factory() as session:
        stored = (
            await session.execute(
                select(Diagnosis).where(Diagnosis.investigation_id == investigation_id)
            )
        ).scalar_one()
        assert stored.selected_hypothesis_id == ids[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses",
    [
        [HypothesisStatus.REJECTED, HypothesisStatus.REJECTED],
        [HypothesisStatus.BLOCKED, HypothesisStatus.BLOCKED],
        [HypothesisStatus.REJECTED, HypothesisStatus.BLOCKED],
    ],
)
async def test_no_verified_hypothesis_skips_model(session_factory, statuses):
    investigation_id, ids = await _seed(session_factory, statuses)
    arbiter = ArbiterAgent()
    with patch.object(arbiter, "_chat", new=AsyncMock()) as mock_chat:
        report = await arbiter.run(investigation_id, session_factory)
    mock_chat.assert_not_called()
    assert report.selected_hypothesis_id is None
    assert "No hypothesis" in report.summary
    assert {r.hypothesis_id for r in report.rejected_hypotheses} == set(ids)


@pytest.mark.asyncio
async def test_model_cannot_select_unverified_hypothesis(session_factory):
    investigation_id, ids = await _seed(
        session_factory, [HypothesisStatus.VERIFIED, HypothesisStatus.REJECTED]
    )
    arbiter = ArbiterAgent()
    with patch.object(arbiter, "_chat", new=AsyncMock(return_value=_response(ids[1]))):
        report = await arbiter.run(investigation_id, session_factory)
    assert report.selected_hypothesis_id is None


@pytest.mark.asyncio
async def test_unproven_verified_status_is_not_eligible(session_factory):
    investigation_id, ids = await _seed(session_factory, [HypothesisStatus.VERIFIED])
    async with session_factory() as session:
        evidence = (
            await session.execute(select(EvidenceORM).where(EvidenceORM.hypothesis_id == ids[0]))
        ).scalar_one()
        evidence.post_fix_result = "FAIL"
        await session.commit()
    arbiter = ArbiterAgent()
    with patch.object(arbiter, "_chat", new=AsyncMock()) as mock_chat:
        report = await arbiter.run(investigation_id, session_factory)
    mock_chat.assert_not_called()
    assert report.selected_hypothesis_id is None


@pytest.mark.asyncio
async def test_retry_then_persist_one_diagnosis(session_factory):
    investigation_id, ids = await _seed(session_factory, [HypothesisStatus.VERIFIED])
    arbiter = ArbiterAgent()
    responses = [
        {"choices": [{"message": {"content": "thinking", "tool_calls": []}}]},
        _response(ids[0]),
    ]
    with patch.object(arbiter, "_chat", new=AsyncMock(side_effect=responses)) as chat:
        await arbiter.run(investigation_id, session_factory)
        assert chat.call_count == 2
    with patch.object(arbiter, "_chat", new=AsyncMock(return_value=_response(ids[0]))):
        await arbiter.run(investigation_id, session_factory)
    async with session_factory() as session:
        rows = (await session.execute(select(Diagnosis))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_missing_emit_call_falls_back_to_manual_review(session_factory):
    investigation_id, _ = await _seed(session_factory, [HypothesisStatus.VERIFIED])
    arbiter = ArbiterAgent()
    plain_response = {"choices": [{"message": {"content": "No structured result"}}]}
    with patch.object(arbiter, "_chat", new=AsyncMock(return_value=plain_response)) as chat:
        report = await arbiter.run(investigation_id, session_factory)
    assert chat.call_count == 2
    assert report.selected_hypothesis_id is None
    assert report.summary == "Arbiter could not produce a structured diagnosis."
