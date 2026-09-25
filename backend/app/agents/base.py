from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.schemas.bug_context import BugContext
from app.schemas.hypothesis import Hypothesis


class ExperimentRecord(BaseModel):
    """
    Lightweight record of a single command execution performed during investigation.
    Persisted to the `experiments` table by the orchestrator (CP-06).
    """

    command: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class AgentEvent(BaseModel):
    """
    Structured log entry emitted by an agent during its run.
    Persisted to the `agent_events` table by the orchestrator (CP-06).
    """

    agent: str  # e.g. "code_path"
    action: str  # e.g. "search_code", "read_file", "llm_call"
    target: str | None  # file path, search pattern, etc.
    payload: dict[str, Any] | None = None


class AgentContext(BaseModel):
    """
    Everything an agent needs to investigate a bug.

    SECURITY: No API keys or tokens appear here — they stay in Settings
    and are injected by the concrete AgentModel class (AGENTS.md).
    """

    model_config = {"arbitrary_types_allowed": True}

    bug_context: BugContext
    repo_path: Path  # read-only reference repo (never modified directly)
    worktree_path: Path  # isolated worktree this agent may read/write
    investigation_id: str


class AgentResult(BaseModel):
    """Structured output returned by AgentModel.run()."""

    hypothesis: Hypothesis
    experiments: list[ExperimentRecord] = []
    events: list[AgentEvent] = []


class AgentModel(ABC):
    """
    Abstract base for every LLM-backed investigation agent.

    All subclasses must accept tool callables (not raw subprocess) and
    must produce a validated AgentResult.  The model provider (OpenAI,
    Watsonx, etc.) is injected at construction time via concrete subclasses.

    AGENTS.md constraint: model provider must be swappable without touching
    any agent logic — only the concrete AgentModel subclass changes.
    """

    @abstractmethod
    async def run(
        self,
        context: AgentContext,
        tools: list[Callable],
    ) -> AgentResult:
        """
        Execute the investigation and return a structured hypothesis.

        Args:
            context: Bug context + paths — no secrets included.
            tools:   Callables from the allowlisted tool layer (CP-03).
                     Agents must not use raw subprocess or os.system.

        Returns:
            AgentResult with a validated Hypothesis and execution records.
        """
        ...
