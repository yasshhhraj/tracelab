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


class PathEscapeError(ToolError):
    """Raised when a file path resolves outside the allowed root directory."""
