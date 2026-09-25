# CP-05 — AgentModel Abstraction + Single Investigation Agent

> **Goal:** Implement the `AgentModel` interface, wire up the first working investigation agent (`CodePathAgent`) that produces a structured `Hypothesis`, and expose a CLI entry-point for manual end-to-end runs.  
> **Exit state:** `pytest tests/test_code_path_agent.py` passes with a mocked LLM; `Hypothesis` Pydantic model validates correctly; `python -m app.cli investigate --bug "..." --repo ./fixture` prints valid JSON; `ruff` is clean.  
> **Builds on:** CP-04 (`WorktreeManager`), CP-03 (tool layer), CP-02 (DB models + `InvestigationStatus` / `HypothesisStatus`), CP-01 (`Settings`).

---

## 1. What CP-05 Delivers

| Deliverable | File | Purpose |
|---|---|---|
| Pydantic schemas | `app/schemas/bug_context.py` | Canonical `BugContext` input structure |
| Pydantic schemas | `app/schemas/hypothesis.py` | Canonical `Hypothesis` output structure |
| Agent abstractions | `app/agents/base.py` | `AgentContext`, `AgentResult`, `AgentModel` ABC |
| LLM provider | `app/agents/llm/openai_model.py` | `OpenAIAgentModel` — tool-calling via OpenAI |
| First agent | `app/agents/code_path_agent.py` | `CodePathAgent` — code-path investigator |
| CLI | `app/cli.py` | `python -m app.cli investigate` command |
| Tests | `tests/test_code_path_agent.py` | Mock-based hypothesis validation tests |

---

## 2. Directory Layout After CP-05

```
backend/
├── app/
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── bug_context.py       ← NEW
│   │   └── hypothesis.py        ← NEW
│   ├── agents/
│   │   ├── __init__.py          ← NEW
│   │   ├── base.py              ← NEW
│   │   ├── code_path_agent.py   ← NEW
│   │   └── llm/
│   │       ├── __init__.py      ← NEW
│   │       └── openai_model.py  ← NEW
│   └── cli.py                   ← NEW
└── tests/
    └── test_code_path_agent.py  ← NEW
```

---

## 3. File-by-File Specification

### 3.1 `backend/app/schemas/__init__.py`

```python
# empty — marks schemas as a package
```

---

### 3.2 `backend/app/schemas/bug_context.py`

```python
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
    symptom: str                     # one-sentence description of the observed failure
    expected: str                    # what should have happened
    actual: str                      # what actually happened
    affected_area: str               # e.g. "payment service", "review endpoint"
    error_type: str                  # "deterministic" | "intermittent" | "regression"
    known_evidence: list[str]        # e.g. log snippets, metrics, reproduction steps
    stack_trace: str | None = None
    repository: str                  # URL or local path
    base_branch: str                 # e.g. "main"
```

---

### 3.3 `backend/app/schemas/hypothesis.py`

```python
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.db.models import HypothesisStatus


class Hypothesis(BaseModel):
    """
    Structured output produced by an investigation agent.

    NOTE: `confidence` is INFORMATIONAL ONLY.
    The Arbiter (CP-08) MUST NOT use it for winner selection (AGENTS.md constraint).
    Selection is driven solely by evidence: reproduction + test proof.
    """

    hypothesis_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: Literal["code_path", "git_history", "test_behavior"]
    summary: str                               # ≤ 2 sentences, human-readable
    suspected_files: list[str]                 # relative file paths
    reasoning_summary: str                     # multi-sentence causal chain
    reproduction_plan: list[str]               # ordered steps to reproduce
    candidate_fix: str                         # unified diff or prose description
    confidence: Literal["low", "medium", "high"]  # informational only — never used for selection
    status: str = HypothesisStatus.PROPOSED
```

---

### 3.4 `backend/app/agents/__init__.py`

```python
# empty — marks agents as a package
```

---

### 3.5 `backend/app/agents/base.py`

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

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

    agent: str          # e.g. "code_path"
    action: str         # e.g. "search_code", "read_file", "llm_call"
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
    repo_path: Path        # read-only reference repo (never modified directly)
    worktree_path: Path    # isolated worktree this agent may read/write
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
```

---

### 3.6 `backend/app/agents/llm/__init__.py`

```python
# empty — marks llm sub-package
```

---

### 3.7 `backend/app/agents/llm/openai_model.py`

This is the concrete LLM provider. It implements the agentic loop: build system prompt → call the LLM with tool schemas → process tool calls → loop until the model emits a final structured `Hypothesis` via function-calling.

```python
import json
import logging
from typing import Any, Callable

