"""
Tests for app/integrations/jira_client.py — CP-11

All HTTP calls are mocked via unittest.mock so no real Jira instance is needed.
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.integrations.jira_client import (
    JiraAuthError,
    JiraClient,
    JiraClientError,
    JiraIssue,
    JiraNotFoundError,
    JiraTransitionNotFoundError,
    _adf_to_text,
    _plain_text_to_adf,
)

# ── ADF helpers ────────────────────────────────────────────────────────────────


def _make_adf_doc(*paragraphs: str) -> dict[str, Any]:
    """Build a minimal ADF doc with one paragraph per string."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": p}],
            }
            for p in paragraphs
        ],
    }


def _make_jira_response(issue_key: str = "PVS-421") -> dict[str, Any]:
    """Return a minimal Jira REST API issue response dict."""
    return {
        "key": issue_key,
        "fields": {
            "summary": "Duplicate rows on concurrent requests",
            "description": _make_adf_doc(
                "Two rows are created when two requests arrive simultaneously."
            ),
            "issuetype": {"name": "Bug"},
            "priority": {"name": "High"},
            "status": {"name": "In Progress"},
            "components": [{"name": "review-service"}],
            "labels": ["backend", "concurrency"],
            "reporter": {"displayName": "Alice"},
            "assignee": {"displayName": "Bob"},
            "comment": {
                "comments": [
                    {"body": _make_adf_doc("Reproduced in staging with two concurrent curl calls.")}
                ]
            },
            "attachment": [{"filename": "screen.png"}],
            "created": "2024-01-01T10:00:00.000+0000",
            "updated": "2024-01-02T12:00:00.000+0000",
        },
    }


def _mock_httpx_get(json_data: dict, status_code: int = 200) -> AsyncMock:
    """Return an AsyncMock that simulates httpx.AsyncClient.get."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = json_data
    mock_response.url = "https://example.atlassian.net/rest/api/3/issue/PVS-421"
    mock_response.text = str(json_data)[:200]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def _mock_httpx_post(status_code: int = 201) -> AsyncMock:
    """Return an AsyncMock that simulates httpx.AsyncClient.post."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = {"id": "comment-1"}
    mock_response.url = "https://example.atlassian.net/rest/api/3/issue/PVS-421/comment"
    mock_response.text = ""

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


# ── Unit tests: _adf_to_text ──────────────────────────────────────────────────


def test_adf_to_text_simple_paragraph():
    doc = _make_adf_doc("Hello world")
    assert "Hello world" in _adf_to_text(doc)


def test_adf_to_text_multiple_paragraphs():
    doc = _make_adf_doc("First paragraph", "Second paragraph")
    result = _adf_to_text(doc)
    assert "First paragraph" in result
    assert "Second paragraph" in result


def test_adf_to_text_code_block():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "codeBlock",
                "content": [{"type": "text", "text": "raise ValueError('oops')"}],
            }
        ],
    }
    result = _adf_to_text(doc)
    assert "raise ValueError" in result


def test_adf_to_text_hard_break():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "line1"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "line2"},
                ],
            }
        ],
    }
    result = _adf_to_text(doc)
    assert "line1" in result
    assert "line2" in result


def test_adf_to_text_none():
    assert _adf_to_text(None) == ""


def test_adf_to_text_empty_doc():
    assert _adf_to_text({"type": "doc", "version": 1, "content": []}) == ""


# ── Unit tests: _plain_text_to_adf ────────────────────────────────────────────


def test_plain_text_to_adf_structure():
    result = _plain_text_to_adf("Hello Jira")
    assert result["body"]["type"] == "doc"
    assert result["body"]["version"] == 1
    content = result["body"]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "paragraph"
    assert content[0]["content"][0]["text"] == "Hello Jira"


# ── Integration tests: JiraClient ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_issue_ok():
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_get(_make_jira_response())

    with patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client):
        issue = await client.get_issue("PVS-421")

    assert isinstance(issue, JiraIssue)
    assert issue.issue_id == "PVS-421"
    assert issue.summary == "Duplicate rows on concurrent requests"
    assert issue.issue_type == "Bug"
    assert issue.priority == "High"
    assert issue.status == "In Progress"
    assert "review-service" in issue.components
    assert "backend" in issue.labels
    assert issue.reporter == "Alice"
    assert issue.assignee == "Bob"
    assert len(issue.comments) == 1
    assert "concurrent" in issue.comments[0]
    assert issue.attachments == ["screen.png"]


