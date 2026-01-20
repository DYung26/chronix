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
        Schedule tasks using deadline-aware opportunistic scheduling.
        
        Tasks are placed to minimize deadline violations by:
        - Checking if scheduling a task would endanger higher-priority deadlines
        - Deferring (not skipping) tasks when they would cause conflicts
        - Allowing task interleaving and segmentation
        - Still scheduling tasks even when deadlines are impossible (flagged)

        Args:
            tasks: Ordered list of tasks (sorted by priority)
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

        # Use opportunistic scheduler
        scheduled_tasks, conflicts = self._schedule_opportunistically(
            tasks=tasks,
            start_time=start_time,
            blocked_time=sorted_blocks
        )

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
        Schedule tasks continuously across multiple days using opportunistic scheduling.
        
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
        
        # Use opportunistic scheduler
        scheduled_tasks, conflicts = self._schedule_opportunistically(
            tasks=tasks,
            start_time=start_time,
            blocked_time=sorted_blocks
        )
        
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

    def _schedule_opportunistically(
        self,
        tasks: list[Task],
        start_time: datetime,
        blocked_time: list[TimeBlock]
    ) -> tuple[list[ScheduledTask], list[str]]:
        """
        Schedule tasks using deadline-aware opportunistic algorithm.
        
        This scheduler:
        - Respects the sorted priority order as baseline intent
        - Allows short, safe tasks to run before longer high-priority tasks
        - Ensures external deadlines are protected (tasks deferred if needed)
        - Handles task interleaving and resumption
        - Still schedules tasks even when deadlines are impossible (flags violations)
        
        Returns:
            (list of scheduled task segments, list of conflict messages)
        """
        conflicts = []
        
        # Track remaining work for each incomplete task
        remaining_work = {}
        incomplete_tasks = []
        
        for task in tasks:
            if not task.completed:
                remaining_work[task.id] = task.estimated_duration
                incomplete_tasks.append(task)
        
        current_time = start_time
        
        # Track segments as (task, start, end) tuples
        segments_by_task = defaultdict(list)
        
        # Keep scheduling until all work is done
        while any(duration > timedelta(0) for duration in remaining_work.values()):
            # Find the next task to schedule
            task_to_schedule = self._select_next_task(
                incomplete_tasks,
                remaining_work,
                current_time,
                blocked_time
            )
            
            if task_to_schedule is None:
                # No tasks left to schedule
                break
            
            # Schedule a segment of this task
            segment, next_time = self._schedule_task_segment(
                task=task_to_schedule,
                earliest_start=current_time,
                blocked_time=blocked_time,
                remaining_duration=remaining_work[task_to_schedule.id]
            )
            
            if segment:
                start, end = segment
                segments_by_task[task_to_schedule.id].append((task_to_schedule, start, end))
                segment_duration = end - start
                remaining_work[task_to_schedule.id] -= segment_duration
                current_time = next_time
            else:
                # Could not schedule - should not happen, but safety break
                break
        
        # Convert segments to ScheduledTask objects with proper metadata
        scheduled, conflicts = self._build_scheduled_tasks(segments_by_task)
        
        return scheduled, conflicts

    def _select_next_task(
        self,
        tasks: list[Task],
        remaining_work: dict[str, timedelta],
        current_time: datetime,
        blocked_time: list[TimeBlock]
    ) -> Optional[Task]:
        """
        Select the next task to schedule using urgency-aware logic.
        
        Strategy:
        1. Find tasks with remaining work
        2. For each task, calculate how urgent it is (time until deadline vs time needed)
        3. Prioritize tasks that:
           - Have deadlines that are becoming critical
           - Can fit without violating other critical deadlines
        4. Fall back to sorted order if no urgency differentiation
        """
        candidates = [t for t in tasks if remaining_work.get(t.id, timedelta(0)) > timedelta(0)]
        
        if not candidates:
            return None
        
        # Calculate urgency score for each candidate
        urgency_scores = []
        for task in candidates:
            score = self._calculate_urgency(task, current_time, remaining_work.get(task.id, timedelta(0)), blocked_time)
            urgency_scores.append((score, task))
        
        # Sort by urgency (lower score = more urgent)
        urgency_scores.sort(key=lambda x: x[0])
        
        # Try to schedule the most urgent task that doesn't endanger critical deadlines
        for urgency, task in urgency_scores:
            if self._is_safe_to_schedule(task, current_time, remaining_work, candidates, blocked_time):
                return task
        
        # All tasks would cause violations - schedule most urgent anyway (best effort)
        return urgency_scores[0][1]
    
    def _calculate_urgency(
        self,
        task: Task,
        current_time: datetime,
        remaining_duration: timedelta,
        blocked_time: list[TimeBlock]
    ) -> float:
        """
        Calculate urgency score for a task.
        
        Lower score = more urgent.
        
        Score factors:
        - Time until deadline (less time = more urgent)
        - Time needed to complete (more time needed = more urgent to start)
        - Deadline type (external > user > none)
        """
        if not task.effective_deadline:
            # No deadline - least urgent, use a large number plus duration
            return 1e10 + remaining_duration.total_seconds()
        
        # Time until deadline
        time_until_deadline = task.effective_deadline - current_time
        
        if time_until_deadline <= timedelta(0):
            # Already past deadline - highly urgent
            return -1e9 + time_until_deadline.total_seconds()
        
        # Estimate completion time accounting for blocks
        completion_time = self._estimate_completion_time(current_time, remaining_duration, blocked_time)
        time_with_blocks = completion_time - current_time
        
        # Slack time = how much time we have beyond what we need
        slack = time_until_deadline - time_with_blocks
        
        # Base urgency on slack time
        # Less slack = more urgent (lower score)
        base_score = slack.total_seconds()
        
        # Boost urgency for external deadlines
        if task.deadline_external:
            base_score *= 0.5  # External deadlines are twice as urgent
        
        return base_score

    def _is_safe_to_schedule(
        self,
        task: Task,
        current_time: datetime,
        remaining_work: dict[str, timedelta],
        all_tasks: list[Task],
        blocked_time: list[TimeBlock]
    ) -> bool:
        """
        Check if scheduling this task now would make any critical deadline infeasible.
        
        A deadline is "critical" if:
        - It's an external deadline (hard constraint)
        - OR it's a user deadline that's becoming urgent (less than 2x the time needed)
        
        Returns True if safe to schedule, False if it would endanger critical deadlines.
        """
        # Estimate when this task would finish if scheduled now
        task_remaining = remaining_work.get(task.id, timedelta(0))
        estimated_end = self._estimate_completion_time(
            current_time, task_remaining, blocked_time
        )
        
        # Check each other task with a deadline
        for other_task in all_tasks:
            if other_task.id == task.id:
                continue
            
            if not other_task.effective_deadline:
                continue
            
            other_remaining = remaining_work.get(other_task.id, timedelta(0))
            if other_remaining <= timedelta(0):
                continue
            
            # Only check if the other task's deadline is "critical"
            time_until_other_deadline = other_task.effective_deadline - estimated_end
            time_needed_for_other = self._estimate_duration_with_blocks(
                estimated_end, other_remaining, blocked_time
            )
            
            # Is the other task's deadline critical?
            is_critical = False
            
            if other_task.deadline_external:
                # All external deadlines are critical
                is_critical = True
            elif other_task.deadline_user:
                # User deadlines become critical when slack is less than 2x duration
                slack = time_until_other_deadline - time_needed_for_other
                if slack < time_needed_for_other:  # Less than 2x time needed
                    is_critical = True
            
            # If critical and would be violated, not safe
            if is_critical and time_needed_for_other > time_until_other_deadline:
                return False
        
        return True

    def _estimate_completion_time(
        self,
        start: datetime,
        duration: timedelta,
        blocked_time: list[TimeBlock]
    ) -> datetime:
        """Estimate when a task would complete if started now, accounting for blocks."""
        remaining = duration
        current = start
        
        while remaining > timedelta(0):
            next_block = self._find_next_block(current, blocked_time)
            
            if next_block is None:
                # No more blocks
                return current + remaining
            
            time_until_block = next_block.start - current
            
            if time_until_block <= timedelta(0):
                # Block is now or in past
                current = next_block.end
            elif time_until_block >= remaining:
                # Task fits before block
                return current + remaining
            else:
                # Partial work before block
                remaining -= time_until_block
                current = next_block.end
        
        return current

    def _estimate_duration_with_blocks(
        self,
        start: datetime,
        duration: timedelta,
        blocked_time: list[TimeBlock]
    ) -> timedelta:
        """Calculate effective duration needed accounting for blocked time."""
        completion_time = self._estimate_completion_time(start, duration, blocked_time)
        return completion_time - start

    def _schedule_task_segment(
        self,
        task: Task,
        earliest_start: datetime,
        blocked_time: list[TimeBlock],
        remaining_duration: timedelta
    ) -> tuple[Optional[tuple[datetime, datetime]], datetime]:
        """
        Schedule a single segment of a task.
        
        Returns a (start, end) tuple for the segment and next available time.
        We return raw times instead of ScheduledTask to defer validation.
        """
        current_start = earliest_start
        
        # Skip past any blocks that start before current time
        while True:
            next_block = self._find_next_block(current_start, blocked_time)
            if next_block is None or next_block.start >= current_start:
                break
            current_start = next_block.end
        
        # Find available time until next block
        next_block = self._find_next_block(current_start, blocked_time)
        
        if next_block is None:
            # No more blocks - schedule all remaining duration
            segment_duration = remaining_duration
        else:
            time_until_block = next_block.start - current_start
            
            if time_until_block <= timedelta(0):
                # Block starts now - skip it and try again
                return self._schedule_task_segment(
                    task, next_block.end, blocked_time, remaining_duration
                )
            
            # Schedule up to block or full remaining duration, whichever is less
            segment_duration = min(remaining_duration, time_until_block)
        
        segment_end = current_start + segment_duration
        
        return (current_start, segment_end), segment_end

    def _build_scheduled_tasks(
        self,
        segments_by_task: dict[str, list[tuple[Task, datetime, datetime]]]
    ) -> tuple[list[ScheduledTask], list[str]]:
        """
        Build ScheduledTask objects from raw segment data.
        
        Properly sets segment metadata and violation flags for each task.
        """
        scheduled = []
        conflicts = []
        
        for task_id, segments in segments_by_task.items():
            # Sort segments by start time
            segments.sort(key=lambda s: s[1])
            
            task = segments[0][0]
            final_end = segments[-1][2]
            is_multi_segment = len(segments) > 1
            
            # Check violations based on final end time
            violates_user = self._violates_deadline(final_end, task.deadline_user)
            violates_external = self._violates_deadline(final_end, task.deadline_external)
            
            # Generate conflict messages if violations exist
            if violates_user and task.deadline_user:
                conflicts.append(
                    f"Task '{task.title}' ends at {final_end.strftime('%Y-%m-%d %H:%M')} "
                    f"but user deadline is {task.deadline_user.strftime('%Y-%m-%d %H:%M')}"
                )
            
            if violates_external and task.deadline_external:
                conflicts.append(
                    f"Task '{task.title}' ends at {final_end.strftime('%Y-%m-%d %H:%M')} "
                    f"but external deadline is {task.deadline_external.strftime('%Y-%m-%d %H:%M')}"
                )
            
            # Create ScheduledTask for each segment
            for idx, (_, start, end) in enumerate(segments, start=1):
                scheduled_task = ScheduledTask(
                    task=task,
                    start=start,
                    end=end,
                    violates_deadline_user=violates_user,
                    violates_deadline_external=violates_external,
                    is_segment=is_multi_segment,
                    segment_index=idx if is_multi_segment else None,
                    total_segments=len(segments) if is_multi_segment else None
                )
                scheduled.append(scheduled_task)
        
        # Return in chronological order
        scheduled.sort(key=lambda s: s.start)
        return scheduled, conflicts

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
