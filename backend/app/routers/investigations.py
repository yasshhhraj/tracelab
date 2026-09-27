"""
Investigations router — CP-06 through CP-12

Endpoints:
    POST  /api/investigations                        create + trigger background task
    GET   /api/investigations                        list (paginated)
    GET   /api/investigations/{id}                   detail + embedded diagnosis
    GET   /api/investigations/{id}/hypotheses        all hypotheses
    POST  /api/investigations/{id}/approve           set status → APPROVED
    POST  /api/investigations/{id}/reject            set status → REJECTED
    POST  /api/investigations/{id}/pull-request      only if status == APPROVED (AGENTS.md)
    GET   /api/investigations/{id}/events            agent event stream (timeline)

AGENTS.md constraint enforced here:
    POST /pull-request MUST check status == APPROVED — raises 409 otherwise.
"""

import contextlib
import logging
import re
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import AgentEvent as AgentEventORM
from app.db.models import Hypothesis as HypothesisORM
from app.db.models import HypothesisStatus, InvestigationStatus
from app.db.models import Investigation as InvestigationORM
from app.db.models import Patch as PatchORM
from app.db.session import AsyncSessionLocal, get_session
from app.integrations.github_client import (
    GitHubClient,
    GitHubClientError,
)
from app.integrations.pr_body import build_pr_body, build_pr_title
from app.orchestrator import run_investigation
from app.repository_access import permitted_repository
from app.schemas.bug_context import BugContext
from app.schemas.investigation import (
    AgentEventResponse,
    ApproveResponse,
    HypothesisResponse,
    InvestigationCreate,
    InvestigationDetailResponse,
    InvestigationListResponse,
    InvestigationResponse,
    PullRequestResponse,
    RejectResponse,
)
from app.tools.executor import run_command

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/investigations", tags=["investigations"])


# ── CP-12 helpers ─────────────────────────────────────────────────────────────


def _parse_github_repo(repository: str) -> tuple[str, str]:
    """
    Extract (owner, repo) from a GitHub repository string.

    Accepts:
        "github.com/org/repo"
        "https://github.com/org/repo"
        "https://github.com/org/repo.git"
        "org/repo"  (bare owner/repo)

    Returns (owner, repo).
    Raises ValueError if the string cannot be parsed.
    """
    # Strip common prefixes, including the SSH form returned by git remote.
    repository = re.sub(r"^git@github\.com:", "github.com/", repository)
    repository = re.sub(r"^ssh://git@github\.com/", "github.com/", repository)
    cleaned = re.sub(r"^https?://", "", repository)
    if cleaned.startswith("github.com/"):
        cleaned = cleaned.removeprefix("github.com/")
    elif "/" in cleaned and (cleaned.startswith("/") or "." in cleaned.split("/", 1)[0]):
        raise ValueError(f"Not a GitHub repository: '{repository}'")
    cleaned = cleaned.rstrip("/").removesuffix(".git")

    parts = cleaned.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Cannot parse GitHub owner/repo from repository string: '{repository}'")
    return parts[0], parts[1]


async def _resolve_github_repo(repository: str) -> tuple[str, str]:
    """Resolve a local investigation checkout to its GitHub origin."""
    local_path = Path(repository)
    if local_path.is_dir():
        result = await run_command(["git", "remote", "get-url", "origin"], cwd=local_path)
        if result.exit_code != 0 or result.timed_out:
            raise ValueError(f"Cannot read GitHub origin for local repository: '{repository}'")
        repository = result.stdout.strip()
    return _parse_github_repo(repository)


async def _create_github_pr(
    patch: PatchORM,
    inv: InvestigationORM,
    client: GitHubClient,
) -> str:
    """
    Apply the winning patch diff on a fresh clone and open a draft PR.

    Returns the HTML URL of the created pull request.
    Raises GitHubClientError (or subclass) on any failure.
    """
    owner, repo = await _resolve_github_repo(inv.repository)

    # Build PR metadata from the diagnosis
    diagnosis = inv.diagnosis
    bug_ctx_dict = inv.bug_context or {}
    issue_id = inv.external_issue_id
    symptom = bug_ctx_dict.get("symptom", issue_id)
    base_branch = inv.base_branch or "main"

    if diagnosis:
        verified_cause = diagnosis.verified_cause or ""
        evidence = list(diagnosis.evidence or [])
        rejected = list(diagnosis.rejected_hypotheses or [])
        changed_files = list(diagnosis.changed_files or [])
        risk = diagnosis.risk or "unknown"
    else:
        verified_cause = ""
        evidence = []
        rejected = []
        changed_files = list(patch.files_changed or [])
        risk = "unknown"

    title = build_pr_title(issue_id, symptom)
    body = build_pr_body(
        issue_id=issue_id,
        symptom=symptom,
        verified_cause=verified_cause,
        evidence=evidence,
        rejected_hypotheses=rejected,
        changed_files=changed_files,
        risk=risk,
    )

    # Push the patch onto a fresh clone, then open the draft PR
    await client.clone_apply_and_push(
        owner=owner,
        repo=repo,
        branch=patch.branch,
        base_branch=base_branch,
        diff=patch.diff or "",
        commit_message=f"TraceLab fix: {issue_id}",
    )

    pr_url = await client.create_draft_pr(
        owner=owner,
        repo=repo,
        branch=patch.branch,
        base=base_branch,
        title=title,
        body=body,
    )
    return pr_url


