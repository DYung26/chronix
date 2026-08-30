"""Helpers for sync command: error classification, result tracking, retry logic."""

from datetime import timezone
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
    project_name: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    data: Optional[Any] = None

    def document_label(self) -> str:
        """Format for display: 'project_name (document_id)' or just 'document_id'."""
        if self.project_name:
            return f"{self.project_name} ({self.document_id})"
        return self.document_id


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


def _sync_single_source_with_retries(
    source: 'SourceRef',
    client: Any,
    tz: timezone = timezone.utc,
) -> tuple[DocumentSyncResult, Optional[Any], list]:
    """
    Sync a single configured source (Google Docs or local file) with retry logic.

    `source.priority` is the source's configured scheduling priority rank
    (see DocumentConfig.priority / LocalFileConfig.priority), threaded
    through so it lands on the resulting ProjectTodoList and, from there, on
    every task via TaskAggregator.

    `tz` is the timezone naive metadata datetimes in the source are assumed
    to already be in; should be the app's configured `scheduling.timezone`.

    `client` must already be constructed for this source (see
    chronix.integrations.factory.get_client) and, for google_docs,
    authenticated -- local_files has no authentication step.

    Returns (result, project, meetings) where project and meetings are None on failure.
    """
    from chronix.integrations.factory import get_parser, get_writer_for_client, fetch_raw_document, backfill_source_data
    from chronix.core.todo import TodoDeriver, parse_document_meetings
    from chronix.core.aggregation import ProjectTodoList
    from chronix.cli.formatting import console

    MAX_RETRIES = 3
    RETRY_BACKOFF_SECONDS = 1

    doc_id = source.source_id
    priority = source.priority

    parser = get_parser(source.type)
    deriver = TodoDeriver(tz=tz)

    label = source.label()
    console.print(f"[dim]Fetching {label}...[/dim]")

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            raw = fetch_raw_document(client, source)
            if source.type == "local_files":
                doc_structure = parser.parse_document(raw, doc_id)
            else:
                doc_structure = parser.parse_document(raw)

            project_name = doc_structure.title
            tasks = deriver.derive_todo_list(doc_structure.to_dict(), source=source.type)
            meetings = parse_document_meetings(doc_structure.to_dict(), tz=tz)

            writer = get_writer_for_client(source.type, client, tz=tz)
            tasks = writer.backfill_missing_ids(doc_id, tasks, source_data=backfill_source_data(raw, source))

            project_todo = ProjectTodoList(
                project_name=project_name or source.project_name,
                tasks=tasks,
                project_id=source.project_name,
                source=source.type,
                document_id=doc_id,
                priority=priority,
            )

            task_word = "task" if len(tasks) == 1 else "tasks"
            meeting_word = "meeting" if len(meetings) == 1 else "meetings"
            console.print(f"  [green]✓[/green] [bold]{project_name}[/bold]: [cyan]{len(tasks)}[/cyan] {task_word}, [cyan]{len(meetings)}[/cyan] {meeting_word}")

            result = DocumentSyncResult(
                document_id=doc_id,
                outcome=SyncOutcome.SUCCESS,
                project_name=source.project_name,
                retry_count=attempt,
                data=(project_todo, meetings)
            )
            return result, project_todo, meetings

        except Exception as e:
            last_error = e
            error_type = classify_sync_error(e)

            if error_type == SyncErrorType.DOCUMENT_NOT_FOUND:
                console.print(f"  [yellow]⊘[/yellow] Not found: {label}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.NOT_FOUND,
                    project_name=source.project_name,
                    error=str(e),
                    retry_count=0
                )
                return result, None, []

            if error_type == SyncErrorType.NON_RETRYABLE:
                console.print(f"  [red]✗[/red] Failed to fetch {label}: {e}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.FAILED_AFTER_RETRIES,
                    project_name=source.project_name,
                    error=str(e),
                    retry_count=0
                )
                return result, None, []

            if attempt < MAX_RETRIES - 1:
                console.print(f"  [yellow]⚠[/yellow] Failed to fetch {label}, retrying ({attempt + 1}/{MAX_RETRIES}): {e}")
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            else:
                console.print(f"  [red]✗[/red] Failed to fetch {label} after {MAX_RETRIES} attempts: {e}")
                result = DocumentSyncResult(
                    document_id=doc_id,
                    outcome=SyncOutcome.FAILED_AFTER_RETRIES,
                    project_name=source.project_name,
                    error=str(last_error),
                    retry_count=MAX_RETRIES
                )
                return result, None, []

    result = DocumentSyncResult(
        document_id=doc_id,
        outcome=SyncOutcome.FAILED_AFTER_RETRIES,
        project_name=source.project_name,
        error=str(last_error),
        retry_count=MAX_RETRIES
    )
    return result, None, []


def _sync_single_document_with_retries(
    doc_id: str,
    client: Any,
    priority: Optional[int] = None,
    tz: timezone = timezone.utc,
    project_name: Optional[str] = None,
) -> tuple[DocumentSyncResult, Optional[Any], list]:
    """Google-Docs-only convenience wrapper around _sync_single_source_with_retries.

    Kept for call sites that only ever deal in Google Docs and already hold
    a bare doc_id (rather than a SourceRef). New multi-source call sites
    should construct a SourceRef(type="google_docs", ...) and call
    _sync_single_source_with_retries directly instead.

    `project_name` is the *configured* project identity (see
    chronix.config.settings.ProjectConfig.name), used to merge this sync
    with any other source under the same project. Defaults to doc_id when
    omitted, which is only correct for a single-source, unnamed-project
    caller -- any caller that knows the real configured project name should
    pass it explicitly.
    """
    from chronix.config.settings import SourceRef

    source = SourceRef(
        type="google_docs",
        source_id=doc_id,
        project_name=project_name or doc_id,
        priority=priority,
    )
    return _sync_single_source_with_retries(source, client, tz=tz)
