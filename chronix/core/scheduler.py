"""Time-aware scheduling engine for placing tasks into concrete time slots."""

from datetime import datetime, timedelta, date
from typing import Optional

from chronix.core.models import Task, TimeBlock, ScheduledTask, DaySchedule


class SchedulingEngine:
    """Places ordered tasks into time slots while respecting blocked time."""
    
    def schedule_tasks(
        self,
        tasks: list[Task],
        start_time: datetime,
        blocked_time: list[TimeBlock]
    ) -> DaySchedule:
        """
        Schedule tasks starting from start_time, respecting blocked time.
        
        Args:
            tasks: Ordered list of tasks to schedule
            start_time: When to start scheduling (timezone-aware)
            blocked_time: Time blocks that cannot be used (sleep, meetings, etc.)
        
        Returns:
            DaySchedule with scheduled tasks and conflict information
        """
        if start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        
        for block in blocked_time:
            if block.start.tzinfo is None or block.end.tzinfo is None:
                raise ValueError("All blocked time must be timezone-aware")
        
        sorted_blocks = sorted(blocked_time, key=lambda b: b.start)
        
        scheduled_tasks = []
        conflicts = []
        current_time = start_time
        
        for task in tasks:
            if task.completed:
                continue
            
            scheduled_task, next_time = self._schedule_single_task(
                task=task,
                earliest_start=current_time,
                blocked_time=sorted_blocks
            )
            
            scheduled_tasks.append(scheduled_task)
            
            violations = self._check_deadline_violations(scheduled_task)
            conflicts.extend(violations)
            
            current_time = next_time
        
        schedule_date = start_time.date()
        
        return DaySchedule(
            date=schedule_date,
            scheduled_tasks=scheduled_tasks,
            blocked_time=sorted_blocks,
            conflicts=conflicts
        )
    
    def _schedule_single_task(
        self,
        task: Task,
        earliest_start: datetime,
        blocked_time: list[TimeBlock]
    ) -> tuple[ScheduledTask, datetime]:
        """
        Schedule a single task starting from earliest_start.
        
        Returns:
            (ScheduledTask, next_available_time)
        """
        candidate_start = earliest_start
        task_duration = task.estimated_duration
        
        while True:
            candidate_end = candidate_start + task_duration
            
            conflict_block = self._find_conflicting_block(
                candidate_start,
                candidate_end,
                blocked_time
            )
            
            if conflict_block is None:
                break
            
            candidate_start = conflict_block.end
        
        task_start = candidate_start
        task_end = candidate_start + task_duration
        
        violates_user = self._violates_deadline(task_end, task.deadline_user)
        violates_external = self._violates_deadline(task_end, task.deadline_external)
        
        scheduled_task = ScheduledTask(
            task=task,
            start=task_start,
            end=task_end,
            violates_deadline_user=violates_user,
            violates_deadline_external=violates_external
        )
        
        return scheduled_task, task_end
    
    def _find_conflicting_block(
        self,
        start: datetime,
        end: datetime,
        blocked_time: list[TimeBlock]
    ) -> Optional[TimeBlock]:
        """
        Find first blocked time that conflicts with [start, end).
        
        Returns:
            Conflicting TimeBlock or None if no conflict
        """
        for block in blocked_time:
            if self._time_ranges_overlap(start, end, block.start, block.end):
                return block
        
        return None
    
    def _time_ranges_overlap(
        self,
        start1: datetime,
        end1: datetime,
        start2: datetime,
        end2: datetime
    ) -> bool:
        """Check if two time ranges overlap."""
        return start1 < end2 and start2 < end1
    
    def _violates_deadline(
        self,
        task_end: datetime,
        deadline: Optional[datetime]
    ) -> bool:
        """Check if task end time violates deadline."""
        if deadline is None:
            return False
        return task_end > deadline
    
    def _check_deadline_violations(
        self,
        scheduled_task: ScheduledTask
    ) -> list[str]:
        """Generate human-readable conflict messages for deadline violations."""
        conflicts = []
        task = scheduled_task.task
        
        if scheduled_task.violates_deadline_user:
            conflicts.append(
                f"Task '{task.title}' ends at {scheduled_task.end.strftime('%Y-%m-%d %H:%M')} "
                f"but user deadline is {task.deadline_user.strftime('%Y-%m-%d %H:%M')}"
            )
        
        if scheduled_task.violates_deadline_external:
            conflicts.append(
                f"Task '{task.title}' ends at {scheduled_task.end.strftime('%Y-%m-%d %H:%M')} "
                f"but external deadline is {task.deadline_external.strftime('%Y-%m-%d %H:%M')}"
            )
        
        return conflicts


def schedule_day(
    tasks: list[Task],
    start_time: datetime,
    blocked_time: Optional[list[TimeBlock]] = None
) -> DaySchedule:
    """
    Convenience function to schedule tasks for a day.
    
    Args:
        tasks: Ordered list of tasks to schedule
        start_time: When to start scheduling
        blocked_time: Time blocks to avoid (optional)
    
    Returns:
        DaySchedule with scheduled tasks
    """
    engine = SchedulingEngine()
    return engine.schedule_tasks(
        tasks=tasks,
        start_time=start_time,
        blocked_time=blocked_time or []
    )


def create_time_block(
    start: datetime,
    end: datetime,
    kind: str,
    label: Optional[str] = None
) -> TimeBlock:
    """
    Convenience function to create a TimeBlock.
    
    Args:
        start: Block start time (timezone-aware)
        end: Block end time (timezone-aware)
        kind: Block type (e.g., 'sleep', 'meeting', 'break')
        label: Optional descriptive label
    
    Returns:
        TimeBlock instance
    """
    return TimeBlock(start=start, end=end, kind=kind, label=label)
