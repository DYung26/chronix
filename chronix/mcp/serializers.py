"""JSON-safe serializers for chronix domain objects returned by MCP tools.

Pydantic models (Task, ScheduledTask, DaySchedule, TimeBlock) already provide
`model_dump(mode="json")`, but the field set that makes sense over MCP is
narrower than the full internal model, and a few fields (e.g. computed
properties, project labels) live outside the model entirely. These
functions assemble the response shape actually useful to a calling model,
rather than exposing internal representation as-is.
"""

from datetime import timedelta
from typing import Any, Optional

from chronix.core.aggregation import AggregatedTask, ProjectContext
from chronix.core.models import DaySchedule, ScheduledTask, Task, TimeBlock


def _duration_str(duration: timedelta) -> str:
    from chronix.core.metadata import serialize_duration
    return serialize_duration(duration)


def serialize_task(task: Task, project_label: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "project": project_label or task.project,
        "section": task.section,
        "document_title": task.document_title,
        "duration": _duration_str(task.estimated_duration),
        "remaining_duration": _duration_str(task.remaining_duration),
        "deadline_user": task.deadline_user.isoformat() if task.deadline_user else None,
        "deadline_external": task.deadline_external.isoformat() if task.deadline_external else None,
        "deadline_computed": task.deadline_computed.isoformat() if task.deadline_computed else None,
        "completed": task.completed,
        "ref": task.ref,
        "depends_on": task.depends_on,
        "execution_mode": task.execution_mode,
        "track": task.track,
        "priority": task.priority,
        "is_active": task.is_active,
        "is_paused": task.is_paused,
    }


def serialize_aggregated_task(agg_task: AggregatedTask) -> dict[str, Any]:
    return serialize_task(agg_task.task, project_label=agg_task.project_context.document_label())


def serialize_time_block(block: TimeBlock) -> dict[str, Any]:
    return {
        "type": "blocked",
        "start": block.start.isoformat(),
        "end": block.end.isoformat(),
        "kind": block.kind,
        "label": block.label,
    }


def serialize_scheduled_task(scheduled: ScheduledTask) -> dict[str, Any]:
    return {
        "type": "task",
        "start": scheduled.start.isoformat(),
        "end": scheduled.end.isoformat(),
        "task": serialize_task(scheduled.task),
        "violates_deadline_user": scheduled.violates_deadline_user,
        "violates_deadline_external": scheduled.violates_deadline_external,
        "is_segment": scheduled.is_segment,
        "segment_index": scheduled.segment_index,
        "total_segments": scheduled.total_segments,
        "is_partial": scheduled.is_partial,
    }


def serialize_day_schedule(schedule: DaySchedule) -> dict[str, Any]:
    segments = [serialize_scheduled_task(s) for s in schedule.scheduled_tasks]
    segments += [serialize_time_block(b) for b in schedule.blocked_time]
    segments.sort(key=lambda seg: seg["start"])

    return {
        "date": schedule.date.isoformat(),
        "segments": segments,
        "conflicts": schedule.conflicts,
        "total_scheduled": len(schedule.scheduled_tasks),
    }


def serialize_project_context(project: ProjectContext) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "project_name": project.project_name,
        "document_id": project.document_id,
        "priority": project.priority,
        "label": project.document_label(),
    }
