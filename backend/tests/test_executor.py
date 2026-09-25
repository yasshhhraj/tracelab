import pytest

from app.tools.errors import CommandNotPermittedError
from app.tools.executor import ALLOWED_EXECUTABLES, CommandResult, run_command

# ── allowlist ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cmd",
    [
        ["rm", "-rf", "/"],
        ["sudo", "whoami"],
        ["curl", "https://example.com"],
        ["bash", "-c", "echo hi"],
        ["sh", "-c", "echo hi"],
        ["sleep", "5"],
        ["/bin/rm", "-rf", "/tmp"],
        ["cat", "/etc/passwd"],
    ],
)
async def test_blocked_commands_raise(cmd, tmp_path):
    with pytest.raises(CommandNotPermittedError) as exc_info:
        await run_command(cmd, cwd=tmp_path)
    assert exc_info.value.executable == cmd[0]


@pytest.mark.asyncio
async def test_allowlist_contains_required_executables():
    required = {"git", "pytest", "npm", "pnpm", "mvn", "gradle", "python", "python3"}
    assert required.issubset(ALLOWED_EXECUTABLES)


@pytest.mark.asyncio
async def test_allowlist_is_frozenset():
    assert isinstance(ALLOWED_EXECUTABLES, frozenset)


# ── execution ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_git_version_returns_ok(tmp_path):
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


@pytest.mark.asyncio
async def test_duration_ms_is_positive(tmp_path):
    result = await run_command(["git", "version"], cwd=tmp_path)
    assert result.duration_ms > 0


@pytest.mark.asyncio
async def test_command_stored_on_result(tmp_path):
    cmd = ["git", "version"]
    result = await run_command(cmd, cwd=tmp_path)
    assert result.command == cmd


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
    """python3 sleeping 10s should time out after 1s."""
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
    assert isinstance(result.stdout, str)
    assert isinstance(result.timed_out, bool)
