from datetime import datetime

from pydantic import BaseModel


class InvestigationCreate(BaseModel):
    """Request body for POST /api/investigations."""

    jira_issue_id: str
    repository: str
    base_branch: str = "main"
    # Inline bug context — avoids nested JSON in the request body
    symptom: str
    expected: str
    actual: str
    affected_area: str
    error_type: str  # "deterministic" | "intermittent" | "regression"
    known_evidence: list[str] = []
    stack_trace: str | None = None


class InvestigationResponse(BaseModel):
    """Response model for a single Investigation."""

    model_config = {"from_attributes": True}

    id: str
    external_issue_id: str
    repository: str
    base_branch: str
    status: str
    bug_context: dict | None
    created_at: datetime
    updated_at: datetime


class InvestigationListResponse(BaseModel):
    """Paginated list response for GET /api/investigations."""

    items: list[InvestigationResponse]
    total: int
    limit: int
    offset: int


class HypothesisResponse(BaseModel):
    """Response model for a single Hypothesis."""

    model_config = {"from_attributes": True}

    id: str
    investigation_id: str
    agent_type: str
    summary: str
    reasoning_summary: str
    candidate_fix: str
    suspected_files: list[str]
    reproduction_plan: list[str]
    confidence: str
    status: str
    created_at: datetime
