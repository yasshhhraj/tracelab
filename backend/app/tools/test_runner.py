from pathlib import Path

from app.tools.executor import CommandResult, run_command

# Detection order: first matching marker file wins.
# Each entry: (marker_filename, base_command_list)
_RUNNER_DETECTION: list[tuple[str, list[str]]] = [
    ("pytest.ini", ["pytest", "--tb=short", "-q"]),
    ("pyproject.toml", ["pytest", "--tb=short", "-q"]),
    ("setup.cfg", ["pytest", "--tb=short", "-q"]),
    ("pnpm-lock.yaml", ["pnpm", "test"]),
    ("package.json", ["npm", "test", "--", "--passWithNoTests"]),
    ("pom.xml", ["mvn", "test", "-q"]),
    ("build.gradle.kts", ["gradle", "test", "--quiet"]),
    ("build.gradle", ["gradle", "test", "--quiet"]),
]


def _detect_runner(repo_path: Path) -> list[str]:
    """Return the best-fit test command for the repository at `repo_path`."""
    for marker_file, cmd in _RUNNER_DETECTION:
        if (repo_path / marker_file).exists():
            return list(cmd)
    # Default: pytest
    return ["pytest", "--tb=short", "-q"]


async def run_test(
    repo_path: Path,
    test_path: str | None = None,
    extra_args: list[str] | None = None,
    timeout_seconds: int | None = None,
) -> CommandResult:
    """
    Run tests in `repo_path`, auto-detecting the test framework.

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
