"""Projected-deadline backfilling for tasks that lack a real deadline.

Tasks with neither `deadline_external` nor `deadline_user` are stacked
sequentially, oldest-created first, each claiming a slice of the timeline
equal to its `estimated_duration`. The stack starts after the later of "now"
or the latest deadline already committed by the rest of the backlog, so the
projection doesn't pretend the calendar is emptier than it is.

Results are advisory (`deadline_computed`): they feed scheduling urgency and
ordering but are never treated as critical, unlike a real external or user
deadline.
"""

from dataclasses import dataclass
from datetime import datetime

from chronix.core.models import Task


@dataclass
class ComputedDeadline:
    task: Task
    deadline: datetime


def compute_backlog_deadlines(tasks: list[Task], now: datetime) -> list[ComputedDeadline]:
    """Project a `deadline_computed` for every incomplete, real-deadline-free task.

    `tasks` should be the full aggregated pool so the projection accounts for
    all committed work, regardless of which subset the caller ultimately
    writes back.
    """
    eligible = [
        t for t in tasks
        if not t.completed and t.deadline_external is None and t.deadline_user is None
    ]
    if not eligible:
        return []

    committed_deadlines = [
        t.deadline_external or t.deadline_user
        for t in tasks
        if not t.completed and (t.deadline_external or t.deadline_user)
    ]
    anchor = max(committed_deadlines, default=now)
    anchor = max(anchor, now)

    ordered = sorted(eligible, key=_backlog_sort_key)

    results = []
    cursor = anchor
    for task in ordered:
        cursor = cursor + task.estimated_duration
        results.append(ComputedDeadline(task=task, deadline=cursor))
    return results


def _backlog_sort_key(task: Task):
    """Oldest-created first; tasks without a `created` timestamp sort last."""
    return (task.created is None, task.created, task.title)