import httpx

from app.agents.base import AgentContext, AgentEvent, AgentModel, AgentResult, ExperimentRecord
from app.config import settings
from app.schemas.hypothesis import Hypothesis

logger = logging.getLogger(__name__)

# ── tool schema helpers ───────────────────────────────────────────────────────


def _make_tool_schema(fn: Callable) -> dict[str, Any]:
    """
    Build an OpenAI function-calling schema from a Python callable.

    Relies on the callable having a `__tool_schema__` attribute dict
    (set by the agent when it creates tool wrappers), or falls back to
    a minimal schema using the function name and docstring.
    """
    if hasattr(fn, "__tool_schema__"):
        return fn.__tool_schema__
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip().split("\n")[0],
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


# ── hypothesis output schema (sent as a tool for structured extraction) ───────

_EMIT_HYPOTHESIS_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_hypothesis",
        "description": (
            "Call this once you have finished investigating to emit your "
            "structured hypothesis. This terminates the investigation loop."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "summary",
                "suspected_files",
                "reasoning_summary",
                "reproduction_plan",
                "candidate_fix",
                "confidence",
            ],
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "1-2 sentence human-readable hypothesis statement.",
                },
                "suspected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relative file paths most likely containing the defect.",
                },
                "reasoning_summary": {
                    "type": "string",
                    "description": "Multi-sentence causal chain explaining the bug.",
                },
                "reproduction_plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered steps to reproduce the bug.",
                },
                "candidate_fix": {
                    "type": "string",
                    "description": "Unified diff or prose description of the proposed fix.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Informational only — not used for selection.",
                },
            },
        },
    },
}

# ── max agentic loop iterations (safety guard) ────────────────────────────────
_MAX_ITERATIONS = 12


