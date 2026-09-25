import os
import tempfile
from pathlib import Path

from app.tools.executor import CommandResult, run_command


async def git_log(
    repo_path: Path,
    path: str | None = None,
    n: int = 20,
) -> CommandResult:
    """
    Run `git log --oneline -n N [-- <path>]`.

    Args:
        repo_path: root of the git repository (or worktree)
        path:      optional file path to filter log by
        n:         number of commits to return (default 20)
    """
    cmd = ["git", "log", "--oneline", f"-n{n}"]
    if path:
        cmd += ["--", path]
    return await run_command(cmd, cwd=repo_path)


async def git_blame(
    repo_path: Path,
    file_path: str,
) -> CommandResult:
    """Run `git blame <file_path>`."""
    return await run_command(["git", "blame", file_path], cwd=repo_path)


async def git_diff(
    repo_path: Path,
    base: str,
    head: str = "HEAD",
    path: str | None = None,
) -> CommandResult:
    """
    Run `git diff <base>..<head> [-- <path>]`.

    Args:
        repo_path: root of the git repository
        base:      base ref (commit SHA, branch, tag)
        head:      head ref (default: HEAD)
        path:      optional file path filter
    """
    cmd = ["git", "diff", f"{base}..{head}"]
    if path:
        cmd += ["--", path]
    return await run_command(cmd, cwd=repo_path)


async def git_show(
    repo_path: Path,
    ref: str,
    path: str | None = None,
) -> CommandResult:
    """Run `git show <ref>[:<path>]` to inspect a commit or file at a ref."""
    target = f"{ref}:{path}" if path else ref
    return await run_command(["git", "show", target], cwd=repo_path)


async def apply_patch(
    repo_path: Path,
    diff: str,
    check_only: bool = False,
) -> CommandResult:
    """
    Apply a unified diff via `git apply`.

    Writes the diff to a temp file then calls `git apply [--check] <file>`.

    Args:
        repo_path:  root of the git repository (or worktree)
        diff:       unified diff string
        check_only: if True, runs `git apply --check` (dry run, no file changes)
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as f:
        f.write(diff)
        patch_file = f.name

    try:
        cmd = ["git", "apply"]
        if check_only:
            cmd.append("--check")
        cmd.append(patch_file)
        return await run_command(cmd, cwd=repo_path)
    finally:
        os.unlink(patch_file)
