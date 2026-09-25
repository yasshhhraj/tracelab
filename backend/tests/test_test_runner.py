from pathlib import Path

import pytest

from app.tools.executor import CommandResult
from app.tools.test_runner import _detect_runner, run_test


@pytest.fixture
def pytest_project(tmp_path: Path) -> Path:
    """Minimal pytest project with a passing test."""
    root = tmp_path / "pytest_project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_passing.py").write_text("def test_always_passes():\n    assert True\n")
    return root


@pytest.fixture
def npm_project(tmp_path: Path) -> Path:
    """Minimal project with package.json to trigger npm detection."""
    root = tmp_path / "npm_project"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"test": "echo ok"}}\n')
    return root


@pytest.fixture
def pnpm_project(tmp_path: Path) -> Path:
    root = tmp_path / "pnpm_project"
    root.mkdir()
    (root / "pnpm-lock.yaml").write_text("")
    (root / "package.json").write_text('{"scripts": {"test": "echo ok"}}\n')
    return root


# ── detection ─────────────────────────────────────────────────────────────────


def test_detect_runner_pytest_via_pyproject(pytest_project):
    cmd = _detect_runner(pytest_project)
    assert cmd[0] == "pytest"


def test_detect_runner_npm(npm_project):
    cmd = _detect_runner(npm_project)
    assert cmd[0] == "npm"


def test_detect_runner_pnpm_takes_priority_over_npm(pnpm_project):
    """pnpm-lock.yaml is checked before package.json."""
    cmd = _detect_runner(pnpm_project)
    assert cmd[0] == "pnpm"


def test_detect_runner_default_is_pytest(tmp_path):
    """No marker file → falls back to pytest."""
    cmd = _detect_runner(tmp_path)
    assert cmd[0] == "pytest"


def test_detect_runner_returns_copy(pytest_project):
    """Mutations to the returned list must not affect the detection table."""
    cmd1 = _detect_runner(pytest_project)
    cmd1.append("--extra-flag")
    cmd2 = _detect_runner(pytest_project)
    assert "--extra-flag" not in cmd2


# ── execution ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_test_runs_pytest(pytest_project):
    result = await run_test(pytest_project)
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "passed" in combined


@pytest.mark.asyncio
async def test_run_test_with_explicit_path(pytest_project):
    result = await run_test(pytest_project, test_path="tests/test_passing.py")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_run_test_returns_command_result(pytest_project):
    result = await run_test(pytest_project)
    assert isinstance(result, CommandResult)


@pytest.mark.asyncio
async def test_run_test_extra_args_passed(pytest_project):
    result = await run_test(pytest_project, extra_args=["-v"])
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "passed" in combined.lower()
