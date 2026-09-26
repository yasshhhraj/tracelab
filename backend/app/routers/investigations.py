"""
Investigations router — CP-06 through CP-09

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

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import AgentEvent as AgentEventORM
from app.db.models import Hypothesis as HypothesisORM
from app.db.models import Investigation as InvestigationORM
from app.db.models import InvestigationStatus
from app.db.models import Patch as PatchORM
from app.db.session import AsyncSessionLocal, get_session
from app.orchestrator import run_investigation
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/investigations", tags=["investigations"])

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
    Create a draft pull request for an approved investigation.

    AGENTS.md constraint: status MUST be APPROVED — raises 409 otherwise.
    Reads the winning patch from the diagnosis selected_hypothesis_id.

    Note: Full GitHub integration is implemented in CP-12. This endpoint
    returns a stub PR URL derived from the investigation and branch while
    confirming the approval gate is enforced.
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

    # Resolve the winning patch branch from the diagnosis
    pr_url: str = ""
    branch: str = ""

    if inv.diagnosis and inv.diagnosis.selected_hypothesis_id:
        # Retrieve the latest patch for the winning hypothesis
        patch_result = await session.execute(
            select(PatchORM)
            .where(PatchORM.hypothesis_id == inv.diagnosis.selected_hypothesis_id)
            .order_by(PatchORM.created_at.desc())
            .limit(1)
        )
        patch = patch_result.scalar_one_or_none()

        if patch:
            branch = patch.branch
            if patch.pr_url:
                # Already created (e.g. by CP-12 GitHub integration)
                pr_url = patch.pr_url
            else:
                # Stub URL — full GitHub push/PR in CP-12
                pr_url = (
                    f"https://github.com/placeholder/{inv.repository.split('/')[-1]}"
                    f"/pull/new/{branch}"
                )
                patch.pr_url = pr_url
                await session.commit()

    if not branch:
        # No patch available yet — derive a branch name from the investigation
        branch = f"ai-debug/{inv.external_issue_id}-diagnosis"
        pr_url = (
            f"https://github.com/placeholder/{inv.repository.split('/')[-1]}"
            f"/pull/new/{branch}"
        )

    logger.info(
        "Pull-request stub created for investigation %s: %s", investigation_id, pr_url
    )
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