class OpenAIAgentModel(AgentModel):
    """
    Concrete AgentModel backed by the OpenAI Chat Completions API with
    function-calling (tool use).

    Construction:
        model = OpenAIAgentModel(agent_type="code_path", system_prompt="...")

    The API key is read from Settings.llm_api_key — it NEVER appears in
    any message passed to this class, any log line, or any DB record.

    The agentic loop:
        1. Build messages: [system, user(bug_context)]
        2. POST /chat/completions with tools=[investigation_tools..., emit_hypothesis]
        3. If the model calls an investigation tool → execute it, append result, go to 2
        4. If the model calls emit_hypothesis → parse args → return AgentResult
        5. If the model replies with text only (no tool call) → treat as fallback and
           construct a minimal INCONCLUSIVE hypothesis
        6. Safety: after _MAX_ITERATIONS, force emit with whatever was collected
    """

    def __init__(self, agent_type: str, system_prompt: str) -> None:
        self._agent_type = agent_type
        self._system_prompt = system_prompt

    async def run(
        self,
        context: AgentContext,
        tools: list[Callable],
    ) -> AgentResult:
        """Execute the agentic investigation loop."""
        # Build tool index: name → callable
        tool_index: dict[str, Callable] = {fn.__name__: fn for fn in tools}

        # Build OpenAI tool schemas for investigation tools
        tool_schemas = [_make_tool_schema(fn) for fn in tools]
        # Always append the terminal emit_hypothesis tool
        tool_schemas.append(_EMIT_HYPOTHESIS_SCHEMA)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": self._build_user_message(context)},
        ]

        experiments: list[ExperimentRecord] = []
        events: list[AgentEvent] = []

        for iteration in range(_MAX_ITERATIONS):
            response = await self._chat(messages, tool_schemas)
            choice = response["choices"][0]
            message = choice["message"]
            messages.append(message)

            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # Model replied with plain text — no tool call; emit inconclusive
                logger.warning(
                    "Agent %s produced no tool call on iteration %d; emitting inconclusive",
                    self._agent_type,
                    iteration,
                )
                hypothesis = Hypothesis(
                    agent_type=self._agent_type,  # type: ignore[arg-type]
                    summary="Investigation incomplete — model did not emit a structured hypothesis.",
                    suspected_files=[],
                    reasoning_summary=message.get("content") or "",
                    reproduction_plan=[],
                    candidate_fix="",
                    confidence="low",
                )
                return AgentResult(hypothesis=hypothesis, experiments=experiments, events=events)

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args_raw = tc["function"].get("arguments", "{}")
                try:
                    fn_args = json.loads(fn_args_raw)
                except json.JSONDecodeError:
                    fn_args = {}

                # ── terminal call: emit_hypothesis ────────────────────────────
                if fn_name == "emit_hypothesis":
                    hypothesis = Hypothesis(
                        agent_type=self._agent_type,  # type: ignore[arg-type]
                        **fn_args,
                    )
                    events.append(
                        AgentEvent(
                            agent=self._agent_type,
                            action="emit_hypothesis",
                            target=None,
                            payload={"hypothesis_id": hypothesis.hypothesis_id},
                        )
                    )
                    return AgentResult(
                        hypothesis=hypothesis,
                        experiments=experiments,
                        events=events,
                    )

                # ── investigation tool call ───────────────────────────────────
                if fn_name not in tool_index:
                    tool_result = f"ERROR: unknown tool '{fn_name}'"
                    logger.warning("Agent %s called unknown tool: %s", self._agent_type, fn_name)
                else:
                    callable_fn = tool_index[fn_name]
                    events.append(
                        AgentEvent(
                            agent=self._agent_type,
                            action=fn_name,
                            target=fn_args.get("path") or fn_args.get("pattern"),
                            payload=fn_args,
                        )
                    )
                    try:
                        raw_result = await callable_fn(**fn_args)
                        tool_result = (
                            raw_result
                            if isinstance(raw_result, str)
                            else json.dumps(raw_result, default=str)
                        )
                        # If the tool returns a CommandResult-like dict, record as experiment
                        if isinstance(raw_result, dict) and "exit_code" in raw_result:
                            experiments.append(ExperimentRecord(**raw_result))
                    except Exception as exc:  # noqa: BLE001
                        tool_result = f"ERROR: {exc}"
                        logger.warning(
                            "Tool %s raised: %s", fn_name, exc, exc_info=True
                        )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(tool_result),
                    }
                )

        # Safety fallback after max iterations
        hypothesis = Hypothesis(
            agent_type=self._agent_type,  # type: ignore[arg-type]
            summary="Investigation exceeded maximum iterations without reaching a conclusion.",
            suspected_files=[],
            reasoning_summary="Max iteration limit reached.",
            reproduction_plan=[],
            candidate_fix="",
            confidence="low",
        )
        return AgentResult(hypothesis=hypothesis, experiments=experiments, events=events)

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_user_message(self, context: AgentContext) -> str:
        """
        Build the investigation prompt from the bug context.
        NOTE: No API keys or credentials appear here (AGENTS.md).
        """
        bc = context.bug_context
        lines = [
            f"Issue: {bc.issue_id}",
            f"Symptom: {bc.symptom}",
            f"Expected: {bc.expected}",
            f"Actual: {bc.actual}",
            f"Affected area: {bc.affected_area}",
            f"Error type: {bc.error_type}",
            f"Repository: {bc.repository}  (branch: {bc.base_branch})",
            f"Worktree path: {context.worktree_path}",
        ]
        if bc.stack_trace:
            lines.append(f"\nStack trace:\n{bc.stack_trace}")
        if bc.known_evidence:
            lines.append("\nKnown evidence:")
            for e in bc.known_evidence:
                lines.append(f"  - {e}")
        lines.append(
            "\nUse the available tools to investigate the code, then call "
            "emit_hypothesis with your findings."
        )
        return "\n".join(lines)

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        POST to the OpenAI Chat Completions API.

        The API key is read from settings.llm_api_key — it is injected as a
        header and NEVER logged or placed in any message (AGENTS.md).
        """
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        # SECURITY: Redact the auth header from any debug logging.
        logger.debug("POST %s/chat/completions model=%s", settings.llm_base_url, settings.llm_model)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.llm_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
```

---

### 3.8 `backend/app/agents/code_path_agent.py`

The Code-Path Agent is the first concrete agent. It investigates the bug by traversing source code, reading files, and inspecting diffs.

```python
from pathlib import Path
from typing import Callable

from app.agents.base import AgentContext, AgentModel, AgentResult
from app.agents.llm.openai_model import OpenAIAgentModel
from app.tools.code_tools import read_file, search_code
from app.tools.git_tools import git_diff

# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a code-path investigation agent for TraceLab, an AI debugging platform.

Your job:
1. Analyse the bug report provided by the user.
2. Use the available tools (search_code, read_file, git_diff) to trace the code
   path most likely responsible for the reported failure.
3. Identify the specific files, functions, and lines where the bug manifests.
4. Formulate a concrete candidate fix (unified diff or clear prose description).
5. When you have enough evidence, call emit_hypothesis with your findings.

Guidelines:
- Focus on CODE PATH — how data flows from entry point to failure.
- Do NOT speculate about causes you cannot verify with tool output.
- suspected_files must list files you actually inspected, not guesses.
- candidate_fix must be specific enough for the Verification Engine to apply.
- confidence is INFORMATIONAL only — report it honestly but it does NOT affect
  whether your hypothesis is selected.
"""

