"""
Intake Agent — CP-11

Converts a raw JiraIssue into a normalised BugContext using a single LLM
round-trip.  The agent does NOT perform any file-system or git operations.

No credentials appear in any prompt (AGENTS.md).
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.integrations.jira_client import JiraIssue
from app.schemas.bug_context import BugContext

_LOGGER = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are TraceLab's intake agent. You receive a raw Jira issue and must extract
a normalised bug context by calling emit_bug_context exactly once.

Rules:
- symptom: one sentence describing the observed failure (not the Jira title).
- expected: what should have happened.
- actual: what actually happened (do not repeat the symptom verbatim).
- error_type: MUST be one of "deterministic", "intermittent", "regression".
  - Use "intermittent" if the issue mentions: flaky, race, timing, sometimes,
    occasionally, randomly, intermittent, concurrent.
  - Use "regression" if the issue mentions: used to work, regression, broke in,
    worked before, recent change, broke after.
  - Use "deterministic" otherwise.
- known_evidence: extract concrete facts — log lines, metrics, stack traces,
  reproduction steps — as individual list items. Max 10 items.
- stack_trace: copy the full stack trace verbatim if one is present, else null.
- affected_area: component or service name — derive from components field first,
  then infer from summary/description.
NEVER invent information that is not present in the Jira fields.
"""

# ── Terminal tool schema ──────────────────────────────────────────────────────

_EMIT_BUG_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_bug_context",
        "description": "Emit the normalised bug context. Call exactly once.",
        "parameters": {
            "type": "object",
            "required": [
                "symptom",
                "expected",
                "actual",
                "affected_area",
                "error_type",
                "known_evidence",
            ],
            "properties": {
                "symptom": {
                    "type": "string",
                    "description": "One sentence describing the observed failure.",
                },
                "expected": {
                    "type": "string",
                    "description": "What should have happened.",
                },
                "actual": {
                    "type": "string",
                    "description": "What actually happened.",
                },
                "affected_area": {
                    "type": "string",
                    "description": "Component or service name.",
                },
                "error_type": {
                    "type": "string",
                    "enum": ["deterministic", "intermittent", "regression"],
                    "description": "Classification of the failure pattern.",
                },
                "known_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Concrete facts extracted from the issue. Max 10.",
                },
                "stack_trace": {
                    "type": "string",
                    "description": "Full stack trace if present, else null.",
                    "nullable": True,
                },
            },
        },
    },
}


# ── Agent ─────────────────────────────────────────────────────────────────────


class IntakeAgent:
    """
    Converts a JiraIssue into a BugContext using a single LLM call.

    No credentials appear in the user message (AGENTS.md).
    Falls back to a heuristic BugContext if the LLM response cannot be parsed.
    """

    async def run(
        self,
        issue: JiraIssue,
        repository: str,
        base_branch: str,
    ) -> BugContext:
        """
        Normalise a JiraIssue into a BugContext.

        Uses tool_choice="required" targeting emit_bug_context so the LLM
        always produces structured output in a single round-trip.
        """
        _LOGGER.info("IntakeAgent normalising issue %s", issue.issue_id)

        user_message = self._build_user_message(issue)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        try:
            response = await self._chat(messages)
            choice = response["choices"][0]
            tool_calls = (choice.get("message") or {}).get("tool_calls") or []

            for tc in tool_calls:
                if tc.get("function", {}).get("name") == "emit_bug_context":
                    args_raw = tc["function"].get("arguments", "{}")
                    args = json.loads(args_raw)
                    return BugContext(
                        issue_id=issue.issue_id,
                        symptom=args.get("symptom", issue.summary),
                        expected=args.get("expected", "No errors or unexpected behaviour."),
                        actual=args.get("actual", issue.summary),
                        affected_area=args.get(
                            "affected_area",
                            (issue.components[0] if issue.components else "unknown"),
                        ),
                        error_type=args.get("error_type", "deterministic"),
                        known_evidence=args.get("known_evidence", []),
                        stack_trace=args.get("stack_trace") or None,
                        repository=repository,
                        base_branch=base_branch,
                    )

        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "IntakeAgent LLM call failed for %s — using heuristic fallback",
                issue.issue_id,
                exc_info=True,
            )

        # Heuristic fallback — use raw Jira fields directly
        return self._fallback(issue, repository, base_branch)

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_user_message(self, issue: JiraIssue) -> str:
        """
        Build the prompt from JiraIssue fields.
        NOTE: No API keys or credentials appear here (AGENTS.md).
        """
        lines = [
            f"Issue: {issue.issue_id}  ({issue.issue_type} · {issue.priority})",
            f"Summary: {issue.summary}",
            f"Status: {issue.status}",
            f"Components: {', '.join(issue.components) or 'none'}",
            f"Labels: {', '.join(issue.labels) or 'none'}",
            f"Reporter: {issue.reporter}",
            "",
            "Description:",
            issue.description or "(no description)",
        ]
        if issue.comments:
            lines.append(f"\nComments ({len(issue.comments)}):")
            for comment in issue.comments[:5]:
                lines.append(f"- {comment[:500]}")
        if issue.attachments:
            lines.append(f"\nAttachments: {', '.join(issue.attachments)}")
        return "\n".join(lines)

    def _fallback(self, issue: JiraIssue, repository: str, base_branch: str) -> BugContext:
        """Heuristic BugContext when LLM is unavailable or returns no tool call."""
        return BugContext(
            issue_id=issue.issue_id,
            symptom=issue.summary,
            expected="No errors or unexpected behaviour.",
            actual=issue.description[:500] if issue.description else issue.summary,
            affected_area=(issue.components[0] if issue.components else "unknown"),
            error_type="deterministic",
            known_evidence=[],
            stack_trace=None,
            repository=repository,
            base_branch=base_branch,
        )

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """
        POST to the LLM chat completions endpoint.

        The API key is injected as a Bearer token from settings — it NEVER
        appears in messages, logs, or any other string (AGENTS.md).
        """
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": [_EMIT_BUG_CONTEXT_SCHEMA],
            "tool_choice": {"type": "function", "function": {"name": "emit_bug_context"}},
        }
        # SECURITY: Do not log headers — they contain the API key.
        _LOGGER.debug("IntakeAgent POST chat/completions model=%s", settings.llm_model)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.llm_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
