"""Abstract interface for writing tasks to task sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from chronix.core.models import Task

_UNSET = object()


@dataclass(frozen=True)
class NewTask:
    """An immutable description of a task to be created.

    Well-known fields (title, duration, deadlines, mode) are first-class.
    Arbitrary extra metadata can be passed via ``extra`` and will be
    preserved verbatim in the serialized line after the well-known keys.

    ``created`` is set automatically by the writer when not supplied.

    ``tab`` selects which document tab (by title or tab ID) to insert the
    task into, for sources that support tabs. If unset, the writer falls
    back to its default (e.g. the first tab with a valid task section).
    """

    title: str
    duration: timedelta
    id: Optional[str] = None
    external_deadline: Optional[datetime] = None
    user_deadline: Optional[datetime] = None
    mode: Optional[str] = None
    ref: Optional[str] = None
    depends: Optional[str] = None
    created: Optional[datetime] = None
    tab: Optional[str] = None
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskUpdate:
    """Describes the set of changes to apply to an existing task.

    Only fields explicitly set by the caller are applied; everything else is
    left unchanged.  Use the ``set_*`` helpers or assign directly.

    ``metadata`` holds arbitrary extra key=value pairs to merge into the
    task's existing metadata.  Unknown keys in the document are preserved.
    ``metadata_remove`` lists keys to delete from the metadata section.

    Both ``completed=True`` and ``completed=False`` are meaningful, so None
    means "do not change completion state".
    """

    title: Optional[str] = None
    duration: Optional[timedelta] = None
    external_deadline: Optional[datetime] = field(default=_UNSET)  # type: ignore[assignment]
    user_deadline: Optional[datetime] = field(default=_UNSET)  # type: ignore[assignment]
    mode: Optional[str] = None
    completed: Optional[bool] = None
    metadata: dict[str, str] = field(default_factory=dict)
    metadata_remove: list[str] = field(default_factory=list)

    def has_external_deadline_change(self) -> bool:
        return self.external_deadline is not _UNSET

    def has_user_deadline_change(self) -> bool:
        return self.user_deadline is not _UNSET


class TaskWriter(ABC):
    """Abstract interface for writing tasks to a task source."""

    @abstractmethod
    def create_task(self, document_id: str, task: NewTask) -> None:
        """Write a new task to the specified document."""
        ...

    @abstractmethod
    def update_task(self, document_id: str, task_id: str, update: TaskUpdate) -> None:
        """Apply a generic update to an existing task identified by its persistent ID.

        Only fields set on ``update`` are modified; all others are preserved.
        Raises ``TaskNotFoundError`` if no task with ``task_id`` exists.
        """
        ...

    @abstractmethod
    def delete_task(self, document_id: str, task_id: str) -> None:
        """Remove a task from the document, leaving surrounding content intact.

        Raises ``TaskNotFoundError`` if no task with ``task_id`` exists.
        """
        ...

    @abstractmethod
    def backfill_missing_ids(
        self,
        document_id: str,
        tasks: list[Task],
        source_data: Any = None,
    ) -> list[Task]:
        """Assign persistent IDs to any tasks without them, writing back to the source.

        source_data may be passed to avoid a redundant document fetch when the
        caller already holds the raw source document.

        Returns the task list with IDs populated on previously-unidentified tasks.
        """
        ...


class TaskNotFoundError(Exception):
    """Raised when a task with the given ID cannot be located in the document."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Task with id='{task_id}' not found")
