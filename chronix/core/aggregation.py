"""Project-level task aggregation and normalization."""

from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from chronix.core.models import Task
from chronix.core.dependencies import resolve_task_dependencies, DependencyError


# ---------------------------------------------------------------------------
# Backlog ageing
# ---------------------------------------------------------------------------

_AGEING_HALF_LIFE_DAYS = 30.0

# Sentinel used so unranked projects (Task.priority is None) sort after every
# explicitly ranked one, consistent with ProjectConfig.priority's semantics
# (lower number = higher priority; unset = lowest priority tier).
_UNRANKED_PRIORITY = 2 ** 31


def _priority_sort_key(task: Task) -> int:
    """Sort key for a task's project priority (lower sorts first)."""
    return task.priority if task.priority is not None else _UNRANKED_PRIORITY


def _ageing_bonus_seconds(task: Task, now: datetime) -> float:
    """Return a scheduling bonus in seconds derived from how long a task has existed.

    Applies only to tasks without any deadline.  The bonus grows as the task
    ages and is bounded so it can never exceed the urgency of a legitimate
    deadline task.  The half-life controls how quickly the bonus accumulates:
    a task at half-life age earns half the maximum bonus.

    A larger bonus means the task should be scheduled earlier (lower sort key).
    """
    if task.effective_deadline is not None:
        return 0.0
    if task.created is None:
        return 0.0
    age_days = max(0.0, (now - task.created).total_seconds() / 86400)
    # Bounded growth: bonus saturates asymptotically toward _MAX_AGEING_BONUS_SECONDS
    _MAX_AGEING_BONUS_SECONDS = 3600 * 24 * 14  # 14 days worth of seconds
    bonus = _MAX_AGEING_BONUS_SECONDS * (1.0 - 2.0 ** (-age_days / _AGEING_HALF_LIFE_DAYS))
    return bonus


@dataclass
class ProjectContext:
    """Project identity and metadata.

    Identity is the project's configured name alone (see
    chronix.config.settings.ProjectConfig.name), not source-qualified: a
    project synced from two sources (one google_docs, one local_files)
    produces two ProjectTodoLists that share one equal ProjectContext here,
    which is what lets TaskAggregator.aggregate treat their tasks as one
    backlog. `source` and `document_id` below describe only the *last*
    source stamped onto this particular ProjectContext instance -- for a
    two-source project, prefer chronix.config.settings.SourceRef /
    ChronixConfig.sources_for_project to enumerate all of a project's
    sources rather than relying on these two fields.
    """

    project_id: str
    project_name: str
    source: str = "google_docs"
    document_id: Optional[str] = None
    # Scheduling priority rank from this project's config entry (lower =
    # higher priority; None = unranked). Propagated to each Task at
    # aggregation time -- see TaskAggregator._enrich_task_with_project.
    priority: Optional[int] = None

    def __hash__(self):
        return hash(self.project_id)

    def __eq__(self, other):
        if not isinstance(other, ProjectContext):
            return False
        return self.project_id == other.project_id

    def document_label(self) -> str:
        """Format for display: the project's id."""
        return self.project_id


@dataclass
class AggregatedTask:
    """A task with explicit project context."""

    task: Task
    project_context: ProjectContext

    def __hash__(self):
        return hash((self.task.id, self.project_context))

    def __eq__(self, other):
        if not isinstance(other, AggregatedTask):
            return False
        return self.task.id == other.task.id and self.project_context == other.project_context


# Task fields compared to decide whether two same-id tasks within the same
# project (from different sources) are true duplicates (identical) or a
# conflict (content differs). Deliberately excludes fields that are
# source-derived rather than user-edited content -- source, project,
# document_title, and priority are expected to differ (or be independently
# stamped) across sources and would otherwise falsely flag every
# cross-source duplicate as conflicting.
_CONFLICT_COMPARISON_FIELDS = (
    "title",
    "description",
    "estimated_duration",
    "deadline_user",
    "deadline_external",
    "completed",
    "ref",
    "depends_on",
    "execution_mode",
    "track",
)


