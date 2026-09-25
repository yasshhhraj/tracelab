# CP-03 — Tool Layer (Safe Shell Execution)

> **Goal:** A controlled execution layer that allowlists commands, captures output, enforces timeouts, and provides structured tool functions for all agent-to-environment interactions.  
> **Exit state:** All tool unit tests pass; dangerous commands are blocked at the allowlist; `ruff` is clean; existing tests unchanged.  
> **Builds on:** CP-01 (venv, config), CP-02 (models — Experiment row structure mirrors CommandResult).

---

## 1. Why This Layer Exists

The AGENTS.md constraint is explicit:

> "All agent interactions with the repo must go through the tool layer — no raw subprocess with arbitrary shell strings."

Agents receive **callables**, not shell access. Every tool call:
1. Checks the executable against an allowlist.
2. Runs inside a specified working directory (worktree path, set by CP-04).
3. Is bounded by a configurable timeout (default 120s from `Settings`).
4. Returns a structured `CommandResult` that is persisted as an `Experiment` row (CP-06).

---

## 2. Files to Create

```
backend/
├── app/
│   └── tools/
│       ├── __init__.py
│       ├── errors.py          # CommandNotPermittedError, ToolError
│       ├── executor.py        # run_command — core async subprocess runner
│       ├── git_tools.py       # git_log, git_blame, git_diff, apply_patch
│       ├── code_tools.py      # search_code, read_file, write_test
│       └── test_runner.py     # run_test — detects pytest / npm / mvn / gradle
└── tests/
    ├── test_executor.py       # allowlist, timeout, exit code, stdout/stderr capture
    ├── test_git_tools.py      # git_log, git_blame, git_diff, apply_patch on fixture repo
    ├── test_code_tools.py     # search_code, read_file, write_test
    └── test_test_runner.py    # run_test runner detection + execution
```

---

## 3. File-by-File Specification

### 3.1 `backend/app/tools/errors.py`

```python
class ToolError(Exception):
    """Base class for all tool-layer errors."""


class CommandNotPermittedError(ToolError):
    """Raised when the executable is not in the allowlist."""

    def __init__(self, executable: str) -> None:
        super().__init__(
            f"Command '{executable}' is not in the allowed executable list. "
            "Agents may only run: git, pytest, npm, pnpm, mvn, gradle, python, python3"
        )
        self.executable = executable
```

---

### 3.2 `backend/app/tools/executor.py`

The core of the tool layer. Everything else calls `run_command`.

```python
import asyncio
import time
from pathlib import Path

from pydantic import BaseModel

from app.config import settings
from app.tools.errors import CommandNotPermittedError

# ── allowlist ─────────────────────────────────────────────────────────────────
# Source: AGENTS.md + PRD §35
ALLOWED_EXECUTABLES: frozenset[str] = frozenset({
    "git",
    "pytest",
    "npm",
    "pnpm",
    "mvn",
    "gradle",
    "python",
    "python3",
})


class CommandResult(BaseModel):
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


async def run_command(
    command: list[str],
    cwd: Path,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """
    Run an allowlisted command inside `cwd` with a hard timeout.

    Args:
        command:         argv list, e.g. ["git", "status"]
        cwd:             working directory — must exist
        timeout_seconds: overrides Settings.max_command_timeout_seconds
        env:             optional extra env vars (merged with os.environ)

    Raises:
        CommandNotPermittedError: if command[0] is not in ALLOWED_EXECUTABLES
        FileNotFoundError:        if cwd does not exist
    """
    if not command:
        raise ValueError("command must be a non-empty list")

    executable = command[0]
    if executable not in ALLOWED_EXECUTABLES:
        raise CommandNotPermittedError(executable)

    if not cwd.exists():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")

    limit = timeout_seconds if timeout_seconds is not None else settings.max_command_timeout_seconds
    timed_out = False
    start = time.monotonic()

    # Merge caller-supplied env vars on top of the current process environment
    import os
    merged_env = {**os.environ, **(env or {})}

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
        try:
            raw_stdout, raw_stderr = await asyncio.wait_for(
                proc.communicate(), timeout=float(limit)
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.communicate()  # drain pipes
            raw_stdout, raw_stderr = b"", b""
    except FileNotFoundError:
        # Executable not found on PATH — treat as a failed command, not a block
        elapsed = int((time.monotonic() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=127,
            stdout="",
            stderr=f"Executable not found: {executable}",
            duration_ms=elapsed,
            timed_out=False,
        )

    elapsed = int((time.monotonic() - start) * 1000)

    return CommandResult(
        command=command,
        exit_code=proc.returncode if not timed_out else -1,
        stdout=raw_stdout.decode("utf-8", errors="replace"),
        stderr=raw_stderr.decode("utf-8", errors="replace"),
        duration_ms=elapsed,
        timed_out=timed_out,
    )
```

