from pathlib import Path

from app.tools.errors import PathEscapeError
from app.tools.executor import CommandResult, run_command


def _assert_within(root: Path, target: Path) -> None:
    """Raise PathEscapeError if `target` is not under `root`."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError as err:
        raise PathEscapeError(f"Path '{target}' is outside the allowed root '{root}'") from err


async def search_code(
    root: Path,
    pattern: str,
    file_glob: str = "*.py",
    max_results: int = 100,
) -> CommandResult:
    """
    Search for a regex pattern in files under `root` using `git grep`.

    git grep is fast and respects .gitignore. Falls back gracefully when
    called outside a git repo (exit code 128).

    Args:
        root:        directory to search within (worktree or repo root)
        pattern:     regex pattern
        file_glob:   glob to restrict files (default "*.py")
        max_results: truncate output to this many lines

    Returns:
        CommandResult — exit_code 1 means no matches (not an error)
    """
    result = await run_command(
        ["git", "grep", "-rn", "--", pattern, f"*{file_glob}"],
        cwd=root,
    )
    if result.stdout:
        lines = result.stdout.splitlines()
        if len(lines) > max_results:
            truncated = lines[:max_results]
            truncated.append(f"... [{len(lines) - max_results} more results truncated]")
            result = result.model_copy(update={"stdout": "\n".join(truncated)})
    return result


def read_file(
    root: Path,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, object]:
    """
    Read a file within `root` and return its content with metadata.

    Args:
        root:       allowed root directory (safety boundary)
        file_path:  path relative to root
        start_line: optional 1-based start line (inclusive)
        end_line:   optional 1-based end line (inclusive)

    Returns:
        {"path": str, "content": str, "total_lines": int,
         "start_line": int | None, "end_line": int | None}

    Raises:
        PathEscapeError:  if file_path resolves outside root
        FileNotFoundError: if the file does not exist
    """
    target = (root / file_path).resolve()
    _assert_within(root, target)

    if not target.exists():
        raise FileNotFoundError(f"File not found: {target}")

    raw = target.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines(keepends=True)
    total = len(lines)

    if start_line is not None or end_line is not None:
        s = (start_line or 1) - 1
        e = end_line or total
        content = "".join(lines[s:e])
    else:
        content = raw

    return {
        "path": str(target.relative_to(root.resolve())),
        "content": content,
        "total_lines": total,
        "start_line": start_line,
        "end_line": end_line,
    }


def write_test(
    root: Path,
    file_path: str,
    content: str,
    overwrite: bool = False,
) -> dict[str, object]:
    """
    Write a test file to `file_path` (relative to `root`).

    Args:
        root:       allowed root directory (worktree root)
        file_path:  relative path for the new/updated test file
        content:    full file content to write
        overwrite:  if False (default), raises FileExistsError if file exists

    Returns:
        {"path": str, "bytes_written": int}

    Raises:
        PathEscapeError:  if file_path resolves outside root
        FileExistsError:  if file exists and overwrite=False
    """
    target = (root / file_path).resolve()
    _assert_within(root, target)

    if target.exists() and not overwrite:
        raise FileExistsError(f"File already exists: {target}. Use overwrite=True to replace it.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return {
        "path": str(target.relative_to(root.resolve())),
        "bytes_written": len(content.encode("utf-8")),
    }