def _tasks_conflict(a: Task, b: Task) -> bool:
    """True if two tasks sharing an id differ on any user-editable content field."""
    return any(getattr(a, field) != getattr(b, field) for field in _CONFLICT_COMPARISON_FIELDS)


@dataclass
class TaskConflict:
    """Two or more same-id tasks within one project, from different sources, whose content disagrees.

    Surfaced via TaskAggregator.get_conflicts rather than raised as an error:
    a conflict affecting one task in the backlog should never block sync or
    scheduling for everything else. `versions` holds one AggregatedTask per
    distinct source that has this id, in the order sync encountered them.
    """

    task_id: str
    versions: list[AggregatedTask]


class CrossProjectIdCollisionError(Exception):
    """Raised when the same task id appears under two different projects.

    Unlike a same-project conflict (see TaskConflict), this is not a
    legitimate mirroring scenario -- task ids are meant to be globally
    unique (see chronix.core.models.generate_task_id), so an id shared
    across projects indicates a real anomaly (e.g. a metadata line
    copy-pasted between files, or an exceedingly unlikely id collision),
    not two sources of the same backlog. Aggregation refuses to guess which
    project the task actually belongs to and raises instead of silently
    merging or dropping either task.
    """

    def __init__(self, task_id: str, project_names: list[str]):
        self.task_id = task_id
        self.project_names = project_names
        super().__init__(
            f"Task id '{task_id}' appears under multiple projects ({', '.join(project_names)}), "
            f"which should never happen since task ids are meant to be globally unique. "
            f"This needs manual resolution -- check both projects' sources for a duplicated "
            f"or copy-pasted task id."
        )


class ProjectTodoList:
    """Represents one source's synced tasks for a project.

    `project_id` (defaulting to a normalized form of `project_name` if not
    given explicitly) is what TaskAggregator.aggregate groups sources by --
    two ProjectTodoLists sharing the same `project_id` (e.g. one from a
    project's google_docs source, one from its local_files source) are
    treated as the same project's backlog and merged together.
    """

    def __init__(
        self,
        project_name: str,
        tasks: list[Task],
        project_id: Optional[str] = None,
        source: str = "google_docs",
        document_id: Optional[str] = None,
        priority: Optional[int] = None,
    ):
        self.project_context = ProjectContext(
            project_id=project_id or self._normalize_project_name(project_name),
            project_name=project_name,
            source=source,
            document_id=document_id,
            priority=priority,
        )
        self.tasks = tasks

    @staticmethod
    def _normalize_project_name(name: str) -> str:
        """Normalize project name to create stable identifier."""
        normalized = name.lower().strip()
        normalized = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in normalized)
        normalized = "_".join(filter(None, normalized.split('_')))
        return normalized or "unnamed_project"

    def __len__(self):
        return len(self.tasks)

    def __repr__(self):
        return f"ProjectTodoList(project='{self.project_context.project_name}', tasks={len(self.tasks)})"


