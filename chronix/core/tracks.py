"""Track resolution: deciding which independently-scheduled timeline a task
belongs to.

Chronix supports two parallel timelines for a single day:

- primary: the main track -- deep-focus, full-attention work.
- secondary: a second, independently-scheduled track for tasks that can
  realistically run alongside the primary track (e.g. in a second terminal
  pane or tmux session) without needing your full attention at once.

A task's ``Task.track`` field is either an explicit override ("primary" /
"secondary") or "auto", in which case this module infers the track from the
task's execution mode and duration. Explicit overrides always win -- the
auto heuristic only ever applies to "auto" tasks.

Track assignment is about *how a task would be executed* -- whether it needs
full attention or can be backgrounded -- not about *how important* it is.
Priority, deadlines, and urgency scoring are untouched by this module and
continue to operate independently within each track.
"""

from datetime import timedelta
from typing import Literal

from chronix.core.models import Task, AUTO_SECONDARY_ATOMIC_THRESHOLD_MINUTES


Track = Literal["primary", "secondary"]


def resolve_track(task: Task) -> Track:
    """Resolve the effective track for a task.

    Explicit track=primary/secondary on the task always wins. For
    track="auto" (the default), applies a heuristic based on execution mode
    and duration:

    - flex tasks are natively chunkable/interruptible -- secondary-eligible.
    - atomic tasks at or under AUTO_SECONDARY_ATOMIC_THRESHOLD_MINUTES are
      short enough to background -- secondary-eligible.
    - Everything else (contiguous_preferred, and atomic tasks longer than
      the threshold) defaults to primary: these are the tasks that
      typically demand focused, uninterrupted attention.

    Note: Task's default execution_mode for durations over 90 minutes is
    contiguous_preferred, not flex (see Task.set_default_execution_mode).
    This is deliberate: a long task should default to primary here unless
    someone explicitly marks it flex, rather than silently landing in the
    background track just because it's long.
    """
    if task.track == "primary":
        return "primary"
    if task.track == "secondary":
        return "secondary"

    if task.execution_mode == "flex":
        return "secondary"

    if task.execution_mode == "atomic":
        threshold = timedelta(minutes=AUTO_SECONDARY_ATOMIC_THRESHOLD_MINUTES)
        if task.estimated_duration <= threshold:
            return "secondary"

    return "primary"


def partition_by_track(tasks: list[Task]) -> tuple[list[Task], list[Task]]:
    """Split a task list into (primary_tasks, secondary_tasks) via resolve_track.

    Order within each returned list is preserved from the input, so callers
    that pass an already-sorted pool (e.g. TaskAggregator.get_task_pool) get
    two sorted sublists back.
    """
    primary: list[Task] = []
    secondary: list[Task] = []
    for task in tasks:
        if resolve_track(task) == "secondary":
            secondary.append(task)
        else:
            primary.append(task)
    return primary, secondary
