"""Google Docs API client."""

from typing import Any, Optional
from pathlib import Path

from chronix.integrations.base import TaskSourceIntegration
from chronix.integrations.google_docs.auth import get_default_auth_strategy, AuthStrategy


class GoogleDocsClient(TaskSourceIntegration):
    """Client for fetching and reading Google Docs documents."""

    def __init__(self, auth_strategy: Optional[AuthStrategy] = None):
        self.auth_strategy = auth_strategy or get_default_auth_strategy()
        self._service = None

    @property
    def service(self) -> Any:
        """Lazily initialize and return the Google Docs API service."""
        if self._service is None:
            self._service = self.auth_strategy.get_service()
        return self._service

    def authenticate(self) -> bool:
        """Authenticate with Google Docs API. Raises exceptions on auth failures."""
        try:
            self.service
            return True
        except Exception as e:
            raise e

    def validate_connection(self) -> bool:
        """Validate connection to Google Docs API."""
        return self.authenticate()

    def fetch_document(self, document_id: str) -> dict[str, Any]:
        """Fetch a single document by ID."""
        return self.service.documents().get(
            documentId=document_id,
            includeTabsContent=True
        ).execute()

    def fetch_document_metadata(self, document_id: str) -> dict[str, Any]:
        """Fetch document metadata (title, creation date, etc)."""
        doc = self.fetch_document(document_id)
        return {
            "document_id": doc.get("documentId"),
            "title": doc.get("title"),
            "revision_id": doc.get("revisionId"),
        }

    def fetch_tasks(self) -> list[Any]:
        """Fetch tasks from Google Docs (placeholder for TaskSourceIntegration)."""
        raise NotImplementedError(
            "Use fetch_document() to retrieve raw content. "
            "Task extraction is handled by a separate parser."
        )

