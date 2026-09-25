from pathlib import Path

import pytest
import pytest_asyncio

from app.tools.code_tools import PathEscapeError, read_file, search_code, write_test
from app.tools.executor import run_command


@pytest_asyncio.fixture
async def code_root(tmp_path: Path) -> Path:
    """Create a minimal Python project tree inside a git repo."""
    root = tmp_path / "project"
    root.mkdir()

    await run_command(["git", "init"], cwd=root)
    await run_command(["git", "config", "user.email", "test@tracelab.ai"], cwd=root)
    await run_command(["git", "config", "user.name", "TraceLab Test"], cwd=root)

    (root / "service.py").write_text(
        "class ReviewService:\n    def create(self, data):\n        pass\n"
    )
    (root / "repository.py").write_text(
        "class ReviewRepository:\n    def save(self, run):\n        pass\n"
    )

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
    # git grep exits 1 when no match found
    assert result.exit_code == 1
    assert result.stdout.strip() == ""


@pytest.mark.asyncio
async def test_search_code_truncates_results(code_root):
    # Write many lines matching the pattern
    (code_root / "big.py").write_text("\n".join([f"# match{i}" for i in range(200)]))
    await run_command(["git", "add", "."], cwd=code_root)
    await run_command(["git", "commit", "-m", "big file"], cwd=code_root)
    result = await search_code(code_root, pattern="match", max_results=10)
    lines = result.stdout.splitlines()
    assert any("truncated" in line for line in lines)
    assert len(lines) == 11  # 10 results + truncation notice


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
async def test_write_test_creates_parent_dirs(code_root):
    write_test(code_root, "nested/deep/test_x.py", "# x\n")
    assert (code_root / "nested" / "deep" / "test_x.py").exists()


@pytest.mark.asyncio
async def test_write_test_no_overwrite_by_default(code_root):
    write_test(code_root, "tests/test_a.py", "# first\n")
    with pytest.raises(FileExistsError):
        write_test(code_root, "tests/test_a.py", "# second\n")


@pytest.mark.asyncio
async def test_write_test_overwrite_flag(code_root):
    write_test(code_root, "tests/test_b.py", "# v1\n")
    write_test(code_root, "tests/test_b.py", "# v2\n", overwrite=True)
    assert "v2" in (code_root / "tests" / "test_b.py").read_text()


@pytest.mark.asyncio
async def test_write_test_path_escape_blocked(code_root):
    with pytest.raises(PathEscapeError):
        write_test(code_root, "../../evil.py", "# evil\n")
