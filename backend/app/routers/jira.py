"""
Jira router — CP-11

Endpoints:
    POST /api/jira/import/{issue_id}   fetch Jira issue → create investigation
    POST /api/jira/webhook             accept Jira issue_updated webhook events
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.intake_agent import IntakeAgent
from app.config import settings
from app.db.models import Investigation as InvestigationORM
from app.db.models import InvestigationStatus
from app.db.session import AsyncSessionLocal, get_session
from app.integrations.jira_client import (
    JiraAuthError,
    JiraClient,
    JiraClientError,
    JiraNotFoundError,
)
from app.orchestrator import run_investigation
from app.repository_access import permitted_repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jira", tags=["jira"])

_TRIGGER_STATUS = "ready for ai debugging"


# ── Request / Response models ─────────────────────────────────────────────────


class JiraImportRequest(BaseModel):
    """Optional overrides when importing a Jira issue."""

    repository: str | None = None  # override settings.target_repository
    base_branch: str = "main"


class JiraImportResponse(BaseModel):
    """Response body for POST /api/jira/import/{issue_id}."""

    investigation_id: str
    issue_id: str
    status: str  # always "CREATED" at return time
    bug_context: dict  # normalised BugContext as a plain dict


# ── Shared helper ─────────────────────────────────────────────────────────────


async def _create_investigation_from_issue(
    issue_id: str,
    repository: str,
    base_branch: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession,
) -> JiraImportResponse:
    """
    Core logic shared by the import endpoint and the webhook handler.

    1. Fetch the Jira issue.
    2. Run IntakeAgent to produce a BugContext.
    3. Persist the Investigation row.
    4. Schedule run_investigation as a background task.
    5. Return JiraImportResponse.

    Raises HTTPException on Jira errors; callers may catch before this point.
    """
    repository = permitted_repository(repository)
    client = JiraClient()
    agent = IntakeAgent()

    try:
        issue = await client.get_issue(issue_id)
    except JiraNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Jira issue not found: {issue_id}") from exc
    except JiraAuthError as exc:
        raise HTTPException(status_code=503, detail="Jira authentication failed") from exc
    except JiraClientError as exc:
        raise HTTPException(status_code=503, detail=f"Jira unreachable: {exc}") from exc

    bug_context = await agent.run(issue, repository, base_branch)

    investigation = InvestigationORM(
        external_issue_id=issue.issue_id,
        repository=repository,
        base_branch=base_branch,
        status=InvestigationStatus.CREATED,
        bug_context=bug_context.model_dump(),
    )
    session.add(investigation)
    await session.commit()
    await session.refresh(investigation)

    background_tasks.add_task(run_investigation, investigation.id, AsyncSessionLocal)
    logger.info(
        "Investigation %s created from Jira issue %s; background task queued.",
        investigation.id,
        issue.issue_id,
    )

    return JiraImportResponse(
        investigation_id=investigation.id,
        issue_id=issue.issue_id,
        status=investigation.status,
        bug_context=bug_context.model_dump(),
    )


# ── POST /api/jira/import/{issue_id} ─────────────────────────────────────────


@router.post("/import/{issue_id}", response_model=JiraImportResponse, status_code=201)
async def import_jira_issue(
    issue_id: str,
    body: JiraImportRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> JiraImportResponse:
    """
    Fetch a Jira issue and start an investigation.

    Steps:
      1. GET /rest/api/3/issue/{issue_id} via JiraClient.
      2. Normalise fields via IntakeAgent → BugContext.
      3. INSERT investigations row (status=CREATED).
      4. Fire run_investigation as a background task.
      5. Return 201 with investigation_id and normalised bug_context.

    HTTP errors:
      404 — Jira issue not found.
      503 — Jira unreachable or auth failure.
      422 — repository not configured in body or settings.
    """
    repository = body.repository or settings.target_repository
    if not repository:
        raise HTTPException(
            status_code=422,
            detail=(
                "repository is required (supply in request body or set TARGET_REPOSITORY env var)"
            ),
        )

    return await _create_investigation_from_issue(
        issue_id=issue_id,
        repository=repository,
        base_branch=body.base_branch,
        background_tasks=background_tasks,
        session=session,
    )


# ── POST /api/jira/webhook ────────────────────────────────────────────────────


def _verify_hmac(secret: str, body: bytes, signature_header: str | None) -> bool:
    """
    Verify a Jira webhook HMAC-SHA256 signature.

    Jira sends the signature as the raw HMAC hex digest in the
    ``X-Hub-Signature`` header (format: ``sha256=<hex>``).

    Returns True when the signature is valid, False otherwise.
    """
    if signature_header is None:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Accept both bare hex and "sha256=<hex>" format.
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


@router.post("/webhook", status_code=202)
async def jira_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Accept Jira ``issue_updated`` webhook events.

    Fires an investigation only when the status transition target matches
    "Ready for AI Debugging" (case-insensitive).

    Webhook HMAC validation:
      - If JIRA_WEBHOOK_SECRET is set: validate X-Hub-Signature header → 403 on failure.
      - If JIRA_WEBHOOK_SECRET is not set: accept all (dev / test mode).

    Always returns 202 immediately; investigation is created asynchronously.
    """
    raw_body = await request.body()

    # HMAC validation — only when a secret is configured.
    if settings.jira_webhook_secret:
        sig = request.headers.get("X-Hub-Signature")
        if not _verify_hmac(settings.jira_webhook_secret, raw_body, sig):
            logger.warning("Jira webhook HMAC validation failed — rejecting request")
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
    else:
        logger.debug("JIRA_WEBHOOK_SECRET not set — accepting webhook without HMAC validation")

    try:
        payload: dict = await request.json()
    except Exception:  # noqa: BLE001
        logger.warning("Jira webhook received invalid JSON body")
        return {"accepted": False, "reason": "invalid JSON"}

    # Extract the new status from the changelog.
    new_status: str | None = None
    for item in (payload.get("changelog") or {}).get("items", []):
        if item.get("field") == "status":
            new_status = item.get("toString", "")
            break

    if new_status is None or new_status.lower() != _TRIGGER_STATUS:
        logger.debug(
            "Jira webhook: status '%s' does not match trigger '%s' — ignoring",
            new_status,
            _TRIGGER_STATUS,
        )
        return {"accepted": True, "triggered": False}

    # Extract issue key from the webhook payload.
    issue_key: str | None = (payload.get("issue") or {}).get("key")
    if not issue_key:
        logger.warning("Jira webhook: cannot determine issue key from payload")
        return {"accepted": True, "triggered": False}

    repository = settings.target_repository
    if not repository:
        logger.warning(
            "Jira webhook: received trigger for %s but TARGET_REPOSITORY is not set",
            issue_key,
        )
        return {"accepted": True, "triggered": False, "reason": "TARGET_REPOSITORY not configured"}

    # Fire investigation creation in background so we return 202 immediately.
    background_tasks.add_task(
        _webhook_create_investigation,
        issue_key,
        repository,
        "main",
    )
    logger.info("Jira webhook: scheduling investigation for issue %s", issue_key)
    return {"accepted": True, "triggered": True, "issue_key": issue_key}


async def _webhook_create_investigation(
    issue_id: str,
    repository: str,
    base_branch: str,
) -> None:
    """
    Background coroutine that creates an investigation from a webhook trigger.

    Uses its own DB session (cannot reuse the request-scoped session).
    Errors are logged but not re-raised — a failed webhook must not crash the server.
    """
    try:
        async with AsyncSessionLocal() as session:
            client = JiraClient()
            agent = IntakeAgent()

            issue = await client.get_issue(issue_id)
            bug_context = await agent.run(issue, repository, base_branch)

            investigation = InvestigationORM(
                external_issue_id=issue.issue_id,
                repository=repository,
                base_branch=base_branch,
                status=InvestigationStatus.CREATED,
                bug_context=bug_context.model_dump(),
            )
            session.add(investigation)
            await session.commit()
            await session.refresh(investigation)
            inv_id = investigation.id

        # Run the full pipeline outside the session context.
        await run_investigation(inv_id, AsyncSessionLocal)
        logger.info("Webhook-triggered investigation %s complete", inv_id)

    except Exception:  # noqa: BLE001
        logger.exception("Webhook-triggered investigation for %s failed", issue_id)
