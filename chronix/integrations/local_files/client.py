"""Local filesystem client for reading plain-text task files."""

from pathlib import Path
from typing import Any

from chronix.integrations.base import TaskSourceIntegration


class LocalFileClient(TaskSourceIntegration):
    """Client for reading local plain-text task files from disk."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path).expanduser().resolve()

    def authenticate(self) -> bool:
        """No authentication is required for local files."""
        return True

    def validate_connection(self) -> bool:
        """Validate the configured file exists and is readable."""
        return self.file_path.is_file()

    def fetch_document(self) -> dict[str, Any]:
        """Read the file and return its raw content plus resolved document id."""
        if not self.file_path.is_file():
            raise FileNotFoundError(f"Local task file not found: {self.file_path}")
        content = self.file_path.read_text(encoding="utf-8")
        return {"document_id": str(self.file_path), "content": content}

    def fetch_tasks(self) -> list[Any]:
        """Fetch tasks from the local file (placeholder for TaskSourceIntegration)."""
        raise NotImplementedError(
            "Use fetch_document() to retrieve raw content. "
            "Task extraction is handled by a separate parser."
        )