class TaskAggregator:
    """Aggregates tasks from multiple projects into a unified view."""

    def __init__(self):
        # Populated by the most recent aggregate() call. A conflict here means
        # the same task id appeared within one project's two sources with
        # differing content (see _tasks_conflict) -- get_conflicts() exposes
        # this for the user to resolve manually rather than aggregate()
        # guessing which version is "right".
        self._last_conflicts: list[TaskConflict] = []

    def aggregate(
        self,
        project_todos: list[ProjectTodoList]
    ) -> list[AggregatedTask]:
        """Aggregate tasks from multiple projects into a single collection.

        Tasks sharing the same non-None id *within the same project*
        (i.e. across that project's two sources) are deduplicated: if every
        compared field agrees (see _CONFLICT_COMPARISON_FIELDS), only the
        first-seen version is kept as a single entry. If any compared field
        disagrees, every version is kept in the returned list (so nothing is
        silently dropped or overwritten) but the id is also recorded in
        get_conflicts() for the user to resolve. Tasks with id=None (not yet
        backfilled) are never deduplicated against each other, since None is
        not a real identity.

        A task id shared across *different* projects is a hard error (see
        CrossProjectIdCollisionError) rather than a conflict: task ids are
        meant to be globally unique, so this should only happen from a real
        anomaly, and guessing which project it "really" belongs to would be
        worse than refusing to proceed.
        """
        aggregated = []
        first_seen_by_id: dict[str, AggregatedTask] = {}
        conflicting_ids: dict[str, list[AggregatedTask]] = {}

        for project_todo in project_todos:
            for task in project_todo.tasks:
                enriched_task = self._enrich_task_with_project(task, project_todo.project_context)
                aggregated_task = AggregatedTask(
                    task=enriched_task,
                    project_context=project_todo.project_context
                )

                task_id = enriched_task.id
                if task_id is None:
                    aggregated.append(aggregated_task)
                    continue

                existing = first_seen_by_id.get(task_id)
                if existing is None:
                    first_seen_by_id[task_id] = aggregated_task
                    aggregated.append(aggregated_task)
                    continue

                if existing.project_context.project_id != aggregated_task.project_context.project_id:
                    raise CrossProjectIdCollisionError(
                        task_id,
                        [existing.project_context.project_name, aggregated_task.project_context.project_name],
                    )

                if _tasks_conflict(existing.task, enriched_task):
                    conflicting_ids.setdefault(task_id, [existing]).append(aggregated_task)
                    aggregated.append(aggregated_task)
                # else: true duplicate: enriched_task is dropped, existing stands.

        self._last_conflicts = [
            TaskConflict(task_id=task_id, versions=versions)
            for task_id, versions in conflicting_ids.items()
        ]

        return aggregated

    def get_conflicts(self) -> list[TaskConflict]:
        """Conflicts found by the most recent aggregate() call.

        Empty before aggregate() has been called, and reset (possibly to
        empty) on every subsequent call -- this reflects only the latest
        aggregation, not an accumulated history across calls.
        """
        return self._last_conflicts

    def _enrich_task_with_project(self, task: Task, project_context: ProjectContext) -> Task:
        """Enrich task with project information if not already set."""
        if not task.project:
            task.project = project_context.project_name

        if task.priority is None:
            task.priority = project_context.priority

        return task

    def get_task_pool(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> list[Task]:
        """Extract raw Task objects from aggregated view and sort globally.

        Tasks whose id appears in get_conflicts() (populated by the most
        recent aggregate() call) are excluded entirely -- with two
        disagreeing versions of the same task, scheduling either arbitrarily
        or both would silently produce a wrong or double-counted schedule.
        Excluding it is visible instead: the task is simply absent from
        today/schedule until the user resolves the conflict, and callers
        that run scheduling commands are expected to warn when
        get_conflicts() is non-empty (see cli.commands).
        """
        conflicted_ids = {c.task_id for c in self._last_conflicts}
        tasks = [
            agg_task.task for agg_task in aggregated_tasks
            if agg_task.task.id not in conflicted_ids
        ]
        sorted_tasks = self._sort_tasks_globally(tasks)
        try:
            return resolve_task_dependencies(sorted_tasks)
        except DependencyError as e:
            raise ValueError(f"Dependency validation failed: {str(e)}") from e
    
    def _sort_tasks_globally(self, tasks: list[Task]) -> list[Task]:
        """
        Sort tasks globally according to deadline-aware prioritization.
        
        Incomplete tasks are categorized by deadline type:
        1. Hard-deadline tasks (external_deadline present)
        2. Soft-deadline tasks (user_deadline present, no external_deadline)
        3. Computed-deadline tasks (deadline_computed present, no real deadline)
        4. No-deadline tasks (no deadline of any kind)

        Within each category, tasks are sorted by:
        - Primary: deadline (earliest first)
        - Secondary: project priority rank (lower rank first; unranked last)
        - Tertiary: duration (shorter first)
        - Quaternary: title (alphabetical)

        Project priority is a soft bias, same as in the scheduler's urgency
        scoring: it only distinguishes tasks that already tie on the primary
        key (or, for no-deadline tasks, ranks above the passive ageing bonus).
        It never lets an unranked/low-priority project's deadline jump ahead
        of a higher-priority project's earlier deadline.

        Completed tasks appear after all incomplete tasks.
        """
        incomplete_hard = []
        incomplete_soft = []
        incomplete_computed = []
        incomplete_none = []
        completed_with_metadata = []
        completed_without_metadata = []

        for task in tasks:
            if task.completed:
                if self._has_valid_metadata(task):
                    completed_with_metadata.append(task)
                else:
                    completed_without_metadata.append(task)
            else:
                if task.deadline_external is not None:
                    incomplete_hard.append(task)
                elif task.deadline_user is not None:
                    incomplete_soft.append(task)
                elif task.deadline_computed is not None:
                    incomplete_computed.append(task)
                else:
                    incomplete_none.append(task)

        max_datetime = datetime.max.replace(tzinfo=timezone.utc)

        hard_sorted = sorted(
            incomplete_hard,
            key=lambda t: (
                t.deadline_external or max_datetime,
                _priority_sort_key(t),
                t.estimated_duration,
                t.title
            )
        )

        soft_sorted = sorted(
            incomplete_soft,
            key=lambda t: (
                t.deadline_user or max_datetime,
                _priority_sort_key(t),
                t.estimated_duration,
                t.title
            )
        )

        computed_sorted = sorted(
            incomplete_computed,
            key=lambda t: (
                t.deadline_computed or max_datetime,
                _priority_sort_key(t),
                t.estimated_duration,
                t.title
            )
        )

        now = datetime.now(timezone.utc)
        none_sorted = sorted(
            incomplete_none,
            key=lambda t: (
                _priority_sort_key(t),
                -_ageing_bonus_seconds(t, now),
                t.estimated_duration,
                t.title,
            )
        )

        completed_meta_sorted = sorted(
            completed_with_metadata,
            key=lambda t: (
                t.estimated_duration,
                t.effective_deadline or max_datetime,
                t.title
            )
        )

        completed_no_meta_sorted = sorted(
            completed_without_metadata,
            key=lambda t: t.title
        )

        return (
            hard_sorted +
            soft_sorted +
            computed_sorted +
            none_sorted +
            completed_meta_sorted +
            completed_no_meta_sorted
        )
    
    def _has_valid_metadata(self, task: Task) -> bool:
        """Check if task has valid metadata (non-default values)."""
        return task.estimated_duration > timedelta(0)

    def get_tasks_by_project(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> dict[str, list[Task]]:
        """Group tasks by project context (project_id -- unique per project, not per source)."""
        by_project = {}

        for agg_task in aggregated_tasks:
            key = agg_task.project_context.project_id
            if key not in by_project:
                by_project[key] = []
            by_project[key].append(agg_task.task)

        return by_project

    def get_all_projects(
        self,
        aggregated_tasks: list[AggregatedTask]
    ) -> list[ProjectContext]:
        """Get all unique project contexts."""
        seen = set()
        projects = []

        for agg_task in aggregated_tasks:
            if agg_task.project_context not in seen:
                seen.add(agg_task.project_context)
                projects.append(agg_task.project_context)
        
        return projects


def aggregate_project_todos(
    project_todos: list[ProjectTodoList]
) -> list[AggregatedTask]:
    """Aggregate multiple project TODO lists into a unified view."""
    aggregator = TaskAggregator()
    return aggregator.aggregate(project_todos)


def create_project_todo(
    project_name: str,
    tasks: list[Task],
    project_id: Optional[str] = None,
    source: str = "google_docs",
    document_id: Optional[str] = None,
    priority: Optional[int] = None,
) -> ProjectTodoList:
    """Create a ProjectTodoList with explicit project identity."""
    return ProjectTodoList(
        project_name=project_name,
        tasks=tasks,
        project_id=project_id,
        source=source,
        document_id=document_id,
        priority=priority,
    )
