"""
Tests for CP-05: AgentModel abstraction and CodePathAgent.

All tests mock OpenAIAgentModel._chat — no real LLM API calls in CI.
"""

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.agents.base import AgentContext, AgentResult
from app.agents.code_path_agent import make_code_path_agent, run_code_path_agent
from app.agents.llm.openai_model import OpenAIAgentModel
from app.db.models import HypothesisStatus
from app.schemas.bug_context import BugContext
from app.schemas.hypothesis import Hypothesis

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bug_context() -> BugContext:
    return BugContext(
        issue_id="TST-1",
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
    (tmp_path / "service.py").write_text("def process():\n    pass\n")
    return AgentContext(
        bug_context=bug_context,
        repo_path=tmp_path,
        worktree_path=tmp_path,
        investigation_id="test-inv-001",
    )


def _emit_response(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a fake OpenAI response that immediately calls emit_hypothesis."""
    args: dict[str, Any] = {
        "summary": "The duplicate row bug is caused by a missing unique constraint.",
        "suspected_files": ["service.py"],
        "reasoning_summary": "The service inserts without checking for existing rows first.",
        "reproduction_plan": ["Send 2 concurrent POST requests", "Observe duplicate rows"],
        "candidate_fix": "Add a unique DB constraint on (idempotency_key).",
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


def _tool_then_emit(tool_name: str, tool_args: dict[str, Any]) -> list[dict[str, Any]]:
    """Two-step response: first a tool call, then emit_hypothesis."""
    tool_response: dict[str, Any] = {
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


# ── Hypothesis schema tests ───────────────────────────────────────────────────


def test_hypothesis_schema_valid():
    h = Hypothesis(
        agent_type="code_path",
        summary="Bug in service.py",
        suspected_files=["service.py"],
        reasoning_summary="Missing check before insert.",
        reproduction_plan=["Step 1", "Step 2"],
        candidate_fix="Add unique constraint.",
        confidence="medium",
    )
    assert h.status == HypothesisStatus.PROPOSED
    assert h.agent_type == "code_path"
    # hypothesis_id is a UUID string (36 chars with hyphens)
    assert len(h.hypothesis_id) == 36
    assert h.hypothesis_id.count("-") == 4


def test_hypothesis_invalid_agent_type():
    with pytest.raises(ValidationError):
        Hypothesis(
            agent_type="unknown_agent",  # not in Literal
            summary="x",
            suspected_files=[],
            reasoning_summary="x",
            reproduction_plan=[],
            candidate_fix="x",
            confidence="medium",
        )


def test_hypothesis_invalid_confidence():
    with pytest.raises(ValidationError):
        Hypothesis(
            agent_type="code_path",
            summary="x",
            suspected_files=[],
            reasoning_summary="x",
            reproduction_plan=[],
            candidate_fix="x",
            confidence="very_high",  # not in Literal
        )


def test_hypothesis_all_three_agent_types():
    for agent_type in ("code_path", "git_history", "test_behavior"):
        h = Hypothesis(
            agent_type=agent_type,  # type: ignore[arg-type]
            summary="x",
            suspected_files=[],
            reasoning_summary="x",
            reproduction_plan=[],
            candidate_fix="x",
            confidence="low",
        )
        assert h.agent_type == agent_type


# ── BugContext schema tests ───────────────────────────────────────────────────


def test_bug_context_schema_valid():
    bc = BugContext(
        issue_id="TST-1",
        symptom="crash",
        expected="no crash",
        actual="crash on startup",
        affected_area="init",
        error_type="deterministic",
        known_evidence=[],
        repository="https://github.com/org/repo",
        base_branch="main",
    )
    assert bc.stack_trace is None
    assert bc.known_evidence == []


def test_bug_context_with_stack_trace():
    bc = BugContext(
        issue_id="TST-2",
        symptom="NPE",
        expected="no error",
        actual="NullPointerException",
        affected_area="parser",
        error_type="regression",
        known_evidence=["introduced in commit abc"],
        stack_trace="Traceback:\n  File foo.py line 42",
        repository="/local/repo",
        base_branch="develop",
    )
    assert bc.stack_trace is not None
    assert "foo.py" in bc.stack_trace


# ── make_code_path_agent factory ──────────────────────────────────────────────


def test_make_code_path_agent_returns_correct_types():
    agent, tools = make_code_path_agent()
    assert isinstance(agent, OpenAIAgentModel)
    assert len(tools) == 3


def test_make_code_path_agent_tool_names():
    _, tools = make_code_path_agent()
    names = {fn.__name__ for fn in tools}
    assert names == {"search_code", "read_file", "git_diff"}


def test_make_code_path_agent_tools_have_schemas():
    _, tools = make_code_path_agent()
    for fn in tools:
        assert hasattr(fn, "__tool_schema__"), f"{fn.__name__} missing __tool_schema__"
        schema = fn.__tool_schema__
        assert schema["type"] == "function"
        assert "name" in schema["function"]
        assert "parameters" in schema["function"]


# ── AgentModel.run — immediate emit ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_returns_valid_result(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    assert isinstance(result, AgentResult)
    assert isinstance(result.hypothesis, Hypothesis)
    assert result.hypothesis.agent_type == "code_path"
    assert result.hypothesis.status == HypothesisStatus.PROPOSED
    assert result.hypothesis.suspected_files == ["service.py"]


@pytest.mark.asyncio
async def test_agent_records_emit_event(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    emit_events = [e for e in result.events if e.action == "emit_hypothesis"]
    assert len(emit_events) == 1
    assert emit_events[0].agent == "code_path"
    assert "hypothesis_id" in (emit_events[0].payload or {})


# ── AgentModel.run — tool dispatch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_dispatches_search_code_tool(agent_context: AgentContext):
    """When LLM calls search_code, the agent records an AgentEvent for it."""
    responses = iter(
        _tool_then_emit(
            "search_code",
            {"pattern": "def process", "path": str(agent_context.worktree_path)},
        )
    )
    with patch.object(
        OpenAIAgentModel,
        "_chat",
        new=AsyncMock(side_effect=lambda *a, **kw: next(responses)),
    ):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    search_events = [e for e in result.events if e.action == "search_code"]
    assert len(search_events) == 1
    assert result.hypothesis.agent_type == "code_path"


@pytest.mark.asyncio
async def test_agent_dispatches_read_file_tool(agent_context: AgentContext):
    responses = iter(
        _tool_then_emit(
            "read_file",
            {"root": str(agent_context.worktree_path), "file_path": "service.py"},
        )
    )
    with patch.object(
        OpenAIAgentModel,
        "_chat",
        new=AsyncMock(side_effect=lambda *a, **kw: next(responses)),
    ):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    read_events = [e for e in result.events if e.action == "read_file"]
    assert len(read_events) == 1


# ── AgentModel.run — edge cases ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_handles_plain_text_response(agent_context: AgentContext):
    """When the model returns plain text with no tool call, emit INCONCLUSIVE hypothesis."""
    plain_text = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I cannot determine the root cause.",
                    "tool_calls": [],
                }
            }
        ]
    }
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=plain_text)):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    assert isinstance(result.hypothesis, Hypothesis)
    assert result.hypothesis.confidence == "low"
    assert result.hypothesis.suspected_files == []


@pytest.mark.asyncio
async def test_agent_handles_unknown_tool_gracefully(agent_context: AgentContext):
    """When the model calls a tool not in the index, the loop continues without raising."""
    unknown_tool_response: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "run_shell",  # not in tool index
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            }
        ]
    }
    responses = iter([unknown_tool_response, _emit_response()])
    with patch.object(
        OpenAIAgentModel,
        "_chat",
        new=AsyncMock(side_effect=lambda *a, **kw: next(responses)),
    ):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    # Should still return a valid hypothesis from the second iteration
    assert isinstance(result.hypothesis, Hypothesis)


# ── run_code_path_agent convenience function ──────────────────────────────────


@pytest.mark.asyncio
async def test_run_code_path_agent_returns_agent_result(agent_context: AgentContext):
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_emit_response())):
        result = await run_code_path_agent(agent_context)

    assert isinstance(result, AgentResult)
    assert result.hypothesis.agent_type == "code_path"
