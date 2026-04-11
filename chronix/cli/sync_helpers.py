"""Helpers for sync command: error classification, result tracking, retry logic."""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Any
import time


class SyncErrorType(Enum):
    """Types of sync errors."""
    GLOBAL_FAILURE = "global_failure"
    DOCUMENT_NOT_FOUND = "document_not_found"
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


class SyncOutcome(Enum):
    """Outcomes for a document sync."""
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    FAILED_AFTER_RETRIES = "failed_after_retries"


@dataclass
class DocumentSyncResult:
    """Result of syncing a single document."""
    document_id: str
    outcome: SyncOutcome
    error: Optional[str] = None
    retry_count: int = 0
    data: Optional[Any] = None


def classify_sync_error(error: Exception) -> SyncErrorType:
    """
    Classify a sync error as global, retryable, or document-level.
    
    Returns SyncErrorType indicating how to handle the error.
    """
    error_str = str(error).lower()
    error_type = type(error).__name__
    
    if "404" in error_str or "not found" in error_str:
        return SyncErrorType.DOCUMENT_NOT_FOUND
    
    if error_type == "HttpError":
        error_code = getattr(error, "resp", {}).get("status")
        if error_code == 404:
            return SyncErrorType.DOCUMENT_NOT_FOUND
        if error_code in (500, 502, 503, 504, 429):
            return SyncErrorType.RETRYABLE
    
    if any(keyword in error_str for keyword in ["timeout", "connection", "network", "temporary"]):
        return SyncErrorType.RETRYABLE
    
    return SyncErrorType.NON_RETRYABLE


def is_global_failure(error: Exception) -> bool:
    """
    Check if an error is a global/run-level failure that should abort immediately.
    
    Global failures include auth, config, client initialization, etc.
    """
    error_str = str(error).lower()
    
    if any(keyword in error_str for keyword in ["auth", "credential", "oauth", "token", "permission"]):
        return True
    
    if "config" in error_str:
        return True
    
    return False


def _sync_single_document_with_retries(doc_id: str, client: Any) -> tuple[DocumentSyncResult, Optional[Any], list]:
    """
    Sync a single document with retry logic for transient failures.
    
    Returns (result, project, meetings) where project and meetings are None on failure.
    """
    from chronix.integrations.google_docs.parser import GoogleDocsParser
    from chronix.core.todo import TodoDeriver, parse_document_meetings
    from chronix.core.aggregation import ProjectTodoList
    from chronix.cli.formatting import console
    
    MAX_RETRIES = 3
    RETRY_BACKOFF_SECONDS = 1
    
    parser = GoogleDocsParser()
    deriver = TodoDeriver()
    
    console.print(f"[dim]Fetching document {doc_id}...[/dim]")
    
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            doc = client.fetch_document(doc_id)
            doc_structure = parser.parse_document(doc)

            project_name = doc_structure.title
            tasks = deriver.derive_todo_list(doc_structure.to_dict())
            meetings = parse_document_meetings(doc_structure.to_dict())

            project_todo = ProjectTodoList(
                project_name=project_name,
                tasks=tasks,
                document_id=doc_id
            )
            
            console.print(f"  [green]✓[/green] [bold]{project_name}[/bold]: [cyan]{len(tasks)}[/cyan] tasks, [cyan]{len(meetings)}[/cyan] meetings")
            
            result = DocumentSyncResult(
                document_id=doc_id,
                outcome=SyncOutcome.SUCCESS,
                retry_count=attempt,
                data=(project_todo, meetings)
            )
            return result, project_todo, meetings
        
        except Exception as e:
            last_error = e
            error_type = classify_sync_error(e)
            
            if error_type == SyncErrorType.DOCUMENT_NOT_FOUND:
                console.print(f"  [yellow]⊘[/yellow] Document not found: {doc_id}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.NOT_FOUND,
                    error=str(e),
                    retry_count=0
                )
                return result, None, []
            
            if error_type == SyncErrorType.NON_RETRYABLE:
                console.print(f"  [red]✗[/red] Failed to fetch document {doc_id}: {e}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.FAILED_AFTER_RETRIES,
                    error=str(e),
                    retry_count=0
                )
                return result, None, []
            
            # Retryable error
            if attempt < MAX_RETRIES - 1:
                console.print(f"  [yellow]⚠[/yellow] Failed to fetch document {doc_id}, retrying ({attempt + 1}/{MAX_RETRIES}): {e}")
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            else:
                console.print(f"  [red]✗[/red] Failed to fetch document {doc_id} after {MAX_RETRIES} attempts: {e}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.FAILED_AFTER_RETRIES,
                    error=str(last_error),
                    retry_count=MAX_RETRIES
                )
                return result, None, []
    
    result = DocumentSyncResult(
        document_id=doc_id,
        outcome=SyncOutcome.FAILED_AFTER_RETRIES,
        error=str(last_error),
        retry_count=MAX_RETRIES
    )
    return result, None, []