# ── tool wrappers (add __tool_schema__ for OpenAI function definitions) ────────


def _wrap_search_code() -> Callable:
    async def search_code_tool(pattern: str, path: str) -> str:
        """Search for a regex pattern in the codebase under `path`."""
        result = await search_code(pattern=pattern, path=Path(path))
        return result

    search_code_tool.__name__ = "search_code"
    search_code_tool.__tool_schema__ = {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern in files under the given path.",
            "parameters": {
                "type": "object",
                "required": ["pattern", "path"],
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "path": {"type": "string", "description": "Directory or file path to search."},
                },
            },
        },
    }
    return search_code_tool


def _wrap_read_file() -> Callable:
    async def read_file_tool(path: str) -> str:
        """Read the contents of a file."""
        result = await read_file(path=Path(path))
        return result

    read_file_tool.__name__ = "read_file"
    read_file_tool.__tool_schema__ = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a source file.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path."},
                },
            },
        },
    }
    return read_file_tool


def _wrap_git_diff() -> Callable:
    async def git_diff_tool(repo_path: str, base: str, head: str, file_path: str = "") -> str:
        """Get the git diff between two refs for an optional file path."""
        result = await git_diff(
            repo_path=Path(repo_path),
            base=base,
            head=head,
            file_path=file_path or None,
        )
        return result

    git_diff_tool.__name__ = "git_diff"
    git_diff_tool.__tool_schema__ = {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Get the unified diff between two git refs.",
            "parameters": {
                "type": "object",
                "required": ["repo_path", "base", "head"],
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository."},
                    "base": {"type": "string", "description": "Base ref (commit SHA or branch)."},
                    "head": {"type": "string", "description": "Head ref (commit SHA or branch)."},
                    "file_path": {
                        "type": "string",
                        "description": "Optional file path to restrict the diff.",
                    },
                },
            },
        },
    }
    return git_diff_tool


# ── factory ───────────────────────────────────────────────────────────────────


def make_code_path_agent() -> tuple[AgentModel, list[Callable]]:
    """
    Instantiate a CodePathAgent backed by OpenAIAgentModel.

    Returns:
        (agent, tools) — pass both to agent.run(context, tools)
    """
    model = OpenAIAgentModel(agent_type="code_path", system_prompt=_SYSTEM_PROMPT)
    tools: list[Callable] = [
        _wrap_search_code(),
        _wrap_read_file(),
        _wrap_git_diff(),
    ]
    return model, tools


async def run_code_path_agent(context: AgentContext) -> AgentResult:
    """Convenience function: create agent + tools, run, return result."""
    agent, tools = make_code_path_agent()
    return await agent.run(context, tools)
```

---

### 3.9 `backend/app/cli.py`

```python
"""
TraceLab CLI

Entry point:
    python -m app.cli investigate --bug "..." --repo ./path/to/repo [--branch main]

Prints the resulting Hypothesis as formatted JSON to stdout.

Manual E2E check — NOT run in CI (requires a real LLM API key and a real repo).
"""

import asyncio
import json
from pathlib import Path

import click

from app.agents.base import AgentContext
from app.agents.code_path_agent import run_code_path_agent
from app.schemas.bug_context import BugContext


@click.group()
def cli() -> None:
    """TraceLab — multi-agent differential debugging."""


