"""
Tests for app/routers/jira.py — CP-11

Uses client_with_db fixture from conftest.py.
JiraClient and IntakeAgent are patched with AsyncMock.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import Investigation as InvestigationORM
from app.integrations.jira_client import (
    JiraAuthError,
    JiraClientError,
    JiraIssue,
    JiraNotFoundError,
)
from app.schemas.bug_context import BugContext


# ── Fixtures / helpers ─────────────────────────────────────────────────────────


def _make_jira_issue(issue_id: str = "PVS-421") -> JiraIssue:
    return JiraIssue(
        issue_id=issue_id,
        summary="Duplicate rows on concurrent requests",
        description="Two rows are created under concurrent load.",
        issue_type="Bug",
        priority="High",
        status="Ready for AI Debugging",
        components=["review-service"],
        labels=[],
        reporter="Alice",
        assignee=None,
        comments=[],
        attachments=[],
        created_at="2024-01-01T10:00:00Z",
        updated_at="2024-01-02T10:00:00Z",
    )


def _make_bug_context(issue_id: str = "PVS-421") -> BugContext:
    return BugContext(
        issue_id=issue_id,
        symptom="Duplicate rows appear on concurrent requests",
        expected="One row per unique key",
        actual="Two rows inserted",
        affected_area="review-service",
        error_type="intermittent",
        known_evidence=["Reproduced in staging"],
        stack_trace=None,
        repository="/tmp/test-repo",
        base_branch="main",
    )


def _make_webhook_payload(
    issue_key: str = "PVS-421",
    new_status: str = "Ready for AI Debugging",
) -> dict:
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": issue_key, "id": "10001"},
        "changelog": {
            "items": [
                {
                    "field": "status",
                    "fromString": "In Progress",
                    "toString": new_status,
                }
            ]
        },
    }


# ── Import endpoint tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_creates_investigation(client_with_db: AsyncClient):
    """POST /api/jira/import/PVS-421 → 201 with investigation_id."""
    issue = _make_jira_issue()
    bug_ctx = _make_bug_context()

    with (
        patch("app.routers.jira.JiraClient") as MockClient,
        patch("app.routers.jira.IntakeAgent") as MockAgent,
        patch("app.routers.jira.run_investigation", new_callable=AsyncMock),
        patch("app.config.settings.target_repository", "/tmp/test-repo"),
    ):
        MockClient.return_value.get_issue = AsyncMock(return_value=issue)
        MockAgent.return_value.run = AsyncMock(return_value=bug_ctx)

        response = await client_with_db.post(
            "/api/jira/import/PVS-421",
            json={"repository": "/tmp/test-repo", "base_branch": "main"},
        )

    assert response.status_code == 201
    data = response.json()
    assert "investigation_id" in data
    assert data["issue_id"] == "PVS-421"
    assert data["status"] == "CREATED"


@pytest.mark.asyncio
async def test_import_investigation_in_db(client_with_db: AsyncClient, db_engine: AsyncEngine):
    """Investigation row appears in the DB with the correct external_issue_id."""
    issue = _make_jira_issue()
    bug_ctx = _make_bug_context()

    with (
        patch("app.routers.jira.JiraClient") as MockClient,
        patch("app.routers.jira.IntakeAgent") as MockAgent,
        patch("app.routers.jira.run_investigation", new_callable=AsyncMock),
    ):
        MockClient.return_value.get_issue = AsyncMock(return_value=issue)
        MockAgent.return_value.run = AsyncMock(return_value=bug_ctx)

        response = await client_with_db.post(
            "/api/jira/import/PVS-421",
            json={"repository": "/tmp/test-repo"},
        )

    assert response.status_code == 201
    inv_id = response.json()["investigation_id"]

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        result = await session.execute(
            select(InvestigationORM).where(InvestigationORM.id == inv_id)
        )
        row = result.scalar_one_or_none()

    assert row is not None
    assert row.external_issue_id == "PVS-421"
    assert row.repository == "/tmp/test-repo"


@pytest.mark.asyncio
async def test_import_bug_context_populated(client_with_db: AsyncClient):
    """The bug_context in the response matches the IntakeAgent output."""
    issue = _make_jira_issue()
    bug_ctx = _make_bug_context()

    with (
        patch("app.routers.jira.JiraClient") as MockClient,
        patch("app.routers.jira.IntakeAgent") as MockAgent,
        patch("app.routers.jira.run_investigation", new_callable=AsyncMock),
    ):
        MockClient.return_value.get_issue = AsyncMock(return_value=issue)
        MockAgent.return_value.run = AsyncMock(return_value=bug_ctx)

        response = await client_with_db.post(
            "/api/jira/import/PVS-421",
            json={"repository": "/tmp/test-repo"},
        )

    assert response.status_code == 201
    ctx = response.json()["bug_context"]
    assert ctx["symptom"] == bug_ctx.symptom
    assert ctx["error_type"] == bug_ctx.error_type
    assert ctx["affected_area"] == bug_ctx.affected_area


@pytest.mark.asyncio
async def test_import_not_found(client_with_db: AsyncClient):
    """JiraNotFoundError → 404."""
    with (
        patch("app.routers.jira.JiraClient") as MockClient,
        patch("app.routers.jira.IntakeAgent"),
    ):
        MockClient.return_value.get_issue = AsyncMock(
            side_effect=JiraNotFoundError("Not found")
        )
        response = await client_with_db.post(
            "/api/jira/import/MISSING-1",
            json={"repository": "/tmp/test-repo"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_import_auth_error(client_with_db: AsyncClient):
    """JiraAuthError → 503."""
    with (
        patch("app.routers.jira.JiraClient") as MockClient,
        patch("app.routers.jira.IntakeAgent"),
    ):
        MockClient.return_value.get_issue = AsyncMock(
            side_effect=JiraAuthError("Bad credentials")
        )
        response = await client_with_db.post(
            "/api/jira/import/PVS-421",
            json={"repository": "/tmp/test-repo"},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_import_no_repository(client_with_db: AsyncClient):
    """Neither body repository nor settings.target_repository → 422."""
    with (
        patch("app.routers.jira.JiraClient"),
        patch("app.routers.jira.IntakeAgent"),
        patch("app.config.settings.target_repository", ""),
    ):
        response = await client_with_db.post(
            "/api/jira/import/PVS-421",
            json={},  # no repository
        )

    assert response.status_code == 422


# ── Webhook endpoint tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_ready_for_ai(client_with_db: AsyncClient):
    """Webhook with matching status → 202 and investigation scheduled."""
    payload = _make_webhook_payload(new_status="Ready for AI Debugging")

    with (
        patch("app.routers.jira.settings.jira_webhook_secret", ""),
        patch("app.routers.jira.settings.target_repository", "/tmp/test-repo"),
        patch("app.routers.jira._webhook_create_investigation", new_callable=AsyncMock) as mock_create,
    ):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["accepted"] is True
    assert data["triggered"] is True
    mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_other_status(client_with_db: AsyncClient):
    """Webhook with non-trigger status → 202 but no investigation."""
    payload = _make_webhook_payload(new_status="In Progress")

    with (
        patch("app.routers.jira.settings.jira_webhook_secret", ""),
        patch("app.routers.jira.settings.target_repository", "/tmp/test-repo"),
        patch("app.routers.jira._webhook_create_investigation", new_callable=AsyncMock) as mock_create,
    ):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["triggered"] is False
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_hmac_invalid(client_with_db: AsyncClient):
    """Valid secret set but wrong signature → 403."""
    payload = _make_webhook_payload()
    raw_body = json.dumps(payload).encode()

    with patch("app.routers.jira.settings.jira_webhook_secret", "mysecret"):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": "sha256=invalidsignature",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_webhook_hmac_missing(client_with_db: AsyncClient):
    """Secret set but no X-Hub-Signature header → 403."""
    payload = _make_webhook_payload()

    with patch("app.routers.jira.settings.jira_webhook_secret", "mysecret"):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_webhook_no_secret(client_with_db: AsyncClient):
    """Secret not set → accept all requests (dev mode)."""
    payload = _make_webhook_payload(new_status="Some Other Status")

    with (
        patch("app.routers.jira.settings.jira_webhook_secret", ""),
        patch("app.routers.jira.settings.target_repository", "/tmp/repo"),
        patch("app.routers.jira._webhook_create_investigation", new_callable=AsyncMock),
    ):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            # No X-Hub-Signature header — should not matter when secret is empty
        )

    assert response.status_code == 202


@pytest.mark.asyncio
async def test_webhook_valid_hmac(client_with_db: AsyncClient):
    """Correct HMAC signature passes validation."""
    payload = _make_webhook_payload(new_status="Ready for AI Debugging")
    raw_body = json.dumps(payload).encode()
    secret = "webhook-secret-123"
    sig = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    with (
        patch("app.routers.jira.settings.jira_webhook_secret", secret),
        patch("app.routers.jira.settings.target_repository", "/tmp/test-repo"),
        patch("app.routers.jira._webhook_create_investigation", new_callable=AsyncMock),
    ):
        response = await client_with_db.post(
            "/api/jira/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": f"sha256={sig}",
            },
        )

    assert response.status_code == 202
    assert response.json()["triggered"] is True
