"""
RegressionTestGenerator — CP-07

Generates a targeted regression test for a hypothesis using an LLM.

Security (AGENTS.md): API key read from settings only — never placed in any
message, log line, or DB record.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.schemas.bug_context import BugContext
from app.tools.code_tools import read_file as _read_file
from app.tools.code_tools import write_test as _write_test

logger = logging.getLogger(__name__)

# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a regression test engineer for TraceLab, an AI debugging platform.

Your job:
1. Read the bug description and the source files provided.
2. Write a single, focused pytest test function that:
   - Reproduces the described bug on the ORIGINAL (unfixed) code.
   - FAILS on the original code.
   - PASSES after the candidate fix described is applied.
3. Use only the Python standard library and packages already imported in the
   provided source files — do NOT add new dependencies.
4. Do NOT import or apply the fix inside the test — test the code AS-IS.
5. When ready, call emit_test with the file path and complete test content.

Guidelines:
- One test function per file is sufficient — keep it minimal and targeted.
- The test file path must be under the tests/ directory.
- The function name must start with test_.
- Include a docstring explaining what the test proves.
"""

# ── emit_test terminal tool ───────────────────────────────────────────────────

_EMIT_TEST_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_test",
        "description": "Emit the regression test file content.",
        "parameters": {
            "type": "object",
            "required": ["test_file_path", "test_content"],
            "properties": {
                "test_file_path": {
                    "type": "string",
                    "description": (
                        "Relative path for the new test file "
                        "(e.g. 'tests/test_regression_abc123.py')."
                    ),
                },
                "test_content": {
                    "type": "string",
                    "description": "Full Python content of the regression test file.",
                },
            },
        },
    },
}

_MAX_ITERATIONS = 6


class RegressionTestGenerator:
    """
    Generates a pytest regression test for a given hypothesis via LLM.

    Usage::

        gen = RegressionTestGenerator()
        test_path, test_content = await gen.generate(
            hypothesis_id="abc",
            worktree_path=Path("/worktrees/h1"),
            bug_context=bug_ctx,
            candidate_fix="Add unique constraint...",
            suspected_files=["review_service.py"],
            reproduction_plan=["POST /reviews twice with same key"],
        )
    """

    async def generate(
        self,
        hypothesis_id: str,
        worktree_path: Path,
        bug_context: BugContext,
        candidate_fix: str,
        suspected_files: list[str],
        reproduction_plan: list[str],
    ) -> tuple[str, str]:
        """
        Generate a regression test via LLM and write it into the worktree.

        Returns:
            (test_file_path, test_content)
            test_file_path is relative to worktree_path.

        Raises:
            RuntimeError: if the LLM does not emit a test within _MAX_ITERATIONS.
        """
        # Read suspected file contents for context (best-effort)
        file_contents: dict[str, str] = {}
        for rel_path in suspected_files:
            try:
                result = _read_file(root=worktree_path, file_path=rel_path)
                file_contents[rel_path] = str(result.get("content", ""))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not read suspected file %s: %s", rel_path, exc)

        user_message = self._build_prompt(
            hypothesis_id=hypothesis_id,
            bug_context=bug_context,
            candidate_fix=candidate_fix,
            reproduction_plan=reproduction_plan,
            file_contents=file_contents,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        default_path = f"tests/test_regression_{hypothesis_id[:8]}.py"

        for _ in range(_MAX_ITERATIONS):
            response = await self._chat(messages, [_EMIT_TEST_SCHEMA])
            message = response["choices"][0]["message"]
            messages.append(message)

            for tc in message.get("tool_calls") or []:
                if tc["function"]["name"] == "emit_test":
                    try:
                        args = json.loads(tc["function"].get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    test_path = args.get("test_file_path", default_path)
                    test_content = args.get("test_content", "")

                    # Write the test file into the worktree
                    _write_test(
                        root=worktree_path,
                        file_path=test_path,
                        content=test_content,
                        overwrite=True,
                    )
                    logger.info(
                        "Regression test written to %s/%s", worktree_path, test_path
                    )
                    return test_path, test_content

            # Model replied with plain text — continue loop
            if message.get("content"):
                messages.append(
                    {
                        "role": "user",
                        "content": "Please call emit_test with the test file path and content.",
                    }
                )

        # Fallback: return a minimal placeholder test that always fails
        fallback_content = (
            f'"""Fallback regression test — LLM did not emit a test."""\n\n'
            f"def test_regression_{hypothesis_id[:8]}():\n"
            f'    """Placeholder: regression test generation failed."""\n'
            f"    assert False, 'Regression test not generated'\n"
        )
        _write_test(
            root=worktree_path,
            file_path=default_path,
            content=fallback_content,
            overwrite=True,
        )
        logger.warning(
            "RegressionTestGenerator: fallback test written for hypothesis %s", hypothesis_id
        )
        return default_path, fallback_content

    # ── private helpers ───────────────────────────────────────────────────────

    def _build_prompt(
        self,
        hypothesis_id: str,
        bug_context: BugContext,
        candidate_fix: str,
        reproduction_plan: list[str],
        file_contents: dict[str, str],
    ) -> str:
        """
        Build the user-facing LLM prompt.
        NOTE: No API keys or credentials appear here (AGENTS.md).
        """
        lines = [
            f"Hypothesis ID: {hypothesis_id[:8]}",
            f"Issue: {bug_context.issue_id}",
            f"Symptom: {bug_context.symptom}",
            f"Expected behaviour: {bug_context.expected}",
            f"Actual behaviour: {bug_context.actual}",
            f"Affected area: {bug_context.affected_area}",
            "",
            "Reproduction steps:",
        ]
        for step in reproduction_plan:
            lines.append(f"  - {step}")

        lines += [
            "",
            "Candidate fix (DO NOT import — for context only):",
            candidate_fix,
            "",
        ]

        if file_contents:
            lines.append("Relevant source files:")
            for path, content in file_contents.items():
                # Truncate very large files
                truncated = content[:3000] + ("...[truncated]" if len(content) > 3000 else "")
                lines += [f"\n--- {path} ---\n{truncated}"]

        lines += [
            "",
            "Write a pytest regression test that FAILS on the original code and "
            "PASSES after the candidate fix is applied. "
            f"Use test file path: tests/test_regression_{hypothesis_id[:8]}.py",
            "Call emit_test when ready.",
        ]
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
        logger.debug(
            "RegressionTestGenerator POST %s/chat/completions", settings.llm_base_url
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.llm_base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
