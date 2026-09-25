import json
import logging
from collections.abc import Callable
from typing import Any

import httpx

from app.agents.base import AgentContext, AgentEvent, AgentModel, AgentResult, ExperimentRecord
from app.config import settings
from app.schemas.hypothesis import Hypothesis

logger = logging.getLogger(__name__)

# ── tool schema helpers ───────────────────────────────────────────────────────


def _make_tool_schema(fn: Callable) -> dict[str, Any]:
    """
    Build an OpenAI function-calling schema from a Python callable.

    Reads the ``__tool_schema__`` attribute set by the agent on its tool
    wrappers. Falls back to a minimal schema using the function name and
    the first line of the docstring.
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


# ── terminal tool: emit_hypothesis ────────────────────────────────────────────

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

# ── safety cap on agentic loop iterations ────────────────────────────────────
_MAX_ITERATIONS = 12


class OpenAIAgentModel(AgentModel):
    """
    Concrete AgentModel backed by the OpenAI Chat Completions API with
    function-calling (tool use).

    Construction::

        model = OpenAIAgentModel(agent_type="code_path", system_prompt="...")

    The API key is read from ``settings.llm_api_key`` — it NEVER appears in
    any message passed to this class, any log line, or any DB record
    (AGENTS.md constraint).

    Agentic loop:
        1. Build messages: [system, user(bug_context)]
        2. POST /chat/completions with tools=[investigation_tools..., emit_hypothesis]
        3. If the model calls an investigation tool → execute it, append result, go to 2
        4. If the model calls emit_hypothesis → parse args → return AgentResult
        5. If the model replies with text only → emit a low-confidence INCONCLUSIVE hypothesis
        6. Safety: after _MAX_ITERATIONS, force emit with a low-confidence hypothesis
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
        tool_index: dict[str, Callable] = {fn.__name__: fn for fn in tools}
        tool_schemas = [_make_tool_schema(fn) for fn in tools]
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
                # Model replied with plain text — emit inconclusive hypothesis
                logger.warning(
                    "Agent %s produced no tool call on iteration %d; emitting inconclusive",
                    self._agent_type,
                    iteration,
                )
                hypothesis = Hypothesis(
                    agent_type=self._agent_type,  # type: ignore[arg-type]
                    summary=(
                        "Investigation incomplete — model did not emit a structured hypothesis."
                    ),
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
                        # Record CommandResult-like dicts as experiments
                        if isinstance(raw_result, dict) and "exit_code" in raw_result:
                            experiments.append(
                                ExperimentRecord(
                                    command=raw_result.get("command", []),
                                    cwd=str(raw_result.get("cwd", "")),
                                    exit_code=raw_result["exit_code"],
                                    stdout=raw_result.get("stdout", ""),
                                    stderr=raw_result.get("stderr", ""),
                                    duration_ms=raw_result.get("duration_ms", 0),
                                    timed_out=raw_result.get("timed_out", False),
                                )
                            )
                    except Exception as exc:  # noqa: BLE001
                        tool_result = f"ERROR: {exc}"
                        logger.warning("Tool %s raised: %s", fn_name, exc, exc_info=True)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(tool_result),
                    }
                )

        # Safety fallback after _MAX_ITERATIONS
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

        The API key is read from settings.llm_api_key and injected as a
        Bearer token — it is NEVER logged or placed in any message (AGENTS.md).
        """
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        # SECURITY: Do not log headers (would expose the API key).
        logger.debug("POST %s/chat/completions model=%s", settings.llm_base_url, settings.llm_model)

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.llm_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
