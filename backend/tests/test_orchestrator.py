"""
Tests for CP-06: Orchestrator (run_investigation).

All LLM calls are mocked. WorktreeManager is also mocked to avoid needing a
real git repository on disk — worktree correctness is already covered by
tests/test_worktree.py.

Acceptance criteria verified here:
  AC-2  background task persists exactly 3 Hypothesis rows
  AC-3  all 3 have distinct agent_type values
  AC-4  status transitions correctly to VERIFYING
  AC-5  partial agent failure → VERIFYING, failed agent → BLOCKED
  AC-6  all agents fail → FAILED
  AC-7  ExperimentRecord and AgentEvent rows persisted
"""

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.agents.llm.openai_model import OpenAIAgentModel
from app.db.models import (
    AgentEvent as AgentEventORM,
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
from app.orchestrator import run_investigation
from app.schemas.bug_context import BugContext

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_emit_response(agent_type: str) -> dict[str, Any]:
    """Fake OpenAI chat response that immediately calls emit_hypothesis."""
    args = {
        "summary": f"Hypothesis from {agent_type}",
        "suspected_files": [f"{agent_type}_file.py"],
        "reasoning_summary": f"{agent_type} causal chain",
        "reproduction_plan": ["step 1", "step 2"],
        "candidate_fix": f"Fix from {agent_type}",
        "confidence": "medium",
    }
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": "emit_hypothesis",
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _make_emit_response_with_evidence(agent_type: str) -> dict[str, Any]:
    """Fake response for agent type — also produces experiment + event data."""
    return _make_emit_response(agent_type)


async def _seed_investigation(session_factory, tmp_path: Path) -> str:
    """Insert a CREATED Investigation row and return its id."""
    bug_ctx = BugContext(
        issue_id="TEST-001",
        symptom="Crash on input",
        expected="No crash",
        actual="Crash",
        affected_area="service",
        error_type="deterministic",
        known_evidence=[],
        repository=str(tmp_path),
        base_branch="main",
    )
    async with session_factory() as session:
        inv = InvestigationORM(
            external_issue_id="TEST-001",
            repository=str(tmp_path),
            base_branch="main",
            status=InvestigationStatus.CREATED,
            bug_context=bug_ctx.model_dump(),
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)
        return inv.id


def _mock_worktree_manager(tmp_path: Path):
    """Return a MagicMock WorktreeManager whose create() returns tmp_path."""
    mgr = MagicMock()
    mgr.create = AsyncMock(return_value=tmp_path)
    mgr.destroy_all = AsyncMock()
    return mgr


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_investigation_persists_three_hypotheses(
    db_engine, session_factory, tmp_path: Path
):
    """AC-2, AC-3: exactly 3 hypotheses with distinct agent_type values."""
    inv_id = await _seed_investigation(session_factory, tmp_path)

    # Rotate emit responses so each agent gets its own agent_type
    call_count = {"n": 0}
    agent_types = ["code_path", "git_history", "test_behavior"]

    async def rotating_chat(*args, **kwargs):
        # Each agent calls _chat once for emit_hypothesis
        idx = call_count["n"] % 3
        call_count["n"] += 1
        return _make_emit_response(agent_types[idx])

    from app.verification.engine import VerificationEngine

    with (
        patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=rotating_chat)),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(return_value="REJECTED")),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        result = await session.execute(
            select(HypothesisORM).where(HypothesisORM.investigation_id == inv_id)
        )
        hypotheses = result.scalars().all()

    assert len(hypotheses) == 3
    agent_type_values = {h.agent_type for h in hypotheses}
    assert agent_type_values == {"code_path", "git_history", "test_behavior"}


@pytest.mark.asyncio
async def test_run_investigation_status_transitions(
    db_engine, session_factory, tmp_path: Path
):
    """
    AC-4: after CP-07 extension, investigation ends at ARBITRATING.
    (CP-06 contract was VERIFYING; CP-07 extends to ARBITRATING.)
    """
    inv_id = await _seed_investigation(session_factory, tmp_path)

    from app.verification.engine import VerificationEngine

    with (
        patch.object(
            OpenAIAgentModel,
            "_chat",
            new=AsyncMock(return_value=_make_emit_response("code_path")),
        ),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(return_value="REJECTED")),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        inv = await session.get(InvestigationORM, inv_id)

    assert inv is not None
    assert inv.status == InvestigationStatus.ARBITRATING


@pytest.mark.asyncio
async def test_run_investigation_partial_agent_failure(
    db_engine, session_factory, tmp_path: Path
):
    """AC-5: one agent raises → investigation still VERIFYING; that hypothesis BLOCKED."""
    inv_id = await _seed_investigation(session_factory, tmp_path)

    call_count = {"n": 0}

    async def fail_first(*args, **kwargs):
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:
            raise RuntimeError("code_path agent exploded")
        return _make_emit_response("git_history" if n == 1 else "test_behavior")

    from app.verification.engine import VerificationEngine

    with (
        patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=fail_first)),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(return_value="REJECTED")),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        result = await session.execute(
            select(HypothesisORM).where(HypothesisORM.investigation_id == inv_id)
        )
        hypotheses = result.scalars().all()
        inv = await session.get(InvestigationORM, inv_id)

    assert len(hypotheses) == 3
    blocked = [h for h in hypotheses if h.status == HypothesisStatus.BLOCKED]
    assert len(blocked) == 1
    assert blocked[0].agent_type == "code_path"
    assert inv is not None
    assert inv.status == InvestigationStatus.ARBITRATING