@cli.command()
@click.option("--issue-id", default="CLI-1", show_default=True, help="Issue identifier.")
@click.option("--bug", required=True, help="One-sentence symptom description.")
@click.option("--expected", default="", help="Expected behaviour.")
@click.option("--actual", default="", help="Actual behaviour.")
@click.option("--area", default="unknown", help="Affected system area.")
@click.option(
    "--error-type",
    default="deterministic",
    type=click.Choice(["deterministic", "intermittent", "regression"]),
    help="Error classification.",
)
@click.option("--repo", required=True, type=click.Path(exists=True), help="Repository path.")
@click.option("--branch", default="main", show_default=True, help="Base branch.")
@click.option("--investigation-id", default="cli-investigation-1", help="Investigation ID.")
def investigate(
    issue_id: str,
    bug: str,
    expected: str,
    actual: str,
    area: str,
    error_type: str,
    repo: str,
    branch: str,
    investigation_id: str,
) -> None:
    """Run a single CodePathAgent investigation and print the hypothesis JSON."""
    repo_path = Path(repo).resolve()

    bug_context = BugContext(
        issue_id=issue_id,
        symptom=bug,
        expected=expected or "No errors or unexpected behaviour.",
        actual=actual or bug,
        affected_area=area,
        error_type=error_type,
        known_evidence=[],
        repository=str(repo_path),
        base_branch=branch,
    )

    context = AgentContext(
        bug_context=bug_context,
        repo_path=repo_path,
        worktree_path=repo_path,   # CLI uses the main repo directly (no worktree)
        investigation_id=investigation_id,
    )

    result = asyncio.run(run_code_path_agent(context))

    click.echo(
        json.dumps(result.hypothesis.model_dump(), indent=2, default=str)
    )


if __name__ == "__main__":
    cli()
```

---

### 3.10 `backend/tests/test_code_path_agent.py`

All tests mock the LLM — no real API calls in CI. The mock replaces `OpenAIAgentModel._chat` so the schema validation, tool dispatch, and `AgentResult` assembly are all exercised.

```python
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base import AgentContext, AgentResult
from app.agents.code_path_agent import make_code_path_agent, run_code_path_agent
from app.agents.llm.openai_model import OpenAIAgentModel
from app.db.models import HypothesisStatus
from app.schemas.bug_context import BugContext
from app.schemas.hypothesis import Hypothesis


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def bug_context() -> BugContext:
    return BugContext(
        issue_id="TST-1",
        symptom="Duplicate rows inserted on concurrent requests",
        expected="Only one row per idempotency key",
        actual="Two rows inserted under concurrent load",
        affected_area="review_service",
        error_type="deterministic",
        known_evidence=["Seen in staging with 2 concurrent requests"],
        repository="/tmp/fixture-repo",
        base_branch="main",
    )


@pytest.fixture
def agent_context(bug_context: BugContext, tmp_path: Path) -> AgentContext:
    # Create minimal fake repo structure
    (tmp_path / "service.py").write_text("def process():\n    pass\n")
    return AgentContext(
        bug_context=bug_context,
        repo_path=tmp_path,
        worktree_path=tmp_path,
        investigation_id="test-inv-001",
    )


def _make_emit_response(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a fake OpenAI response that calls emit_hypothesis."""
    args = {
        "summary": "The duplicate row bug is caused by missing unique constraint.",
        "suspected_files": ["service.py"],
        "reasoning_summary": "The service inserts without checking for existing rows first.",
        "reproduction_plan": ["Send 2 concurrent POST requests", "Observe duplicate rows"],
        "candidate_fix": "Add a unique DB constraint on (idempotency_key).",
        "confidence": "high",
        **(overrides or {}),
    }
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": "emit_hypothesis",
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _make_tool_call_then_emit(tool_name: str, tool_args: dict[str, Any]) -> list[dict[str, Any]]:
    """Build two fake LLM responses: first calls a tool, second emits hypothesis."""
    tool_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_tool_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }
                    ],
                }
            }
        ]
    }
    emit_response = _make_emit_response()
    return [tool_response, emit_response]


# ── schema tests ──────────────────────────────────────────────────────────────


def test_hypothesis_schema_valid():
    """Hypothesis Pydantic model validates correctly with all required fields."""
    h = Hypothesis(
        agent_type="code_path",
        summary="Bug in service.py",
        suspected_files=["service.py"],
        reasoning_summary="Missing check before insert.",
        reproduction_plan=["Step 1", "Step 2"],
        candidate_fix="Add unique constraint.",
        confidence="medium",
    )
    assert h.status == HypothesisStatus.PROPOSED
    assert h.agent_type == "code_path"
    assert len(h.hypothesis_id) == 36  # UUID format


def test_hypothesis_invalid_agent_type():
    """Invalid agent_type raises ValidationError."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Hypothesis(
            agent_type="unknown_agent",   # not in Literal
            summary="x",
            suspected_files=[],
            reasoning_summary="x",
            reproduction_plan=[],
            candidate_fix="x",
            confidence="medium",
        )