**Key design decisions:**

| Decision | Reason |
|---|---|
| `frozenset` allowlist | Immutable, O(1) lookup, no accidental mutation |
| `timeout_seconds=None` default, falls back to `settings` | Tests can override without touching config |
| `proc.kill()` + drain on timeout | Prevents zombie processes |
| Executable not on PATH → `exit_code=127` (not a block) | Blocked = wrong executable name; missing PATH = env problem, agent should see the error |
| `env` merge on top of `os.environ` | Allows injecting test variables (e.g. `PYTHONPATH`) without replacing the full environment |
| **Credentials never passed via `env`** | Any secrets from `Settings` are injected by the orchestrator, never forwarded as raw strings to agent-visible env |

---

### 3.3 `backend/app/tools/git_tools.py`

All functions call `run_command` — no raw subprocess.

```python
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
    if path:
        target = f"{ref}:{path}"
    else:
        target = ref
    return await run_command(["git", "show", target], cwd=repo_path)


async def apply_patch(
    repo_path: Path,
    diff: str,
    check_only: bool = False,
) -> CommandResult:
    """
    Apply a unified diff via `git apply`.

    Writes the diff to a temp file then calls `git apply [--check] -`.

    Args:
        repo_path:  root of the git repository (or worktree)
        diff:       unified diff string
        check_only: if True, runs `git apply --check` (dry run, no file changes)
    """
    import tempfile

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
        import os
        os.unlink(patch_file)
```

---

### 3.4 `backend/app/tools/code_tools.py`

```python
from pathlib import Path

from app.tools.errors import ToolError
from app.tools.executor import CommandResult, run_command


class PathEscapeError(ToolError):
    """Raised when a tool path resolves outside the allowed root directory."""


def _assert_within(root: Path, target: Path) -> None:
    """Raise PathEscapeError if `target` is not under `root`."""
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        raise PathEscapeError(
            f"Path '{target}' is outside the allowed root '{root}'"
        )


async def search_code(
    root: Path,
    pattern: str,
    file_glob: str = "*.py",
    max_results: int = 100,
) -> CommandResult:
    """
    Search for a regex pattern in files under `root`.

    Uses `git grep` (preferred inside git repos — respects .gitignore)
    falling back to the tool layer's `grep` via `git grep -r`.

    Args:
        root:        directory to search within (worktree or repo root)
        pattern:     regex pattern
        file_glob:   glob to restrict files (default "*.py")
        max_results: truncate results to this many lines
    """
    # git grep is fast and .gitignore-aware; -n adds line numbers
    result = await run_command(
        ["git", "grep", "-rn", "--", pattern, f"*{file_glob}"],
        cwd=root,
    )
    # Truncate to max_results lines to prevent flooding agent context
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
        {
            "path": str,
            "content": str,
            "total_lines": int,
            "start_line": int | None,
            "end_line": int | None,
        }

    Raises:
        PathEscapeError: if file_path resolves outside root
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
        selected = lines[s:e]
        content = "".join(selected)
    else:
        content = raw

    return {
        "path": str(target.relative_to(root)),
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

    Safety: the resolved target must be inside `root`.

    Args:
        root:       allowed root directory (worktree root)
        file_path:  relative path for the new/updated test file
        content:    full file content to write
        overwrite:  if False (default), raises FileExistsError if the file exists

    Returns:
        {"path": str, "bytes_written": int}
    """
    target = (root / file_path).resolve()
    _assert_within(root, target)

    if target.exists() and not overwrite:
        raise FileExistsError(
            f"File already exists: {target}. Use overwrite=True to replace it."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    return {
        "path": str(target.relative_to(root)),
        "bytes_written": len(content.encode("utf-8")),
    }
```

---

### 3.5 `backend/app/tools/test_runner.py`

Detects the test framework from project files, then delegates to `run_command`.

