"""Time-aware scheduling engine for placing tasks into concrete time slots."""

from datetime import datetime, timedelta, date
from typing import Optional
from collections import defaultdict

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

            task_segments, next_time = self._schedule_single_task(
                task=task,
                earliest_start=current_time,
                blocked_time=sorted_blocks
            )

            scheduled_tasks.extend(task_segments)

            # Check deadline violations (only need to check once per task)
            if task_segments:
                violations = self._check_deadline_violations(task_segments[0])
                conflicts.extend(violations)

            current_time = next_time

        schedule_date = start_time.date()

        return DaySchedule(
            date=schedule_date,
            scheduled_tasks=scheduled_tasks,
            blocked_time=sorted_blocks,
            conflicts=conflicts
        )

    def schedule_continuous(
        self,
        tasks: list[Task],
        start_time: datetime,
        num_days: int,
        daily_blocked_time_fn
    ) -> dict[date, DaySchedule]:
        """
        Schedule tasks continuously across multiple days.
        
        Args:
            tasks: Ordered list of tasks to schedule
            start_time: When to start scheduling (timezone-aware)
            num_days: Number of days to schedule
            daily_blocked_time_fn: Function that takes a date and returns blocked time for that day
        
        Returns:
            Dictionary mapping date to DaySchedule
        """
        if start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        
        # Collect all blocked time across all days
        all_blocked_time = []
        for day_offset in range(num_days + 1):  # Extra day to handle overflow
            day_date = start_time.date() + timedelta(days=day_offset)
            day_blocks = daily_blocked_time_fn(day_date)
            all_blocked_time.extend(day_blocks)
        
        # Sort all blocked time
        sorted_blocks = sorted(all_blocked_time, key=lambda b: b.start)
        
        # Schedule all tasks continuously
        scheduled_tasks = []
        conflicts = []
        current_time = start_time
        
        for task in tasks:
            if task.completed:
                continue
            
            task_segments, next_time = self._schedule_single_task(
                task=task,
                earliest_start=current_time,
                blocked_time=sorted_blocks
            )
            
            scheduled_tasks.extend(task_segments)
            
            # Check deadline violations
            if task_segments:
                violations = self._check_deadline_violations(task_segments[0])
                conflicts.extend(violations)
            
            current_time = next_time
        
        # Partition scheduled tasks and blocked time by day
        schedules_by_day = {}
        
        for day_offset in range(num_days):
            day_date = start_time.date() + timedelta(days=day_offset)
            day_start = datetime.combine(day_date, datetime.min.time(), tzinfo=start_time.tzinfo)
            day_end = datetime.combine(day_date, datetime.max.time(), tzinfo=start_time.tzinfo).replace(
                hour=23, minute=59, second=59
            )
            
            # Filter scheduled tasks that have any portion in this day
            day_scheduled = [
                st for st in scheduled_tasks
                if st.start.date() <= day_date <= st.end.date()
            ]
            
            # Filter blocked time for this day
            day_blocked = [
                block for block in sorted_blocks
                if block.start.date() <= day_date <= block.end.date()
            ]
            
            # Only include conflicts for the first day (they're already strings)
            day_conflicts = conflicts if day_offset == 0 else []
            
            schedules_by_day[day_date] = DaySchedule(
                date=day_date,
                scheduled_tasks=day_scheduled,
                blocked_time=day_blocked,
                conflicts=day_conflicts
            )
        
        return schedules_by_day

    def _schedule_single_task(
        self,
        task: Task,
        earliest_start: datetime,
        blocked_time: list[TimeBlock]
    ) -> tuple[list[ScheduledTask], datetime]:
        """
        Schedule a single task starting from earliest_start, potentially splitting across time windows.
        
        Args:
            task: Task to schedule
            earliest_start: Earliest possible start time
            blocked_time: List of blocked time blocks
        
        Returns:
            (list of ScheduledTask segments, next_available_time)
        """
        segments = []
        remaining_duration = task.estimated_duration
        current_start = earliest_start
        
        while remaining_duration > timedelta(0):
            # Find available time until next block
            next_block = self._find_next_block(current_start, blocked_time)
            
            if next_block is None:
                # No more blocks - schedule remaining duration
                segment_end = current_start + remaining_duration
                segments.append((current_start, segment_end))
                current_start = segment_end
                remaining_duration = timedelta(0)
            else:
                # Block exists - check if we can fit some work before it
                time_until_block = next_block.start - current_start
                
                if time_until_block <= timedelta(0):
                    # Block starts at or before current time - skip past it
                    current_start = next_block.end
                elif time_until_block >= remaining_duration:
                    # Entire remaining task fits before block
                    segment_end = current_start + remaining_duration
                    segments.append((current_start, segment_end))
                    current_start = segment_end
                    remaining_duration = timedelta(0)
                else:
                    # Partial fit before block
                    segment_end = current_start + time_until_block
                    segments.append((current_start, segment_end))
                    remaining_duration -= time_until_block
                    current_start = next_block.end
        
        # Create ScheduledTask objects for each segment
        scheduled_segments = []
        final_end = segments[-1][1] if segments else earliest_start
        
        violates_user = self._violates_deadline(final_end, task.deadline_user)
        violates_external = self._violates_deadline(final_end, task.deadline_external)
        
        is_multi_segment = len(segments) > 1
        
        for idx, (seg_start, seg_end) in enumerate(segments, start=1):
            scheduled_task = ScheduledTask(
                task=task,
                start=seg_start,
                end=seg_end,
                violates_deadline_user=violates_user,
                violates_deadline_external=violates_external,
                is_segment=is_multi_segment,
                segment_index=idx if is_multi_segment else None,
                total_segments=len(segments) if is_multi_segment else None
            )
            scheduled_segments.append(scheduled_task)
        
        return scheduled_segments, final_end
    
    def _find_next_block(
        self,
        from_time: datetime,
        blocked_time: list[TimeBlock]
    ) -> Optional[TimeBlock]:
        """
        Find the next blocked time that starts at or after from_time.
        
        Returns:
            Next TimeBlock or None if no blocks remain
        """
        for block in blocked_time:
            if block.start >= from_time or block.end > from_time:
                return block
        return None
    
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
