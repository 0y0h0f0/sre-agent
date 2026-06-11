"""Application error types."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base exception rendered as the standard API error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class ValidationAppError(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            "VALIDATION_ERROR",
            message,
            status_code=400,
            details=details,
        )


class NotFoundError(AppError):
    def __init__(self, resource: str, public_id: str) -> None:
        super().__init__(
            "NOT_FOUND",
            f"{resource} not found",
            status_code=404,
            details={"id": public_id},
        )


class ConflictError(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("CONFLICT", message, status_code=409, details=details)


class DependencyUnavailableError(AppError):
    def __init__(self, dependency: str, message: str) -> None:
        super().__init__(
            "DEPENDENCY_UNAVAILABLE",
            message,
            status_code=503,
            details={"dependency": dependency},
        )


class ApprovalRequiredError(AppError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("APPROVAL_REQUIRED", message, status_code=403, details=details)


class TooManyRequestsError(AppError):
    def __init__(
        self,
        message: str = "Rate limit exceeded",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("TOO_MANY_REQUESTS", message, status_code=429, details=details)
