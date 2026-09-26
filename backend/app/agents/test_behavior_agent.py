import json
from collections.abc import Callable
from pathlib import Path

from app.agents.base import AgentContext, AgentModel, AgentResult
from app.agents.llm.openai_model import OpenAIAgentModel
from app.tools.code_tools import read_file as _read_file
from app.tools.code_tools import search_code as _search_code
from app.tools.test_runner import run_test as _run_test

# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a test-behavior investigation agent for TraceLab, an AI debugging platform.

Your job:
1. Analyse the bug report provided by the user.
2. Use the available tools (run_test, search_code, read_file) to investigate the
   TEST BEHAVIOR around the affected area — what tests exist, which are failing,
   and what is missing.
3. Identify the specific test assertions that expose the bug, or the gap in
   coverage that allowed the bug to go undetected.
4. Formulate a concrete candidate fix, including what regression test would
   prove the fix is correct.
5. When you have enough evidence, call emit_hypothesis with your findings.

Guidelines:
- Focus on TEST BEHAVIOR — find existing tests, run them, observe what fails.
- Use search_code to locate test files and relevant test functions.
- Use run_test to execute specific tests and capture pass/fail output.
- Use read_file to understand the test assertions and the code under test.
- Do NOT speculate about causes you cannot verify with tool output.
- suspected_files must include both the source file AND the relevant test file.
- reproduction_plan must include exact test commands a developer can run.
- candidate_fix must describe both the source fix and the regression test to add.
- confidence is INFORMATIONAL only — report it honestly but it does NOT affect
  whether your hypothesis is selected.
"""

# ── tool wrappers ─────────────────────────────────────────────────────────────


def _wrap_run_test() -> Callable:
    async def run_test(repo_path: str, test_path: str = "") -> str:
        """Run the test suite (or a specific test path) and return the output."""
        result = await _run_test(
            repo_path=Path(repo_path),
            test_path=test_path or None,
        )
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(result.stderr)
        combined = "\n".join(output_parts) or "(no output)"
        exit_label = "PASS" if result.exit_code == 0 else f"FAIL (exit {result.exit_code})"
        return f"{exit_label}\n{combined}"

    run_test.__name__ = "run_test"
    run_test.__tool_schema__ = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "run_test",
            "description": (
                "Run the test suite (auto-detects pytest/npm/mvn). "
                "Optionally restrict to a specific test file or test node ID."
            ),
            "parameters": {
                "type": "object",
                "required": ["repo_path"],
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the project root (worktree).",
                    },
                    "test_path": {
                        "type": "string",
                        "description": (
                            "Optional test file path or pytest node ID "
                            "(e.g. 'tests/test_service.py::test_idempotency')."
                        ),
                    },
                },
            },
        },
    }
    return run_test


def _wrap_search_code() -> Callable:
    async def search_code(pattern: str, path: str) -> str:
        """Search for a regex pattern in the codebase under `path`."""
        result = await _search_code(root=Path(path), pattern=pattern)
        return result.stdout or "(no matches)"

    search_code.__name__ = "search_code"
    search_code.__tool_schema__ = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a regex pattern in source files under the given path.",
            "parameters": {
                "type": "object",
                "required": ["pattern", "path"],
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path to search (worktree or repo root).",
                    },
                },
            },
        },
    }
    return search_code


def _wrap_read_file() -> Callable:
    async def read_file(root: str, file_path: str) -> str:
        """Read the contents of a file relative to `root`."""
        result = _read_file(root=Path(root), file_path=file_path)
        return json.dumps(result)

    read_file.__name__ = "read_file"
    read_file.__tool_schema__ = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a source file.",
            "parameters": {
                "type": "object",
                "required": ["root", "file_path"],
                "properties": {
                    "root": {
                        "type": "string",
                        "description": "Absolute path to the repo or worktree root.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to root.",
                    },
                },
            },
        },
    }
    return read_file


# ── factory ───────────────────────────────────────────────────────────────────


def make_test_behavior_agent() -> tuple[AgentModel, list[Callable]]:
    """
    Instantiate a TestBehaviorAgent backed by OpenAIAgentModel.

    Returns:
        (agent, tools) — pass both to ``agent.run(context, tools)``
    """
    model = OpenAIAgentModel(agent_type="test_behavior", system_prompt=_SYSTEM_PROMPT)
    tools: list[Callable] = [
        _wrap_run_test(),
        _wrap_search_code(),
        _wrap_read_file(),
    ]
    return model, tools


async def run_test_behavior_agent(context: AgentContext) -> AgentResult:
    """Convenience function: create agent + tools, run, return result."""
    agent, tools = make_test_behavior_agent()
    return await agent.run(context, tools)