```python
from pathlib import Path

from app.tools.executor import CommandResult, run_command

# Detection order: first match wins
_RUNNER_DETECTION: list[tuple[str, list[str]]] = [
    # file-that-must-exist  → command to run
    ("pytest.ini",         ["pytest", "--tb=short", "-q"]),
    ("pyproject.toml",     ["pytest", "--tb=short", "-q"]),
    ("setup.cfg",          ["pytest", "--tb=short", "-q"]),
    ("package.json",       ["npm", "test", "--", "--passWithNoTests"]),
    ("pnpm-lock.yaml",     ["pnpm", "test"]),
    ("pom.xml",            ["mvn", "test", "-q"]),
    ("build.gradle",       ["gradle", "test", "--quiet"]),
    ("build.gradle.kts",   ["gradle", "test", "--quiet"]),
]


def _detect_runner(repo_path: Path) -> list[str]:
    """Return the best-fit test command for the repository at `repo_path`."""
    for marker_file, cmd in _RUNNER_DETECTION:
        if (repo_path / marker_file).exists():
            return cmd
    # Default: pytest
    return ["pytest", "--tb=short", "-q"]


async def run_test(
    repo_path: Path,
    test_path: str | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> CommandResult:
    """
    Run tests in `repo_path`, auto-detecting the framework.

    Args:
        repo_path:       root of the project to test (worktree path)
        test_path:       optional specific test file / directory / node ID
        extra_args:      additional CLI args appended to the detected command
        timeout_seconds: overrides Settings.max_command_timeout_seconds

    Returns:
        CommandResult with stdout, stderr, exit_code, duration_ms
    """
    cmd = _detect_runner(repo_path)
    if test_path:
        cmd = cmd + [test_path]
    if extra_args:
        cmd = cmd + extra_args
    return await run_command(cmd, cwd=repo_path, timeout_seconds=timeout_seconds)
```

---

### 3.6 `backend/app/tools/__init__.py`

Re-exports the public API so agents import from one place:

```python
from app.tools.code_tools import read_file, search_code, write_test
from app.tools.errors import CommandNotPermittedError, PathEscapeError, ToolError
from app.tools.executor import ALLOWED_EXECUTABLES, CommandResult, run_command
from app.tools.git_tools import apply_patch, git_blame, git_diff, git_log, git_show
from app.tools.test_runner import run_test

__all__ = [
    "ALLOWED_EXECUTABLES",
    "CommandNotPermittedError",
    "CommandResult",
    "PathEscapeError",
    "ToolError",
    "apply_patch",
    "git_blame",
    "git_diff",
    "git_log",
    "git_show",
    "read_file",
    "run_command",
    "run_test",
    "search_code",
    "write_test",
]
```

---

## 4. Tests

### 4.1 `backend/tests/test_executor.py`

Tests the allowlist, timeout, exit code, stdout/stderr capture, and working directory enforcement.

```python
import pytest
from pathlib import Path
from app.tools.executor import CommandResult, run_command, ALLOWED_EXECUTABLES
from app.tools.errors import CommandNotPermittedError


# ── allowlist ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", [
    ["rm", "-rf", "/"],
    ["sudo", "whoami"],
    ["curl", "https://example.com"],
    ["bash", "-c", "echo hi"],
    ["sh", "-c", "echo hi"],
    ["sleep", "5"],
    ["/bin/rm", "-rf", "/tmp"],
])
async def test_blocked_commands_raise(cmd, tmp_path):
    with pytest.raises(CommandNotPermittedError) as exc_info:
        await run_command(cmd, cwd=tmp_path)
    assert exc_info.value.executable == cmd[0]


@pytest.mark.asyncio
async def test_allowlist_contains_required_executables():
    required = {"git", "pytest", "npm", "pnpm", "mvn", "gradle", "python", "python3"}
    assert required.issubset(ALLOWED_EXECUTABLES)


# ── execution ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_git_version_returns_ok(tmp_path):
    """git is on PATH in both dev and Docker (Dockerfile installs it)."""
    result = await run_command(["git", "version"], cwd=tmp_path)
    assert result.exit_code == 0
    assert "git" in result.stdout.lower()
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_stdout_and_stderr_captured(tmp_path):
    result = await run_command(["git", "status"], cwd=tmp_path)
    # Outside a git repo — git writes to stderr and exits non-zero
    assert isinstance(result.stdout, str)
    assert isinstance(result.stderr, str)
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_exit_code_captured(tmp_path):
    result = await run_command(["git", "version"], cwd=tmp_path)
    assert result.exit_code == 0
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_duration_ms_is_positive(tmp_path):
    result = await run_command(["git", "version"], cwd=tmp_path)
    assert result.duration_ms > 0


@pytest.mark.asyncio
async def test_nonexistent_cwd_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await run_command(["git", "version"], cwd=tmp_path / "does_not_exist")


@pytest.mark.asyncio
async def test_empty_command_raises(tmp_path):
    with pytest.raises(ValueError):
        await run_command([], cwd=tmp_path)


@pytest.mark.asyncio
async def test_timeout_fires(tmp_path):
    """python3 -c 'import time; time.sleep(10)' should time out at 1s."""
    # init a git repo so python3 cwd is valid
    await run_command(["git", "init"], cwd=tmp_path)
    result = await run_command(
        ["python3", "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout_seconds=1,
    )
    assert result.timed_out is True
    assert result.exit_code == -1


@pytest.mark.asyncio
async def test_command_result_is_pydantic_model(tmp_path):
    result = await run_command(["git", "version"], cwd=tmp_path)
    assert isinstance(result, CommandResult)
    assert isinstance(result.command, list)
```

