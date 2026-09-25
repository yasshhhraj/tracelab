from pydantic import BaseModel


class BugContext(BaseModel):
    """
    Normalised representation of a bug report.

    Populated by:
    - Direct API callers (POST /api/investigations, body)
    - Jira IntakeAgent (CP-11) parsing raw Jira fields into this shape

    SECURITY: Never placed in agent prompt context with credentials attached.
    The Jira/GitHub tokens are injected server-side only (AGENTS.md).
    """

    issue_id: str
    symptom: str  # one-sentence description of the observed failure
    expected: str  # what should have happened
    actual: str  # what actually happened
    affected_area: str  # e.g. "payment service", "review endpoint"
    error_type: str  # "deterministic" | "intermittent" | "regression"
    known_evidence: list[str]  # e.g. log snippets, metrics, reproduction steps
    stack_trace: str | None = None
    repository: str  # URL or local path
    base_branch: str  # e.g. "main"
