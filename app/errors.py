from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Error rendered as {error: {code, message, details?}}."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.extra = extra or {}

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error, **self.extra}