# ── valid transitions for approve / reject ────────────────────────────────────
_APPROVABLE_STATUSES = {InvestigationStatus.WAITING_FOR_REVIEW}
_REJECTABLE_STATUSES = {InvestigationStatus.WAITING_FOR_REVIEW}


# ── POST /api/investigations ──────────────────────────────────────────────────


@router.post("", response_model=InvestigationResponse, status_code=201)
async def create_investigation(
    body: InvestigationCreate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> InvestigationResponse:
    """
    Start a new investigation.

    Creates the Investigation row immediately and fires the orchestrator as a
    background task.  Returns the investigation in CREATED state without
    waiting for agents to complete.
    """
    repository = body.repository or settings.target_repository
    if not repository:
        raise HTTPException(status_code=422, detail="Repository is required")
    repository = permitted_repository(repository)

    bug_context = BugContext(
        issue_id=body.jira_issue_id,
        symptom=body.symptom,
        expected=body.expected,
        actual=body.actual,
        affected_area=body.affected_area,
        error_type=body.error_type,
        known_evidence=body.known_evidence,
        stack_trace=body.stack_trace,
        repository=repository,
        base_branch=body.base_branch,
    )

    investigation = InvestigationORM(
        external_issue_id=body.jira_issue_id,
        repository=repository,
        base_branch=body.base_branch,
        status=InvestigationStatus.CREATED,
        bug_context=bug_context.model_dump(),
    )
    session.add(investigation)
    await session.commit()
    await session.refresh(investigation)

    # Fire background task — BackgroundTasks runs after response is sent
    background_tasks.add_task(run_investigation, investigation.id, AsyncSessionLocal)
    logger.info("Investigation %s created; background task queued.", investigation.id)

    return InvestigationResponse.model_validate(investigation)


# ── GET /api/investigations ───────────────────────────────────────────────────


@router.get("", response_model=InvestigationListResponse)
async def list_investigations(
    limit: int = 20,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> InvestigationListResponse:
    """Return a paginated list of all investigations."""
    total_result = await session.execute(select(func.count()).select_from(InvestigationORM))
    total: int = total_result.scalar_one()

    rows_result = await session.execute(
        select(InvestigationORM)
        .order_by(InvestigationORM.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [InvestigationResponse.model_validate(r) for r in rows_result.scalars().all()]
    return InvestigationListResponse(items=items, total=total, limit=limit, offset=offset)


# ── GET /api/investigations/{id} ──────────────────────────────────────────────


@router.get("/{investigation_id}", response_model=InvestigationDetailResponse)
async def get_investigation(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> InvestigationDetailResponse:
    """Return a single investigation by ID, including embedded diagnosis."""
    inv = await session.get(
        InvestigationORM,
        investigation_id,
        options=[selectinload(InvestigationORM.diagnosis)],
    )
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return InvestigationDetailResponse.model_validate(inv)


# ── GET /api/investigations/{id}/hypotheses ───────────────────────────────────


@router.get("/{investigation_id}/hypotheses", response_model=list[HypothesisResponse])
async def list_hypotheses(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[HypothesisResponse]:
    """Return all hypotheses for an investigation."""
    inv = await session.get(InvestigationORM, investigation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    result = await session.execute(
        select(HypothesisORM)
        .where(HypothesisORM.investigation_id == investigation_id)
        .order_by(HypothesisORM.created_at)
    )
    return [HypothesisResponse.model_validate(h) for h in result.scalars().all()]


# ── POST /api/investigations/{id}/approve ─────────────────────────────────────


@router.post("/{investigation_id}/approve", response_model=ApproveResponse)
async def approve_investigation(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> ApproveResponse:
    """
    Approve an investigation for PR creation.

    Transitions the investigation status from WAITING_FOR_REVIEW → APPROVED.
    Only investigations currently in WAITING_FOR_REVIEW may be approved.
    """
    inv = await session.get(InvestigationORM, investigation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if inv.status not in _APPROVABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Investigation cannot be approved in status '{inv.status}'. "
                f"Must be in WAITING_FOR_REVIEW."
            ),
        )

    inv.status = InvestigationStatus.APPROVED
    await session.commit()
    logger.info("Investigation %s approved.", investigation_id)
    return ApproveResponse(id=investigation_id, status=InvestigationStatus.APPROVED)


# ── POST /api/investigations/{id}/reject ──────────────────────────────────────


@router.post("/{investigation_id}/reject", response_model=RejectResponse)
async def reject_investigation(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> RejectResponse:
    """
    Reject an investigation's diagnosis.

    Transitions the investigation status from WAITING_FOR_REVIEW → REJECTED.
    Only investigations currently in WAITING_FOR_REVIEW may be rejected.
    """
    inv = await session.get(InvestigationORM, investigation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    if inv.status not in _REJECTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Investigation cannot be rejected in status '{inv.status}'. "
                f"Must be in WAITING_FOR_REVIEW."
            ),
        )

    inv.status = InvestigationStatus.REJECTED
    await session.commit()
    logger.info("Investigation %s rejected.", investigation_id)
    return RejectResponse(id=investigation_id, status=InvestigationStatus.REJECTED)


# ── POST /api/investigations/{id}/pull-request ────────────────────────────────


@router.post("/{investigation_id}/pull-request", response_model=PullRequestResponse)
async def create_pull_request(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> PullRequestResponse:
    """
    Create a GitHub draft pull request for an approved investigation.

    AGENTS.md constraint: status MUST be APPROVED — raises 409 otherwise.

    Steps (CP-12):
      1. Load investigation + diagnosis; assert APPROVED.
      2. Load the winning Patch row.
      3. If patch.pr_url already set → return cached (idempotent).
      4. Require a verified patch and GITHUB_TOKEN.
      5. Push diff + create draft PR via GitHubClient.
      6. Persist pr_url on Patch row.
      7. Post PR link as Jira comment (fire-and-forget, suppress errors).
      8. Return PullRequestResponse.
    """
    inv = await session.get(
        InvestigationORM,
        investigation_id,
        options=[selectinload(InvestigationORM.diagnosis)],
    )
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    # AGENTS.md: must check status == APPROVED before proceeding
    if inv.status != InvestigationStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail="Investigation must be APPROVED before PR creation",
        )

    # ── resolve verified patch ────────────────────────────────────────────────
    selected_id = inv.diagnosis.selected_hypothesis_id if inv.diagnosis else None
    if not selected_id:
        raise HTTPException(status_code=409, detail="No verified hypothesis is available for a PR")

    hypothesis = await session.get(HypothesisORM, selected_id)
    if hypothesis is None or hypothesis.status != HypothesisStatus.VERIFIED:
        raise HTTPException(status_code=409, detail="Selected hypothesis is not VERIFIED")

    patch_result = await session.execute(
        select(PatchORM)
        .where(PatchORM.hypothesis_id == selected_id)
        .order_by(PatchORM.created_at.desc())
        .limit(1)
    )
    patch = patch_result.scalar_one_or_none()
    if patch is None or not patch.diff or not patch.diff.strip():
        raise HTTPException(
            status_code=409,
            detail="No patch is available for the verified hypothesis",
        )

    branch = patch.branch
    if patch.pr_url:
        logger.info("PR already exists for investigation %s: %s", investigation_id, patch.pr_url)
        return PullRequestResponse(
            investigation_id=investigation_id,
            pr_url=patch.pr_url,
            branch=branch,
        )

    if not settings.github_token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN is not configured")

    try:
        pr_url = await _create_github_pr(patch, inv, GitHubClient())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot parse GitHub repository: {exc}",
        ) from exc
    except GitHubClientError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GitHub error: {exc}",
        ) from exc

    # ── persist pr_url on patch ───────────────────────────────────────────────
    patch.pr_url = pr_url
    await session.commit()

    # ── post Jira PR-link comment (CP-11 + CP-12, fire-and-forget) ────────────
    with contextlib.suppress(Exception):
        from app.integrations.jira_client import JiraClient

        await JiraClient().add_comment(
            inv.external_issue_id,
            f"Draft PR created: {pr_url}",
        )

    logger.info("Pull-request created for investigation %s: %s", investigation_id, pr_url)
    return PullRequestResponse(
        investigation_id=investigation_id,
        pr_url=pr_url,
        branch=branch,
    )


# ── GET /api/investigations/{id}/events ───────────────────────────────────────


@router.get("/{investigation_id}/events", response_model=list[AgentEventResponse])
async def get_events(
    investigation_id: str,
    session: AsyncSession = Depends(get_session),
) -> list[AgentEventResponse]:
    """
    Return all agent events for an investigation, ordered by timestamp.

    Used by the dashboard timeline to show what each agent did and when.
    """
    inv = await session.get(InvestigationORM, investigation_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Investigation not found")

    result = await session.execute(
        select(AgentEventORM)
        .where(AgentEventORM.investigation_id == investigation_id)
        .order_by(AgentEventORM.timestamp)
    )
    return [AgentEventResponse.model_validate(e) for e in result.scalars().all()]