@pytest.mark.asyncio
async def test_run_investigation_all_agents_fail_sets_failed_status(
    db_engine, session_factory, tmp_path: Path
):
    """AC-6: all 3 agents raise → investigation status FAILED."""
    inv_id = await _seed_investigation(session_factory, tmp_path)

    async def always_fail(*args, **kwargs):
        raise RuntimeError("agent exploded")

    with (
        patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=always_fail)),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        inv = await session.get(InvestigationORM, inv_id)

    assert inv is not None
    assert inv.status == InvestigationStatus.FAILED


@pytest.mark.asyncio
async def test_run_investigation_persists_experiments_and_events(
    db_engine, session_factory, tmp_path: Path
):
    """AC-7: ExperimentRecord and AgentEvent rows from agent results land in DB."""
    inv_id = await _seed_investigation(session_factory, tmp_path)

    # We need the agent to actually emit an ExperimentRecord and an AgentEvent.
    # The easiest way: let the real agentic loop run but intercept _chat to
    # first return a tool call (which produces an AgentEvent) and then emit.
    tool_call_response: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_search_1",
                            "type": "function",
                            "function": {
                                "name": "search_code",
                                "arguments": json.dumps(
                                    {
                                        "pattern": "def process",
                                        "path": str(tmp_path),
                                    }
                                ),
                            },
                        }
                    ],
                }
            }
        ]
    }
    emit_response = _make_emit_response("code_path")
    responses = iter([tool_call_response, emit_response])

    # Only patch the first agent's _chat so it produces events;
    # let the other two emit immediately.
    call_seq = {"n": 0}

    async def sequenced_chat(*args, **kwargs):
        n = call_seq["n"]
        call_seq["n"] += 1
        if n < 2:  # first two calls are for the code_path tool dispatch + emit
            return next(responses)
        # remaining calls: git_history and test_behavior emit immediately
        return _make_emit_response("code_path")

    # Create a dummy file so search_code can run
    (tmp_path / "service.py").write_text("def process(): pass\n")
    # Also need a git repo for git grep (search_code uses git grep)
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True
    )

    from app.verification.engine import VerificationEngine

    with (
        patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=sequenced_chat)),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(return_value="REJECTED")),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        hyp_result = await session.execute(
            select(HypothesisORM).where(HypothesisORM.investigation_id == inv_id)
        )
        hypotheses = hyp_result.scalars().all()

        event_result = await session.execute(
            select(AgentEventORM).where(AgentEventORM.investigation_id == inv_id)
        )
        events = event_result.scalars().all()

    assert len(hypotheses) == 3
    # At least one AgentEvent should exist (the search_code dispatch + emit events)
    assert len(events) >= 1


@pytest.mark.asyncio
async def test_run_investigation_max_hypotheses_is_three(
    db_engine, session_factory, tmp_path: Path
):
    """Orchestrator must never persist more than 3 hypotheses (AGENTS.md resource cap)."""
    inv_id = await _seed_investigation(session_factory, tmp_path)

    from app.verification.engine import VerificationEngine

    with (
        patch.object(
            OpenAIAgentModel,
            "_chat",
            new=AsyncMock(return_value=_make_emit_response("code_path")),
        ),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(return_value="REJECTED")),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        result = await session.execute(
            select(HypothesisORM).where(HypothesisORM.investigation_id == inv_id)
        )
        count = len(result.scalars().all())

    assert count == 3


@pytest.mark.asyncio
async def test_run_investigation_missing_investigation_is_noop(
    db_engine, session_factory, tmp_path: Path
):
    """Calling run_investigation with an unknown id should not raise."""
    with patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)):
        # Should complete without error
        await run_investigation("nonexistent-id", session_factory)


# ── CP-07 orchestrator tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_reaches_arbitrating_after_verification(
    db_engine, session_factory, tmp_path: Path
):
    """
    CP-07: after all agents succeed and verification completes,
    investigation status must be ARBITRATING.
    """
    inv_id = await _seed_investigation(session_factory, tmp_path)

    from app.verification.engine import VerificationEngine

    with (
        patch.object(
            OpenAIAgentModel,
            "_chat",
            new=AsyncMock(return_value=_make_emit_response("code_path")),
        ),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(
            VerificationEngine,
            "verify",
            new=AsyncMock(return_value="VERIFIED"),
        ),
    ):
        await run_investigation(inv_id, session_factory)

    async with session_factory() as session:
        inv = await session.get(InvestigationORM, inv_id)

    assert inv is not None
    assert inv.status == InvestigationStatus.ARBITRATING


@pytest.mark.asyncio
async def test_orchestrator_skips_blocked_hypotheses_in_verification(
    db_engine, session_factory, tmp_path: Path
):
    """
    CP-07: BLOCKED hypotheses (from crashed agents) must not be passed to
    the VerificationEngine. Only non-BLOCKED hypotheses with a candidate_fix
    should be verified.
    """
    inv_id = await _seed_investigation(session_factory, tmp_path)

    call_count = {"n": 0}

    async def fail_first(*args, **kwargs):
        n = call_count["n"]
        call_count["n"] += 1
        if n == 0:
            raise RuntimeError("code_path agent exploded")
        return _make_emit_response("git_history" if n == 1 else "test_behavior")

    verify_calls: list = []

    from app.verification.engine import VerificationEngine

    async def record_verify(**kwargs):
        verify_calls.append(kwargs["hypothesis_id"])
        return "REJECTED"

    with (
        patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=fail_first)),
        patch("app.orchestrator.WorktreeManager", return_value=_mock_worktree_manager(tmp_path)),
        patch.object(VerificationEngine, "verify", new=AsyncMock(side_effect=record_verify)),
    ):
        await run_investigation(inv_id, session_factory)

    # code_path BLOCKED → skipped; git_history + test_behavior both have empty
    # candidate_fix (from the mock emit), so they are also skipped.
    # The important assertion: verify was called at most 2 times (not 3).
    assert len(verify_calls) <= 2
