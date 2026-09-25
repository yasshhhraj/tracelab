import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.base import AgentContext, AgentModel, AgentResult
from app.agents.llm.openai_model import OpenAIAgentModel
from app.tools.code_tools import read_file as _read_file
from app.tools.code_tools import search_code as _search_code
from app.tools.git_tools import git_diff as _git_diff

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

# ── tool wrappers ─────────────────────────────────────────────────────────────
# Each wrapper bridges the LLM-facing parameter names to the actual tool
# signatures in app/tools/. The __tool_schema__ attribute provides the
# OpenAI function definition; __name__ provides the dispatch key.


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


def _wrap_git_diff() -> Callable:
    async def git_diff(repo_path: str, base: str, head: str = "HEAD", path: str = "") -> str:
        """Get the git diff between two refs, optionally filtered to a file path."""
        result = await _git_diff(
            repo_path=Path(repo_path),
            base=base,
            head=head,
            path=path or None,
        )
        return result.stdout or "(empty diff)"

    git_diff.__name__ = "git_diff"
    git_diff.__tool_schema__: dict[str, Any] = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Get the unified diff between two git refs.",
            "parameters": {
                "type": "object",
                "required": ["repo_path", "base", "head"],
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the git repository.",
                    },
                    "base": {
                        "type": "string",
                        "description": "Base ref (commit SHA or branch name).",
                    },
                    "head": {
                        "type": "string",
                        "description": "Head ref (commit SHA or branch name). Defaults to HEAD.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional file path to restrict the diff.",
                    },
                },
            },
        },
    }
    return git_diff


# ── factory ───────────────────────────────────────────────────────────────────


def make_code_path_agent() -> tuple[AgentModel, list[Callable]]:
    """
    Instantiate a CodePathAgent backed by OpenAIAgentModel.

    Returns:
        (agent, tools) — pass both to ``agent.run(context, tools)``
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
