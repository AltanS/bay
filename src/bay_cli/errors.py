"""Bay CLI error types with structured error codes."""

from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    """Named error codes for machine-readable error classification."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    CONFIG_ERROR = "CONFIG_ERROR"
    REMOTE_ERROR = "REMOTE_ERROR"
    DEPENDENCY_ERROR = "DEPENDENCY_ERROR"


# Exit code mapping: ErrorCode -> process exit code
_EXIT_CODES: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 1,
    ErrorCode.NOT_FOUND: 1,
    ErrorCode.CONFLICT: 2,
    ErrorCode.REMOTE_ERROR: 3,
    ErrorCode.CONFIG_ERROR: 4,
    ErrorCode.DEPENDENCY_ERROR: 4,
}


class BayError(Exception):
    """User-facing error with structured metadata.

    Backwards-compatible: ``raise BayError("message")`` still works.
    New code can use error codes: ``raise BayError("msg", code=ErrorCode.CONFLICT)``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.VALIDATION_ERROR,
        field: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.hint = hint

    @property
    def exit_code(self) -> int:
        """Return the process exit code for this error."""
        return _EXIT_CODES.get(self.code, 1)

    def to_dict(self) -> dict[str, str | None]:
        """Return a JSON-serializable error object."""
        return {
            "code": self.code.value,
            "field": self.field,
            "message": str(self),
            "hint": self.hint,
        }

    # ── Convenience constructors ─────────────────────────────────────

    @classmethod
    def not_found(cls, resource: str, name: str) -> BayError:
        """Resource does not exist."""
        return cls(
            f"{resource} '{name}' not found",
            code=ErrorCode.NOT_FOUND,
            field=resource,
        )

    @classmethod
    def conflict(cls, field: str, value: str, owner: str) -> BayError:
        """Resource already in use."""
        return cls(
            f"{field} '{value}' is already used by '{owner}'",
            code=ErrorCode.CONFLICT,
            field=field,
        )

    @classmethod
    def config(cls, message: str, *, hint: str | None = None) -> BayError:
        """Configuration error."""
        return cls(message, code=ErrorCode.CONFIG_ERROR, hint=hint)

    @classmethod
    def dependency(cls, service: str, dependents: list[str]) -> BayError:
        """Cannot remove due to dependents."""
        deps = ", ".join(dependents)
        return cls(
            f"Cannot remove '{service}' — required by: {deps}",
            code=ErrorCode.DEPENDENCY_ERROR,
            field=service,
            hint=f"Remove {deps} first, or use --force",
        )

    @classmethod
    def remote(cls, message: str, *, hint: str | None = None) -> BayError:
        """Remote operation failed."""
        return cls(message, code=ErrorCode.REMOTE_ERROR, hint=hint)
