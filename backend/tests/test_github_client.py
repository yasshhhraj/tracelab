"""
Tests for app/integrations/github_client.py — CP-12

All HTTP calls and run_command invocations are mocked.
No real GitHub credentials or network access required.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.integrations.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubClientError,
    GitHubConflictError,
    GitHubNotFoundError,
)
from app.tools.executor import CommandResult


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_command_result(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        command=["git", "push"],
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=100,
        timed_out=timed_out,
    )


def _make_http_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data or {}
    mock.text = text or str(json_data or "")
    mock.url = "https://api.github.com/test"
    return mock


def _make_async_client(responses: list[MagicMock]) -> AsyncMock:
    """Build an AsyncMock httpx.AsyncClient that returns responses in order."""
    call_count = [0]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    def _next_response(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        return responses[idx] if idx < len(responses) else responses[-1]

    mock_client.get = AsyncMock(side_effect=_next_response)
    mock_client.post = AsyncMock(side_effect=_next_response)
    mock_client.patch = AsyncMock(side_effect=_next_response)
    return mock_client


# ── push_branch ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_branch_ok(tmp_path: Path):
    """Successful push returns HEAD commit SHA; token not in returned value."""
    sha = "abc123def456"
    push_result = _make_command_result(exit_code=0, stdout="")
    sha_result = _make_command_result(exit_code=0, stdout=sha + "\n")

    client = GitHubClient(token="supersecret_token")

    with patch(
        "app.integrations.github_client.run_command",
        new_callable=AsyncMock,
        side_effect=[push_result, sha_result],
    ) as mock_run:
        result = await client.push_branch(
            owner="org", repo="myapp",
            branch="ai-debug/PVS-421-h1",
            local_repo=tmp_path,
        )

    assert result == sha
    assert "supersecret_token" not in result
    # Verify that git push was called (the first call)
    first_call_args = mock_run.call_args_list[0][0][0]  # argv list
    assert first_call_args[0] == "git"
    assert first_call_args[1] == "push"
    # Token embedded in URL, not as a standalone arg after "push"
    push_url = first_call_args[2]
    assert "supersecret_token" in push_url


@pytest.mark.asyncio
async def test_push_branch_auth_failure(tmp_path: Path):
    """stderr containing '401' → GitHubAuthError."""
    result = _make_command_result(
        exit_code=128,
        stderr="fatal: Authentication failed: 401 Unauthorized",
    )
    client = GitHubClient(token="bad_token")

    with patch(
        "app.integrations.github_client.run_command",
        new_callable=AsyncMock,
        return_value=result,
    ):
        with pytest.raises(GitHubAuthError):
            await client.push_branch("org", "repo", "branch", tmp_path)


@pytest.mark.asyncio
async def test_push_branch_repo_not_found(tmp_path: Path):
    """stderr containing 'not found' → GitHubNotFoundError."""
    result = _make_command_result(
        exit_code=128,
        stderr="remote: Repository not found.",
    )
    client = GitHubClient(token="token123")

    with patch(
        "app.integrations.github_client.run_command",
        new_callable=AsyncMock,
        return_value=result,
    ):
        with pytest.raises(GitHubNotFoundError):
            await client.push_branch("org", "missing-repo", "branch", tmp_path)


@pytest.mark.asyncio
async def test_push_branch_other_failure(tmp_path: Path):
    """Non-zero exit with unrecognised stderr → GitHubClientError."""
    result = _make_command_result(
        exit_code=1,
        stderr="error: failed to push some refs",
    )
    client = GitHubClient(token="token123")

    with patch(
        "app.integrations.github_client.run_command",
        new_callable=AsyncMock,
        return_value=result,
    ):
        with pytest.raises(GitHubClientError):
            await client.push_branch("org", "repo", "branch", tmp_path)


@pytest.mark.asyncio
async def test_push_branch_timeout(tmp_path: Path):
    """Timed-out push → GitHubClientError."""
    result = _make_command_result(exit_code=-1, timed_out=True)
    client = GitHubClient(token="token123")

    with patch(
        "app.integrations.github_client.run_command",
        new_callable=AsyncMock,
        return_value=result,
    ):
        with pytest.raises(GitHubClientError, match="timed out"):
            await client.push_branch("org", "repo", "branch", tmp_path)


def test_push_branch_token_not_in_log(tmp_path: Path, caplog):
    """The GitHub token must never appear in any log record."""
    secret = "MY_VERY_SECRET_GITHUB_TOKEN"
    client = GitHubClient(token=secret)
    with caplog.at_level(logging.DEBUG, logger="app.integrations.github_client"):
        _ = client._auth_header
    for record in caplog.records:
        assert secret not in record.getMessage()


# ── create_draft_pr ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_draft_pr_ok():
    """Successful PR creation returns html_url; payload has draft=True."""
    pr_data = {"html_url": "https://github.com/org/repo/pull/42", "number": 42}
    mock_resp = _make_http_response(status_code=201, json_data=pr_data)

    client = GitHubClient(token="token123")
    mock_client = _make_async_client([mock_resp])

    with patch("app.integrations.github_client.httpx.AsyncClient", return_value=mock_client):
        url = await client.create_draft_pr(
            owner="org", repo="repo",
            branch="ai-debug/PVS-421-h1",
            base="main",
            title="Fix PVS-421",
            body="Body text",
        )

    assert url == "https://github.com/org/repo/pull/42"
    # Verify draft=True in payload
    post_payload = mock_client.post.call_args.kwargs["json"]
    assert post_payload["draft"] is True
    assert post_payload["head"] == "ai-debug/PVS-421-h1"
    assert post_payload["base"] == "main"


@pytest.mark.asyncio
async def test_create_draft_pr_conflict():
    """422 with 'already exists' in body → GitHubConflictError."""
    mock_resp = _make_http_response(
        status_code=422,
        text='{"message": "Reference already exists"}',
    )
    mock_resp.json.return_value = {"message": "Reference already exists"}
    mock_resp.text = '{"message": "Reference already exists"}'

    client = GitHubClient(token="token123")
    mock_client = _make_async_client([mock_resp])

    with patch("app.integrations.github_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GitHubConflictError):
            await client.create_draft_pr("org", "repo", "branch", "main", "T", "B")


@pytest.mark.asyncio
async def test_create_draft_pr_auth():
    """401 → GitHubAuthError."""
    mock_resp = _make_http_response(status_code=401, text="Unauthorized")
    client = GitHubClient(token="bad_token")
    mock_client = _make_async_client([mock_resp])

    with patch("app.integrations.github_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GitHubAuthError):
            await client.create_draft_pr("org", "repo", "branch", "main", "T", "B")


@pytest.mark.asyncio
async def test_create_draft_pr_not_found():
    """404 → GitHubNotFoundError."""
    mock_resp = _make_http_response(status_code=404, text="Not Found")
    client = GitHubClient(token="token123")
    mock_client = _make_async_client([mock_resp])

    with patch("app.integrations.github_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(GitHubNotFoundError):
            await client.create_draft_pr("org", "repo", "branch", "main", "T", "B")


# ── get_or_create_ref ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_or_create_ref_creates_new():
    """404 on GET → POST (create) is called with correct payload."""
    not_found = _make_http_response(status_code=404)
    created = _make_http_response(
        status_code=201,
        json_data={"ref": "refs/heads/test", "object": {"sha": "newsha"}},
    )

    client = GitHubClient(token="token123")

    call_responses: list[MagicMock] = []

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    # First AsyncClient call: GET → 404; second: POST → 201
    client_instances = [
        _build_single_response_client("get", not_found),
        _build_single_response_client("post", created),
    ]
    instance_idx = [0]

    def _factory(*args, **kwargs):
        idx = instance_idx[0]
        instance_idx[0] += 1
        return client_instances[idx]

    with patch("app.integrations.github_client.httpx.AsyncClient", side_effect=_factory):
        result = await client.get_or_create_ref("org", "repo", "test-branch", "newsha")

    assert result == "newsha"


@pytest.mark.asyncio
async def test_get_or_create_ref_updates_existing():
    """200 on GET → PATCH (update) is called."""
    existing = _make_http_response(
        status_code=200,
        json_data={"ref": "refs/heads/test", "object": {"sha": "oldsha"}},
    )
    updated = _make_http_response(
        status_code=200,
        json_data={"ref": "refs/heads/test", "object": {"sha": "newsha"}},
    )

    client = GitHubClient(token="token123")
    client_instances = [
        _build_single_response_client("get", existing),
        _build_single_response_client("patch", updated),
    ]
    instance_idx = [0]

    def _factory(*args, **kwargs):
        idx = instance_idx[0]
        instance_idx[0] += 1
        return client_instances[idx]

    with patch("app.integrations.github_client.httpx.AsyncClient", side_effect=_factory):
        result = await client.get_or_create_ref("org", "repo", "test-branch", "newsha")

    assert result == "newsha"


def _build_single_response_client(method: str, response: MagicMock) -> AsyncMock:
    """Build an AsyncMock client that returns `response` for the given HTTP method."""
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    for m in ("get", "post", "patch", "put", "delete"):
        setattr(c, m, AsyncMock(return_value=MagicMock(status_code=405)))
    setattr(c, method, AsyncMock(return_value=response))
    return c


# ── token not in log ───────────────────────────────────────────────────────────


def test_token_not_in_log(caplog):
    """GitHub token must never appear in any log output."""
    secret = "SUPER_SECRET_GITHUB_PAT_99999"
    client = GitHubClient(token=secret)
    with caplog.at_level(logging.DEBUG, logger="app.integrations.github_client"):
        _ = client._token
        _ = client._auth_header
    for record in caplog.records:
        assert secret not in record.getMessage(), (
            f"GitHub token leaked into log: {record.getMessage()}"
        )
