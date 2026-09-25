import asyncio
import os
import time
from pathlib import Path

from pydantic import BaseModel

from app.config import settings
from app.tools.errors import CommandNotPermittedError

# ── allowlist ─────────────────────────────────────────────────────────────────
# Source: AGENTS.md + PRD §35
ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "git",
        "pytest",
        "npm",
        "pnpm",
        "mvn",
        "gradle",
        "python",
        "python3",
    }
)


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
        env:             optional extra env vars merged with os.environ

    Raises:
        CommandNotPermittedError: if command[0] is not in ALLOWED_EXECUTABLES
        ValueError:               if command is empty
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

    # Merge caller-supplied env vars on top of the current process environment.
    # Credentials from Settings are NEVER forwarded here.
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
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.communicate()  # drain pipes to prevent deadlock
            raw_stdout, raw_stderr = b"", b""
    except FileNotFoundError:
        # Executable found in allowlist but not on PATH — report as failed command
        elapsed = int((time.monotonic() - start) * 1000)
        return CommandResult(
            command=command,
            exit_code=127,
            stdout="",
            stderr=f"Executable not found on PATH: {executable}",
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
