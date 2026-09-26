"""
Tests for CP-06: GitHistoryAgent.

All tests mock OpenAIAgentModel._chat — no real LLM API calls in CI.
Pattern mirrors test_code_path_agent.py.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.git_history_agent import make_git_history_agent, run_git_history_agent
from app.agents.llm.openai_model import OpenAIAgentModel
from app.db.models import HypothesisStatus
from app.schemas.bug_context import BugContext
from app.schemas.hypothesis import Hypothesis

# ── shared helpers ────────────────────────────────────────────────────────────


def _emit_response(overrides: dict | None = None) -> dict:
    """Build a fake OpenAI response that immediately calls emit_hypothesis."""
    args: dict = {
        "summary": "The bug was introduced in commit abc123 which removed a uniqueness guard.",
        "suspected_files": ["review_service.py"],
        "reasoning_summary": "git blame shows the guard was removed 2 weeks ago.",
        "reproduction_plan": ["Check out commit abc123", "Run concurrent requests"],
        "candidate_fix": "Revert the removal of the unique constraint check.",
        "confidence": "high",
        **(overrides or {}),
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


def _tool_then_emit(tool_name: str, tool_args: dict) -> list[dict]:
    """Two-step response: first a tool call, then emit_hypothesis."""
    tool_response: dict = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_tool_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }
                    ],
                }
            }
        ]
    }
    return [tool_response, _emit_response()]


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bug_context() -> BugContext:
    return BugContext(
        issue_id="TST-2",
        symptom="Duplicate rows inserted on concurrent requests",
        expected="Only one row per idempotency key",
        actual="Two rows inserted under concurrent load",
        affected_area="review_service",
        error_type="deterministic",
        known_evidence=["Seen in staging with 2 concurrent requests"],
        repository="/tmp/fixture-repo",
        base_branch="main",
    )


@pytest.fixture
def agent_context(bug_context: BugContext, tmp_path: Path) -> AgentContext:
    (tmp_path / "review_service.py").write_text("def process():\n    pass\n")
    return AgentContext(
        bug_context=bug_context,
        repo_path=tmp_path,
        worktree_path=tmp_path,
        investigation_id="test-inv-002",
    )


# ── factory tests ─────────────────────────────────────────────────────────────


def test_make_git_history_agent_returns_correct_types():
    agent, tools = make_git_history_agent()
    assert isinstance(agent, OpenAIAgentModel)
    assert isinstance(tools, list)


def test_git_history_agent_tool_names():
    _, tools = make_git_history_agent()
    names = {fn.__name__ for fn in tools}
    assert names == {"git_log", "git_blame", "git_diff", "read_file"}


def test_git_history_agent_tools_have_schemas():
    _, tools = make_git_history_agent()
    for fn in tools:
        assert hasattr(fn, "__tool_schema__"), f"{fn.__name__} missing __tool_schema__"
        schema = fn.__tool_schema__
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "parameters" in schema["function"]


# ── AgentModel.run tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_history_agent_returns_valid_result(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        agent, tools = make_git_history_agent()
        result = await agent.run(agent_context, tools)

    assert isinstance(result, AgentResult)
    assert isinstance(result.hypothesis, Hypothesis)
    assert result.hypothesis.agent_type == "git_history"
    assert result.hypothesis.status == HypothesisStatus.PROPOSED
    assert result.hypothesis.suspected_files == ["review_service.py"]


@pytest.mark.asyncio
async def test_git_history_agent_records_emit_event(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        agent, tools = make_git_history_agent()
        result = await agent.run(agent_context, tools)

    emit_events = [e for e in result.events if e.action == "emit_hypothesis"]
    assert len(emit_events) == 1
    assert emit_events[0].agent == "git_history"


@pytest.mark.asyncio
async def test_git_history_agent_dispatches_git_log_tool(agent_context: AgentContext):
    responses = iter(
        _tool_then_emit(
            "git_log",
            {"repo_path": str(agent_context.worktree_path), "path": "review_service.py", "n": 10},
        )
    )
    with patch.object(
        OpenAIAgentModel,
        "_chat",
        new=AsyncMock(side_effect=lambda *a, **kw: next(responses)),
    ):
        agent, tools = make_git_history_agent()
        result = await agent.run(agent_context, tools)

    git_log_events = [e for e in result.events if e.action == "git_log"]
    assert len(git_log_events) == 1
    assert result.hypothesis.agent_type == "git_history"


@pytest.mark.asyncio
async def test_git_history_agent_dispatches_git_blame_tool(agent_context: AgentContext):
    responses = iter(
        _tool_then_emit(
            "git_blame",
            {
                "repo_path": str(agent_context.worktree_path),
                "file_path": "review_service.py",
            },
        )
    )
    with patch.object(
        OpenAIAgentModel,
        "_chat",
        new=AsyncMock(side_effect=lambda *a, **kw: next(responses)),
    ):
        agent, tools = make_git_history_agent()
        result = await agent.run(agent_context, tools)

    blame_events = [e for e in result.events if e.action == "git_blame"]
    assert len(blame_events) == 1


# ── run_git_history_agent convenience function ───────────────────────────────


@pytest.mark.asyncio
async def test_run_git_history_agent_returns_agent_result(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        result = await run_git_history_agent(agent_context)

    assert isinstance(result, AgentResult)
    assert result.hypothesis.agent_type == "git_history"
