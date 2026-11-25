"""Structured, user-locatable errors for plugin package validation."""

from __future__ import annotations


class PluginPackageError(Exception):
    """A package validation failure with a stable code and optional field/path."""

    def __init__(self, code: str, message: str, *, path: str | None = None) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path is not None:
            result["path"] = self.path
        return result

    def __str__(self) -> str:
        location = f" ({self.path})" if self.path else ""
        return f"[{self.code}]{location} {self.message}"