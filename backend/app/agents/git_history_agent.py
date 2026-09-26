import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.agents.base import AgentContext, AgentModel, AgentResult
from app.agents.llm.openai_model import OpenAIAgentModel
from app.tools.code_tools import read_file as _read_file
from app.tools.git_tools import git_blame as _git_blame
from app.tools.git_tools import git_diff as _git_diff
from app.tools.git_tools import git_log as _git_log

# ── system prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a git-history investigation agent for TraceLab, an AI debugging platform.

Your job:
1. Analyse the bug report provided by the user.
2. Use the available tools (git_log, git_blame, git_diff, read_file) to trace
   WHEN and WHERE the bug was introduced in the repository's commit history.
3. Identify the specific commit, author, and code change most likely responsible.
4. Formulate a concrete candidate fix based on reverting or correcting that change.
5. When you have enough evidence, call emit_hypothesis with your findings.

Guidelines:
- Focus on COMMIT HISTORY — when was the affected code last changed, and what changed.
- Use git_log to find recent commits on files related to the affected area.
- Use git_blame to pinpoint the exact commit that introduced each suspect line.
- Use git_diff to understand the full scope of a suspect commit.
- Use read_file to understand the surrounding context of blamed lines.
- Do NOT speculate about causes you cannot verify with tool output.
- suspected_files must list files you actually inspected, not guesses.
- candidate_fix must reference the specific commit/change to revert or correct.
- confidence is INFORMATIONAL only — report it honestly but it does NOT affect
  whether your hypothesis is selected.
"""

# ── tool wrappers ─────────────────────────────────────────────────────────────


def _wrap_git_log() -> Callable:
    async def git_log(repo_path: str, path: str = "", n: int = 20) -> str:
        """List recent commits, optionally filtered to a file path."""
        result = await _git_log(
            repo_path=Path(repo_path),
            path=path or None,
            n=n,
        )
        return result.stdout or "(no commits found)"

    git_log.__name__ = "git_log"
    git_log.__tool_schema__ = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "List recent git commits, optionally filtered to a specific file path.",
            "parameters": {
                "type": "object",
                "required": ["repo_path"],
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the git repository.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional file path to filter commits.",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of commits to return (default 20).",
                    },
                },
            },
        },
    }
    return git_log


def _wrap_git_blame() -> Callable:
    async def git_blame(repo_path: str, file_path: str) -> str:
        """Show per-line blame annotations for a file."""
        result = await _git_blame(
            repo_path=Path(repo_path),
            file_path=file_path,
        )
        return result.stdout or "(no blame output)"

    git_blame.__name__ = "git_blame"
    git_blame.__tool_schema__ = {  # type: ignore[attr-defined]
        "type": "function",
        "function": {
            "name": "git_blame",
            "description": "Show per-line blame annotations (commit SHA, author, date) for a file.",
            "parameters": {
                "type": "object",
                "required": ["repo_path", "file_path"],
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Absolute path to the git repository.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to the repository root.",
                    },
                },
            },
        },
    }
    return git_blame


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
                "required": ["repo_path", "base"],
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


def make_git_history_agent() -> tuple[AgentModel, list[Callable]]:
    """
    Instantiate a GitHistoryAgent backed by OpenAIAgentModel.

    Returns:
        (agent, tools) — pass both to ``agent.run(context, tools)``
    """
    model = OpenAIAgentModel(agent_type="git_history", system_prompt=_SYSTEM_PROMPT)
    tools: list[Callable] = [
        _wrap_git_log(),
        _wrap_git_blame(),
        _wrap_git_diff(),
        _wrap_read_file(),
    ]
    return model, tools


async def run_git_history_agent(context: AgentContext) -> AgentResult:
    """Convenience function: create agent + tools, run, return result."""
    agent, tools = make_git_history_agent()
    return await agent.run(context, tools)