---

### 4.2 `backend/tests/test_git_tools.py`

Uses a real fixture git repo created in `tmp_path`.

```python
import pytest
from pathlib import Path
from app.tools.executor import run_command
from app.tools.git_tools import apply_patch, git_blame, git_diff, git_log, git_show


@pytest.fixture
async def git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()

    # Configure git identity (required in CI environments)
    await run_command(["git", "init"], cwd=repo)
    await run_command(["git", "config", "user.email", "test@tracelab.ai"], cwd=repo)
    await run_command(["git", "config", "user.name", "TraceLab Test"], cwd=repo)

    # First commit
    (repo / "service.py").write_text("def hello():\n    return 'hello'\n")
    await run_command(["git", "add", "."], cwd=repo)
    await run_command(["git", "commit", "-m", "initial commit"], cwd=repo)

    # Second commit — modify the file
    (repo / "service.py").write_text("def hello():\n    return 'hello world'\n")
    await run_command(["git", "add", "."], cwd=repo)
    await run_command(["git", "commit", "-m", "update greeting"], cwd=repo)

    return repo


@pytest.mark.asyncio
async def test_git_log_returns_commits(git_repo):
    result = await git_log(git_repo, n=10)
    assert result.exit_code == 0
    assert "initial commit" in result.stdout
    assert "update greeting" in result.stdout


@pytest.mark.asyncio
async def test_git_log_filters_by_path(git_repo):
    result = await git_log(git_repo, path="service.py", n=10)
    assert result.exit_code == 0
    assert "initial commit" in result.stdout


@pytest.mark.asyncio
async def test_git_blame_returns_output(git_repo):
    result = await git_blame(git_repo, "service.py")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_git_diff_between_commits(git_repo):
    # Get the two commit SHAs
    log_result = await run_command(
        ["git", "log", "--format=%H", "--reverse"], cwd=git_repo
    )
    shas = log_result.stdout.strip().splitlines()
    first_sha, second_sha = shas[0], shas[1]

    result = await git_diff(git_repo, base=first_sha, head=second_sha)
    assert result.exit_code == 0
    assert "hello world" in result.stdout


@pytest.mark.asyncio
async def test_git_diff_filters_by_path(git_repo):
    log_result = await run_command(
        ["git", "log", "--format=%H", "--reverse"], cwd=git_repo
    )
    shas = log_result.stdout.strip().splitlines()
    result = await git_diff(git_repo, base=shas[0], head=shas[1], path="service.py")
    assert result.exit_code == 0
    assert "service.py" in result.stdout


@pytest.mark.asyncio
async def test_apply_patch_check_only(git_repo):
    """apply_patch --check verifies a valid patch without modifying files."""
    valid_diff = (
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hello world'\n"
        "+    return 'hello universe'\n"
    )
    result = await apply_patch(git_repo, diff=valid_diff, check_only=True)
    assert result.exit_code == 0  # --check passes on valid patch


@pytest.mark.asyncio
async def test_apply_patch_modifies_file(git_repo):
    valid_diff = (
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def hello():\n"
        "-    return 'hello world'\n"
        "+    return 'hello universe'\n"
    )
    result = await apply_patch(git_repo, diff=valid_diff)
    assert result.exit_code == 0
    assert "hello universe" in (git_repo / "service.py").read_text()
```