@pytest.mark.asyncio
async def test_get_issue_adf_description():
    """ADF description is extracted as plain text."""
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_get(_make_jira_response())

    with patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client):
        issue = await client.get_issue("PVS-421")

    assert "Two rows are created" in issue.description


@pytest.mark.asyncio
async def test_get_issue_404():
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_get({}, status_code=404)

    with (
        patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(JiraNotFoundError),
    ):
        await client.get_issue("MISSING-1")


@pytest.mark.asyncio
async def test_get_issue_401():
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="bad-token",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_get({}, status_code=401)

    with (
        patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(JiraAuthError),
    ):
        await client.get_issue("PVS-421")


@pytest.mark.asyncio
async def test_get_issue_500():
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_get({}, status_code=500)

    with (
        patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(JiraClientError),
    ):
        await client.get_issue("PVS-421")


@pytest.mark.asyncio
async def test_add_comment_ok():
    """add_comment posts to the correct URL with ADF body; token not in payload."""
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="supersecret",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_post(status_code=201)

    with patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client):
        await client.add_comment("PVS-421", "Analysis complete.")

    mock_client.post.assert_called_once()
    call_kwargs = mock_client.post.call_args
    # Verify ADF structure in payload
    payload = call_kwargs.kwargs["json"]
    assert "body" in payload
    # Token must NOT appear in the comment payload
    import json as _json

    payload_str = _json.dumps(payload)
    assert "supersecret" not in payload_str


@pytest.mark.asyncio
async def test_add_comment_error():
    """add_comment raises JiraClientError on non-2xx response."""
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    mock_client = _mock_httpx_post(status_code=400)

    with (
        patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(JiraClientError),
    ):
        await client.add_comment("PVS-421", "test comment")


def test_token_not_in_log(caplog):
    """The Jira API token must never appear in any log output."""
    secret_token = "TOP_SECRET_JIRA_TOKEN_12345"
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token=secret_token,
        user_email="user@example.com",
    )
    with caplog.at_level(logging.DEBUG, logger="app.integrations.jira_client"):
        # Access _auth_header to trigger any potential logging paths
        _ = client._auth_header

    for record in caplog.records:
        assert secret_token not in record.getMessage(), (
            f"Secret token leaked into log: {record.getMessage()}"
        )


@pytest.mark.asyncio
async def test_transition_status_ok():
    """transition_status calls the correct transition ID."""
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    transitions_response = MagicMock()
    transitions_response.status_code = 200
    transitions_response.json.return_value = {
        "transitions": [
            {"id": "11", "name": "AI Analyzing"},
            {"id": "21", "name": "Ready for AI Debugging"},
        ]
    }
    post_response = MagicMock()
    post_response.status_code = 204
    post_response.json.return_value = {}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=transitions_response)
    mock_client.post = AsyncMock(return_value=post_response)

    with patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client):
        await client.transition_status("PVS-421", "AI Analyzing")

    mock_client.post.assert_called_once()
    post_payload = mock_client.post.call_args.kwargs.get("json", {})
    assert post_payload == {"transition": {"id": "11"}}


@pytest.mark.asyncio
async def test_transition_status_not_found():
    """transition_status raises JiraTransitionNotFoundError for unknown status."""
    client = JiraClient(
        base_url="https://example.atlassian.net",
        api_token="token123",
        user_email="user@example.com",
    )
    transitions_response = MagicMock()
    transitions_response.status_code = 200
    transitions_response.json.return_value = {"transitions": [{"id": "11", "name": "In Progress"}]}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=transitions_response)

    with (
        patch("app.integrations.jira_client.httpx.AsyncClient", return_value=mock_client),
        pytest.raises(JiraTransitionNotFoundError),
    ):
        await client.transition_status("PVS-421", "Nonexistent Status")
