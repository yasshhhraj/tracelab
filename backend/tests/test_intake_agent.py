"""
Tests for app/agents/intake_agent.py — CP-11

LLM HTTP calls are mocked; no real API key required.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.intake_agent import IntakeAgent
from app.integrations.jira_client import JiraIssue
from app.schemas.bug_context import BugContext


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_issue(
    issue_id: str = "PVS-421",
    summary: str = "Duplicate rows created under load",
    description: str = "Two rows are created when two identical requests arrive simultaneously.",
    issue_type: str = "Bug",
    priority: str = "High",
    status: str = "In Progress",
    components: list[str] | None = None,
    labels: list[str] | None = None,
    comments: list[str] | None = None,
) -> JiraIssue:
    return JiraIssue(
        issue_id=issue_id,
        summary=summary,
        description=description,
        issue_type=issue_type,
        priority=priority,
        status=status,
        components=components or ["review-service"],
        labels=labels or [],
        reporter="Alice",
        assignee=None,
        comments=comments or [],
        attachments=[],
        created_at="2024-01-01T10:00:00Z",
        updated_at="2024-01-02T10:00:00Z",
    )


def _make_llm_response(args: dict[str, Any]) -> dict[str, Any]:
    """Build a fake OpenAI chat completion response that calls emit_bug_context."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "emit_bug_context",
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _make_no_tool_call_response() -> dict[str, Any]:
    """Fake response where the LLM returns text instead of a tool call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I cannot process this issue.",
                    "tool_calls": None,
                }
            }
        ]
    }


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_deterministic_issue():
    """Normal bug → BugContext with error_type=deterministic."""
    llm_args = {
        "symptom": "Two rows are created when two identical requests arrive simultaneously.",
        "expected": "One row per unique key",
        "actual": "Two rows inserted",
        "affected_area": "review-service",
        "error_type": "deterministic",
        "known_evidence": ["Reproduced in staging"],
        "stack_trace": None,
    }
    issue = _make_issue()
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = _make_llm_response(llm_args)
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        result = await agent.run(issue, "/repo", "main")

    assert isinstance(result, BugContext)
    assert result.issue_id == "PVS-421"
    assert result.error_type == "deterministic"
    assert result.symptom == llm_args["symptom"]
    assert result.affected_area == "review-service"
    assert "Reproduced in staging" in result.known_evidence
    assert result.stack_trace is None


@pytest.mark.asyncio
async def test_run_intermittent_issue():
    """Issue describing a race condition → error_type=intermittent."""
    llm_args = {
        "symptom": "Occasionally two rows created under concurrent requests",
        "expected": "One row per key",
        "actual": "Race condition results in duplicate rows",
        "affected_area": "review-service",
        "error_type": "intermittent",
        "known_evidence": ["concurrent requests", "timing-dependent"],
        "stack_trace": None,
    }
    issue = _make_issue(
        description="Race condition between idempotency check and insert. Sometimes two rows appear."
    )
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = _make_llm_response(llm_args)
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        result = await agent.run(issue, "/repo", "main")

    assert result.error_type == "intermittent"


@pytest.mark.asyncio
async def test_run_regression_issue():
    """Issue mentioning 'used to work' → error_type=regression."""
    llm_args = {
        "symptom": "Checkout fails since v2.3 deployment",
        "expected": "Order created as before v2.3",
        "actual": "500 error on checkout endpoint",
        "affected_area": "checkout",
        "error_type": "regression",
        "known_evidence": ["worked before v2.3", "broke after deploy on 2024-01-15"],
        "stack_trace": None,
    }
    issue = _make_issue(
        description="This used to work fine in v2.2. After the v2.3 deploy checkout is broken.",
        components=["checkout"],
    )
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = _make_llm_response(llm_args)
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        result = await agent.run(issue, "/repo", "main")

    assert result.error_type == "regression"


@pytest.mark.asyncio
async def test_run_extracts_stack_trace():
    """Stack trace in description is captured in bug_context.stack_trace."""
    stack = "Traceback (most recent call last):\n  File 'review.py', line 42, in create\nValueError: duplicate key"
    llm_args = {
        "symptom": "ValueError: duplicate key on review creation",
        "expected": "Review created successfully",
        "actual": "ValueError raised",
        "affected_area": "review-service",
        "error_type": "deterministic",
        "known_evidence": [],
        "stack_trace": stack,
    }
    issue = _make_issue(description=f"Stack trace found:\n{stack}")
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = _make_llm_response(llm_args)
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        result = await agent.run(issue, "/repo", "main")

    assert result.stack_trace is not None
    assert "ValueError" in result.stack_trace


@pytest.mark.asyncio
async def test_run_llm_fallback():
    """LLM returns no tool call → fallback BugContext is constructed from raw fields."""
    issue = _make_issue()
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = _make_no_tool_call_response()
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        result = await agent.run(issue, "/repo", "main")

    # Fallback uses the summary as the symptom
    assert isinstance(result, BugContext)
    assert result.issue_id == "PVS-421"
    assert result.symptom == issue.summary
    assert result.repository == "/repo"
    assert result.base_branch == "main"


@pytest.mark.asyncio
async def test_api_key_not_in_message():
    """The LLM API key must never appear in any message sent to the LLM."""
    from app.config import settings

    captured_payloads: list[dict] = []

    issue = _make_issue()
    agent = IntakeAgent()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _make_llm_response({
        "symptom": "s",
        "expected": "e",
        "actual": "a",
        "affected_area": "x",
        "error_type": "deterministic",
        "known_evidence": [],
    })

    async def capture_post(url, *, headers, json, **kwargs):
        captured_payloads.append(json)
        return mock_response

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = capture_post  # type: ignore[assignment]

    api_key = settings.llm_api_key or "test-key-must-not-appear"
    with patch("app.agents.intake_agent.httpx.AsyncClient", return_value=mock_client):
        with patch.object(settings, "llm_api_key", api_key):
            await agent.run(issue, "/repo", "main")

    for payload in captured_payloads:
        payload_str = json.dumps(payload)
        assert api_key not in payload_str, (
            f"LLM API key leaked into request payload"
        )