---

### 4.3 `backend/tests/test_code_tools.py`

```python
import pytest
from pathlib import Path
from app.tools.code_tools import PathEscapeError, read_file, search_code, write_test


@pytest.fixture
async def code_root(tmp_path: Path) -> Path:
    """Create a minimal Python project tree inside a git repo."""
    root = tmp_path / "project"
    root.mkdir()
    from app.tools.executor import run_command
    await run_command(["git", "init"], cwd=root)
    await run_command(["git", "config", "user.email", "test@tracelab.ai"], cwd=root)
    await run_command(["git", "config", "user.name", "TraceLab Test"], cwd=root)
    (root / "service.py").write_text(
        "class ReviewService:\n    def create(self, data):\n        pass\n"
    )
    (root / "repository.py").write_text(
        "class ReviewRepository:\n    def save(self, run):\n        pass\n"
    )
    from app.tools.executor import run_command
    await run_command(["git", "add", "."], cwd=root)
    await run_command(["git", "commit", "-m", "init"], cwd=root)
    return root


@pytest.mark.asyncio
async def test_search_code_finds_pattern(code_root):
    result = await search_code(code_root, pattern="ReviewService")
    assert result.exit_code == 0
    assert "service.py" in result.stdout


@pytest.mark.asyncio
async def test_search_code_no_match_exits_nonzero(code_root):
    result = await search_code(code_root, pattern="NonExistentClass12345")
    # grep / git grep exits 1 when no match found
    assert result.exit_code == 1
    assert result.stdout == "" or result.stdout.strip() == ""


@pytest.mark.asyncio
async def test_read_file_returns_content(code_root):
    info = read_file(code_root, "service.py")
    assert "ReviewService" in info["content"]
    assert info["total_lines"] == 3
    assert info["path"] == "service.py"


@pytest.mark.asyncio
async def test_read_file_line_range(code_root):
    info = read_file(code_root, "service.py", start_line=1, end_line=1)
    assert "ReviewService" in info["content"]
    assert "def create" not in info["content"]


@pytest.mark.asyncio
async def test_read_file_path_escape_blocked(code_root):
    with pytest.raises(PathEscapeError):
        read_file(code_root, "../../../etc/passwd")


@pytest.mark.asyncio
async def test_read_file_missing_raises(code_root):
    with pytest.raises(FileNotFoundError):
        read_file(code_root, "nonexistent.py")


@pytest.mark.asyncio
async def test_write_test_creates_file(code_root):
    result = write_test(
        code_root,
        "tests/test_service.py",
        "def test_placeholder():\n    assert True\n",
    )
    assert result["bytes_written"] > 0
    assert (code_root / "tests" / "test_service.py").exists()


@pytest.mark.asyncio
async def test_write_test_no_overwrite_by_default(code_root):
    write_test(code_root, "tests/test_x.py", "# first\n")
    with pytest.raises(FileExistsError):
        write_test(code_root, "tests/test_x.py", "# second\n")


@pytest.mark.asyncio
async def test_write_test_overwrite_flag(code_root):
    write_test(code_root, "tests/test_y.py", "# v1\n")
    write_test(code_root, "tests/test_y.py", "# v2\n", overwrite=True)
    assert "v2" in (code_root / "tests" / "test_y.py").read_text()


@pytest.mark.asyncio
async def test_write_test_path_escape_blocked(code_root):
    with pytest.raises(PathEscapeError):
        write_test(code_root, "../../evil.py", "# evil\n")
```

---

### 4.4 `backend/tests/test_test_runner.py`

