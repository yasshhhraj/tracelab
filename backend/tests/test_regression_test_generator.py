"""
Tests for CP-07: RegressionTestGenerator.

All LLM calls mocked — no real API calls in CI.
"""

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.schemas.bug_context import BugContext
from app.verification.regression_test_generator import RegressionTestGenerator

# ── helpers ───────────────────────────────────────────────────────────────────


def _emit_test_response(test_path: str, test_content: str) -> dict:
    """Fake OpenAI response that calls emit_test."""
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
                                "name": "emit_test",
                                "arguments": json.dumps(
                                    {
                                        "test_file_path": test_path,
                                        "test_content": test_content,
                                    }
                                ),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _plain_text_response(text: str) -> dict:
    """Fake response with no tool call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [],
                }
            }
        ]
    }


@pytest.fixture
def bug_context() -> BugContext:
    return BugContext(
        issue_id="TST-007",
        symptom="Duplicate rows on concurrent insert",
        expected="One row per key",
        actual="Two rows inserted",
        affected_area="review_service",
        error_type="deterministic",
        known_evidence=["Reproduced in staging"],
        repository="/tmp/repo",
        base_branch="main",
    )


# ── basic generate tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generator_returns_test_path_and_content(
    bug_context: BugContext, tmp_path: Path
):
    """generate() returns (str, str) with the expected path and content."""
    expected_content = "def test_regression():\n    assert True\n"
    expected_path = "tests/test_regression_abc12345.py"

    with patch.object(
        RegressionTestGenerator,
        "_chat",
        new=AsyncMock(return_value=_emit_test_response(expected_path, expected_content)),
    ):
        gen = RegressionTestGenerator()
        test_path, test_content = await gen.generate(
            hypothesis_id="abc12345-0000-0000-0000-000000000000",
            worktree_path=tmp_path,
            bug_context=bug_context,
            candidate_fix="Add a threading lock.",
            suspected_files=[],
            reproduction_plan=["pytest tests/"],
        )

    assert isinstance(test_path, str)
    assert isinstance(test_content, str)
    assert test_path == expected_path
    assert test_content == expected_content


@pytest.mark.asyncio
async def test_generator_writes_file_to_worktree(bug_context: BugContext, tmp_path: Path):
    """The generated test file is written into the worktree directory."""
    content = "def test_written():\n    pass\n"
    rel_path = "tests/test_regression_written.py"
    (tmp_path / "tests").mkdir()

    with patch.object(
        RegressionTestGenerator,
        "_chat",
        new=AsyncMock(return_value=_emit_test_response(rel_path, content)),
    ):
        gen = RegressionTestGenerator()
        returned_path, _ = await gen.generate(
            hypothesis_id="written00-0000-0000-0000-000000000000",
            worktree_path=tmp_path,
            bug_context=bug_context,
            candidate_fix="fix",
            suspected_files=[],
            reproduction_plan=[],
        )

    assert (tmp_path / rel_path).exists()
    assert (tmp_path / rel_path).read_text() == content


@pytest.mark.asyncio
async def test_generator_emit_test_call_parsed_correctly(
    bug_context: BugContext, tmp_path: Path
):
    """Correct test_file_path and test_content extracted from emit_test args."""
    path = "tests/test_regression_parse.py"
    content = "# regression\ndef test_x():\n    assert 1 == 1\n"

    original_emit = _emit_test_response(path, content)

    with patch.object(
        RegressionTestGenerator,
        "_chat",
        new=AsyncMock(return_value=original_emit),
    ):
        gen = RegressionTestGenerator()
        returned_path, returned_content = await gen.generate(
            hypothesis_id="parse000-0000-0000-0000-000000000000",
            worktree_path=tmp_path,
            bug_context=bug_context,
            candidate_fix="fix",
            suspected_files=[],
            reproduction_plan=[],
        )

    assert returned_path == path
    assert returned_content == content


@pytest.mark.asyncio
async def test_generator_api_key_not_in_prompt(bug_context: BugContext, tmp_path: Path):
    """The API key must not appear in any message passed to _chat (AGENTS.md)."""
    from app.config import settings

    # Set a recognisable key value
    original_key = settings.llm_api_key
    settings.llm_api_key = "sk-test-SECRET-KEY-12345"

    captured_calls: list = []

    async def capture_chat(messages, tools):
        captured_calls.append(messages)
        return _emit_test_response("tests/t.py", "def test_x(): pass")

    try:
        with patch.object(
            RegressionTestGenerator, "_chat", new=AsyncMock(side_effect=capture_chat)
        ):
            gen = RegressionTestGenerator()
            await gen.generate(
                hypothesis_id="security0-0000-0000-0000-000000000000",
                worktree_path=tmp_path,
                bug_context=bug_context,
                candidate_fix="fix",
                suspected_files=[],
                reproduction_plan=[],
            )
    finally:
        settings.llm_api_key = original_key

    assert captured_calls, "No _chat calls recorded"
    for messages in captured_calls:
        for msg in messages:
            content = msg.get("content") or ""
            assert "sk-test-SECRET-KEY-12345" not in content, (
                "API key leaked into message content"
            )


@pytest.mark.asyncio
async def test_generator_falls_back_when_no_emit_call(bug_context: BugContext, tmp_path: Path):
    """When LLM never calls emit_test, a fallback placeholder test is written."""
    # Return only plain-text responses for all iterations
    plain = _plain_text_response("I cannot write the test.")

    with patch.object(
        RegressionTestGenerator,
        "_chat",
        new=AsyncMock(return_value=plain),
    ):
        gen = RegressionTestGenerator()
        test_path, test_content = await gen.generate(
            hypothesis_id="fallback0-0000-0000-0000-000000000000",
            worktree_path=tmp_path,
            bug_context=bug_context,
            candidate_fix="fix",
            suspected_files=[],
            reproduction_plan=[],
        )

    assert "fallback" in test_path or "regression" in test_path
    assert "assert False" in test_content  # placeholder always fails
    assert (tmp_path / test_path).exists()
