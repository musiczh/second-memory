class SecondMemoryError(Exception):
    """Base error with a stable CLI error code."""

    code = "error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ValidationError(SecondMemoryError):
    code = "validation_error"


class NotInitializedError(SecondMemoryError):
    code = "not_initialized"


class DirtyWorktreeError(SecondMemoryError):
    code = "dirty_worktree"