```python
import pytest
from pathlib import Path
from app.tools.test_runner import _detect_runner, run_test


@pytest.fixture
def pytest_project(tmp_path: Path) -> Path:
    """Minimal pytest project with a passing test."""
    root = tmp_path / "pytest_project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_passing.py").write_text(
        "def test_always_passes():\n    assert True\n"
    )
    return root


@pytest.fixture
def npm_project(tmp_path: Path) -> Path:
    """Minimal project with package.json to trigger npm detection."""
    root = tmp_path / "npm_project"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"test": "echo ok"}}\n')
    return root


def test_detect_runner_pytest(pytest_project):
    cmd = _detect_runner(pytest_project)
    assert cmd[0] == "pytest"


def test_detect_runner_npm(npm_project):
    cmd = _detect_runner(npm_project)
    assert cmd[0] == "npm"


def test_detect_runner_default(tmp_path):
    """No marker file → falls back to pytest."""
    cmd = _detect_runner(tmp_path)
    assert cmd[0] == "pytest"


@pytest.mark.asyncio
async def test_run_test_runs_pytest(pytest_project):
    result = await run_test(pytest_project)
    assert result.exit_code == 0
    assert "passed" in result.stdout or "passed" in result.stderr


@pytest.mark.asyncio
async def test_run_test_with_explicit_path(pytest_project):
    result = await run_test(pytest_project, test_path="tests/test_passing.py")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_run_test_returns_command_result(pytest_project):
    from app.tools.executor import CommandResult
    result = await run_test(pytest_project)
    assert isinstance(result, CommandResult)
```

---

## 5. Implementation Order

| Step | Action | Verify |
|------|--------|--------|
| 1 | Create `app/tools/__init__.py`, `errors.py` | `python -c "from app.tools.errors import CommandNotPermittedError"` |
| 2 | Create `app/tools/executor.py` | `python -c "from app.tools.executor import run_command"` |
| 3 | Create `app/tools/git_tools.py` | `python -c "from app.tools.git_tools import git_log"` |
| 4 | Create `app/tools/code_tools.py` | `python -c "from app.tools.code_tools import search_code"` |
| 5 | Create `app/tools/test_runner.py` | `python -c "from app.tools.test_runner import run_test"` |
| 6 | Update `app/tools/__init__.py` with full re-exports | — |
| 7 | Create `tests/test_executor.py` | — |
| 8 | Create `tests/test_git_tools.py` | — |
| 9 | Create `tests/test_code_tools.py` | — |
| 10 | Create `tests/test_test_runner.py` | — |
| 11 | `pytest tests/test_executor.py` | All pass |
| 12 | `pytest tests/test_git_tools.py` | All pass |
| 13 | `pytest tests/test_code_tools.py` | All pass |
| 14 | `pytest tests/test_test_runner.py` | All pass |
| 15 | `pytest` (full suite) | All prior tests still pass |
| 16 | `ruff check . && ruff format --check .` | Clean |

---

## 6. Security Properties

| Property | How enforced |
|---|---|
| No arbitrary shell access | `run_command` allowlist check before `create_subprocess_exec` |
| No shell string injection | `create_subprocess_exec` used (not `create_subprocess_shell`) — args are a list, never a string |
| No path traversal in file ops | `_assert_within(root, target)` in `read_file` and `write_test` |
| Credentials never in env | `Settings` secrets (`llm_api_key`, `jira_api_token`, `github_token`) are never passed to `run_command`'s `env` parameter |
| No zombie processes | `proc.kill()` + drain on timeout |
| Output size bounded | `search_code` truncates to `max_results` lines |

---

## 7. Acceptance Criteria (Definition of Done)

```
[ ] run_command(["rm", "-rf", "/"], ...) → CommandNotPermittedError
[ ] run_command(["sudo", "whoami"], ...) → CommandNotPermittedError
[ ] run_command(["git", "version"], cwd=tmp_path) → exit_code=0
[ ] timeout fires within ±500ms of the configured limit
[ ] git_log, git_blame, git_diff, apply_patch all tested against a real fixture repo
[ ] read_file raises PathEscapeError for ../../../etc/passwd
[ ] write_test raises PathEscapeError for ../../evil.py
[ ] run_test auto-detects pytest and passes the test suite
[ ] pytest (full suite) → all tests pass, 0 failures
[ ] ruff check . && ruff format --check . → clean
[ ] No subprocess.shell=True anywhere in app/tools/
[ ] No settings.llm_api_key / jira_api_token / github_token passed to run_command
```

---

## 8. What CP-03 Deliberately Does NOT Include

| Excluded | Added in |
|----------|----------|
| Git worktree creation/destruction | CP-04 |
| Agent prompt / LLM calls | CP-05 |
| Tool calls triggered by an agent | CP-05 |
| Persisting `Experiment` rows to DB | CP-06 (orchestrator wires this) |
| Flaky-test repeated runner | CP-07 (verification engine) |
| Rate limiting / token counting | Post-MVP |
