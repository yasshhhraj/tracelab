from datetime import datetime

from pydantic import BaseModel


class InvestigationCreate(BaseModel):
    """Request body for POST /api/investigations."""

    jira_issue_id: str
    repository: str | None = None
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


class RejectedHypothesisSchema(BaseModel):
    hypothesis_id: str
    agent_type: str
    summary: str
    rejection_reason: str


class DiagnosisSchema(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    investigation_id: str
    selected_hypothesis_id: str | None
    summary: str
    verified_cause: str
    evidence: list[str]
    rejected_hypotheses: list[RejectedHypothesisSchema]
    changed_files: list[str]
    risk: str
    recommended_action: str
    created_at: datetime


class InvestigationDetailResponse(InvestigationResponse):
    diagnosis: DiagnosisSchema | None = None


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


# ── CP-09 schemas ─────────────────────────────────────────────────────────────


class ExperimentResponse(BaseModel):
    """Response model for a single Experiment record."""

    model_config = {"from_attributes": True}

    id: str
    hypothesis_id: str
    command: str
    working_directory: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    created_at: datetime


class TestEvidenceResponse(BaseModel):
    """Response model for a single TestEvidence record."""

    model_config = {"from_attributes": True}

    id: str
    hypothesis_id: str
    test_path: str
    pre_fix_result: str | None
    post_fix_result: str | None
    existing_suite_result: str | None
    runs: int
    failures_before: int
    failures_after: int
    created_at: datetime


class PatchResponse(BaseModel):
    """Response model for a Patch record."""

    model_config = {"from_attributes": True}

    id: str
    hypothesis_id: str
    branch: str
    commit_sha: str | None
    diff: str
    files_changed: list[str]
    pr_url: str | None
    created_at: datetime


class HypothesisEvidenceResponse(BaseModel):
    """Aggregate evidence response for GET /api/hypotheses/{id}/evidence."""

    hypothesis: HypothesisResponse
    experiments: list[ExperimentResponse]
    test_evidence: list[TestEvidenceResponse]
    patches: list[PatchResponse]


class AgentEventResponse(BaseModel):
    """Response model for a single AgentEvent."""

    model_config = {"from_attributes": True}

    id: str
    investigation_id: str
    agent: str
    action: str
    target: str | None
    payload: dict | None
    timestamp: datetime


class ApproveResponse(BaseModel):
    """Response for POST /api/investigations/{id}/approve."""

    id: str
    status: str


class RejectResponse(BaseModel):
    """Response for POST /api/investigations/{id}/reject."""

    id: str
    status: str


class PullRequestResponse(BaseModel):
    """Response for POST /api/investigations/{id}/pull-request."""

    investigation_id: str
    pr_url: str
    branch: str
