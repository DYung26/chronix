"""Structured error types returned by MCP tools instead of raising.

FastMCP tools return their result as the model's next piece of context, so
validation failures are represented as regular return values (not raised
exceptions) whenever the calling model can plausibly self-correct from the
shape of the error -- e.g. a missing required field it can go re-ask the
user for and retry.
"""

from typing import Any, Optional


def missing_fields_error(command: str, missing: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a structured error describing which fields a write command still needs.

    `missing` is a list of `{"field": ..., "description": ...}` entries so the
    calling model knows both what to ask the user for and why it's needed.
    """
    return {
        "ok": False,
        "error": "missing_required_fields",
        "command": command,
        "missing_fields": missing,
    }


def not_found_error(kind: str, identifier: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{kind}_not_found",
        "identifier": identifier,
    }


def validation_error(message: str, field: Optional[str] = None) -> dict[str, Any]:
    error: dict[str, Any] = {"ok": False, "error": "validation_error", "message": message}
    if field is not None:
        error["field"] = field
    return error


def command_error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": "command_failed", "message": message}