def test_hypothesis_invalid_confidence():
    """Invalid confidence raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Hypothesis(
            agent_type="code_path",
            summary="x",
            suspected_files=[],
            reasoning_summary="x",
            reproduction_plan=[],
            candidate_fix="x",
            confidence="very_high",  # not in Literal
        )


def test_bug_context_schema_valid():
    """BugContext validates correctly."""
    bc = BugContext(
        issue_id="TST-1",
        symptom="crash",
        expected="no crash",
        actual="crash on startup",
        affected_area="init",
        error_type="deterministic",
        known_evidence=[],
        repository="https://github.com/org/repo",
        base_branch="main",
    )
    assert bc.stack_trace is None


# ── agent result tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_returns_valid_result(agent_context: AgentContext):
    """Agent with mocked LLM that immediately emits hypothesis returns valid AgentResult."""
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_make_emit_response())):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    assert isinstance(result, AgentResult)
    assert isinstance(result.hypothesis, Hypothesis)
    assert result.hypothesis.agent_type == "code_path"
    assert result.hypothesis.status == HypothesisStatus.PROPOSED
    assert len(result.hypothesis.suspected_files) > 0


@pytest.mark.asyncio
async def test_agent_records_emit_event(agent_context: AgentContext):
    """Agent emits an AgentEvent with action='emit_hypothesis' on completion."""
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_make_emit_response())):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    emit_events = [e for e in result.events if e.action == "emit_hypothesis"]
    assert len(emit_events) == 1
    assert emit_events[0].agent == "code_path"


@pytest.mark.asyncio
async def test_agent_calls_search_code_tool(agent_context: AgentContext, tmp_path: Path):
    """When LLM calls search_code, the agent dispatches it and records the call."""
    responses = iter(
        _make_tool_call_then_emit(
            "search_code",
            {"pattern": "def process", "path": str(tmp_path)},
        )
    )

    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(side_effect=lambda *a, **kw: next(responses))):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    search_events = [e for e in result.events if e.action == "search_code"]
    assert len(search_events) == 1
    assert result.hypothesis.agent_type == "code_path"


@pytest.mark.asyncio
async def test_agent_handles_no_tool_call_gracefully(agent_context: AgentContext):
    """When LLM returns plain text (no tool call), agent emits an INCONCLUSIVE hypothesis."""
    plain_text_response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "I cannot determine the cause.",
                    "tool_calls": [],
                }
            }
        ]
    }
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=plain_text_response)):
        agent, tools = make_code_path_agent()
        result = await agent.run(agent_context, tools)

    assert isinstance(result.hypothesis, Hypothesis)
    assert result.hypothesis.confidence == "low"


@pytest.mark.asyncio
async def test_run_code_path_agent_convenience_fn(agent_context: AgentContext):
    """Convenience function run_code_path_agent works and returns AgentResult."""
    with patch.object(OpenAIAgentModel, "_chat", new=AsyncMock(return_value=_make_emit_response())):
        result = await run_code_path_agent(agent_context)

    assert isinstance(result, AgentResult)
    assert result.hypothesis.agent_type == "code_path"


@pytest.mark.asyncio
async def test_make_code_path_agent_returns_three_tools():
    """make_code_path_agent returns an agent and exactly 3 tools."""
    agent, tools = make_code_path_agent()
    assert isinstance(agent, OpenAIAgentModel)
    tool_names = {fn.__name__ for fn in tools}
    assert tool_names == {"search_code", "read_file", "git_diff"}
```

---

## 4. Dependencies to Add

`httpx` is already present in `pyproject.toml` (added in CP-01). No new packages are required.

---

## 5. Implementation Order

| Step | Action | Verify |
|---|---|---|
| 1 | Create `app/schemas/__init__.py` | — |
| 2 | Create `app/schemas/bug_context.py` | `python -c "from app.schemas.bug_context import BugContext"` |
| 3 | Create `app/schemas/hypothesis.py` | `python -c "from app.schemas.hypothesis import Hypothesis"` |
| 4 | Create `app/agents/__init__.py` | — |
| 5 | Create `app/agents/base.py` | `python -c "from app.agents.base import AgentModel"` |
| 6 | Create `app/agents/llm/__init__.py` | — |
| 7 | Create `app/agents/llm/openai_model.py` | `python -c "from app.agents.llm.openai_model import OpenAIAgentModel"` |
| 8 | Create `app/agents/code_path_agent.py` | `python -c "from app.agents.code_path_agent import make_code_path_agent"` |
| 9 | Create `app/cli.py` | `python -m app.cli --help` |
| 10 | Create `tests/test_code_path_agent.py` | — |
| 11 | `pytest tests/test_code_path_agent.py -v` | All pass |
| 12 | `pytest` (full suite) | All pass (≥ 70 tests) |
| 13 | `ruff check . && ruff format --check .` | Clean |

---

## 6. Key Design Decisions

### Why `__tool_schema__` on callables instead of a separate registry?

The agent wraps each tool function with a closure that carries both the callable and its OpenAI schema as a `__tool_schema__` attribute. This keeps tool definition and implementation co-located (in `code_path_agent.py`) with zero indirection. The `OpenAIAgentModel` reads the schema at run time — it never needs to know which agent it belongs to.

### Why `emit_hypothesis` as a tool rather than structured JSON output?

OpenAI's `response_format: json_object` is stateless and cannot coexist cleanly with multi-turn tool calling. Using `emit_hypothesis` as a terminal tool integrates naturally into the tool-use loop: the model can call investigation tools N times, then signal completion by calling `emit_hypothesis`. No special parsing of free-text JSON is needed.

### Why `_MAX_ITERATIONS = 12`?

This bounds LLM cost and wall-clock time. With three investigation tools and one terminal tool, a thorough investigation rarely exceeds 5–6 iterations. 12 gives headroom for complex bugs without runaway loops. After the limit the agent emits a low-confidence INCONCLUSIVE hypothesis rather than hanging forever.

### Why no credential forwarding in `_chat`?

`settings.llm_api_key` is accessed inside `_chat` only, placed in the `Authorization` header, and never stored in `messages` or any DB record. This satisfies the AGENTS.md constraint that credentials must never appear in agent prompt context.

### Why `worktree_path=repo_path` in the CLI?

The CLI is for quick manual E2E checks, not production use. The orchestrator (CP-06) supplies real isolated worktree paths. Using the main repo for the CLI avoids requiring a git repo with worktree support just for a test run.

### Why no DB writes in CP-05?

The agent layer is deliberately stateless — it produces `AgentResult` and returns it. DB persistence (inserting `Hypothesis`, `Experiment`, `AgentEvent` rows) is the orchestrator's job (CP-06). This separation makes the agent layer fully unit-testable without a database.

---

## 7. Acceptance Criteria (Definition of Done)

```
[ ] app/schemas/bug_context.py — BugContext model importable and valid
[ ] app/schemas/hypothesis.py  — Hypothesis model importable and valid
[ ] app/agents/base.py         — AgentModel ABC, AgentContext, AgentResult importable
[ ] app/agents/llm/openai_model.py — OpenAIAgentModel importable; API key never in messages
[ ] app/agents/code_path_agent.py  — make_code_path_agent() returns (agent, 3 tools)
[ ] app/cli.py                 — `python -m app.cli --help` works
[ ] tests/test_code_path_agent.py — all tests pass with mocked LLM (no real API calls in CI)
[ ] Hypothesis validation: invalid agent_type raises ValidationError
[ ] Hypothesis validation: invalid confidence raises ValidationError
[ ] Agent handles plain-text LLM response gracefully (INCONCLUSIVE hypothesis)
[ ] Agent dispatches search_code tool call and records AgentEvent
[ ] emit_hypothesis tool call terminates the loop and returns AgentResult
[ ] pytest (full suite) → all tests pass, 0 failures
[ ] ruff check . && ruff format --check . → clean
```

---

## 8. What CP-05 Deliberately Does NOT Include

| Excluded | Added in |
|---|---|
| GitHistoryAgent, TestBehaviorAgent | CP-06 (parallel agents) |
| asyncio.gather for parallel agent runs | CP-06 |
| DB persistence of Hypothesis / Experiments | CP-06 (orchestrator) |
| Verification engine (fail-before / pass-after) | CP-07 |
| Arbiter selection | CP-08 |
| REST API endpoints | CP-09 |
| WatsonxAgentModel | Post-MVP (drop-in swap via AgentModel ABC) |
| Retry logic on LLM rate-limit errors | Post-MVP |
