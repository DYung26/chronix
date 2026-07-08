"""Time-aware scheduling engine for placing tasks into concrete time slots."""

from datetime import datetime, timedelta, date
from typing import Optional
from collections import defaultdict

from pydantic import ValidationError

from chronix.core.models import Task, TimeBlock, ScheduledTask, DaySchedule


# Chunk policy constants
WHOLE_TASK_THRESHOLD = timedelta(minutes=90)
DEFAULT_TARGET_CHUNK = timedelta(minutes=90)
DEFAULT_MIN_CHUNK = timedelta(minutes=60)
DEFAULT_MAX_CHUNKS_PER_DAY = 2

# Soft per-rank nudge applied to a task's urgency score based on its source
# document's configured priority (DocumentConfig.priority; rank 1 = highest).
# Deliberately modest relative to typical deadline slack (hours/days): this
# lets document priority break ties and nudge ordering among comparably
# urgent tasks -- or fully determine order among tasks with no deadline at
# all -- without letting a merely "preferred" document jump ahead of a
# critical deadline elsewhere. _is_safe_to_schedule's deadline protection is
# unaffected by this bias.
PRIORITY_BIAS_STEP_SECONDS = 4 * 3600

# Bias rank used for unranked (Task.priority is None) tasks -- deliberately
# higher than any realistic explicit rank, so an unranked document's tasks
# are always nudged less urgent than an explicitly ranked one's, matching
# DocumentConfig.priority's "unset = lowest priority" semantics. If a config
# ever ranks 25+ documents this assumption should be revisited.
UNRANKED_PRIORITY_BIAS_RANK = 25


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
        num_days: int | None,
        daily_blocked_time_fn
    ) -> dict[date, DaySchedule]:
        """
        Schedule tasks continuously across multiple days using opportunistic scheduling.
        
        Args:
            tasks: Ordered list of tasks to schedule
            start_time: When to start scheduling (timezone-aware)
            num_days: Number of days to schedule. If None, schedules all tasks (with no day limit)
            daily_blocked_time_fn: Function that takes a date and returns blocked time for that day
        
        Returns:
            Dictionary mapping date to DaySchedule
        """
        if start_time.tzinfo is None:
            raise ValueError("start_time must be timezone-aware")
        
        # Determine lookahead days for collecting blocked time
        # For unlimited scheduling, use a large buffer based on total task work
        if num_days is None:
            total_work = sum(t.estimated_duration.total_seconds() for t in tasks if not t.completed)
            # Estimate: ~8 hours per day = 28800 seconds; add 20% buffer for blocked time
            estimated_days_needed = max(10, int((total_work / 28800) * 1.2) + 5)
            lookahead_days = estimated_days_needed
        else:
            lookahead_days = num_days
        
        # Collect all blocked time across all days
        all_blocked_time = []
        for day_offset in range(lookahead_days + 1):  # Extra day to handle overflow
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
        
        # Determine the actual number of days to return
        if num_days is None:
            # Find the last day with scheduled tasks
            max_day_offset = 0
            for st in scheduled_tasks:
                day_offset = (st.end.date() - start_time.date()).days
                max_day_offset = max(max_day_offset, day_offset)
            days_to_return = max_day_offset + 1
        else:
            days_to_return = num_days
        
        for day_offset in range(days_to_return):
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
        - Respects dependency constraints (tasks cannot start until dependencies complete)
        - Applies execution mode policies: atomic, flex, contiguous_preferred
        
        Returns:
            (list of scheduled task segments, list of conflict messages)
        """
        conflicts = []
        
        # Track remaining work for each incomplete task
        remaining_work = {}
        incomplete_tasks = []
        completion_times = {}
        
        for task in tasks:
            if not task.completed:
                remaining_work[id(task)] = task.estimated_duration
                incomplete_tasks.append(task)
        
        current_time = start_time
        
        # Track segments as (task, start, end) tuples
        segments_by_task = defaultdict(list)
        
        # Track chunks scheduled per task per calendar day
        chunks_scheduled_today = defaultdict(lambda: defaultdict(int))

        # Tasks that failed to actually place a chunk at current_time despite
        # passing _select_next_task's pre-check (e.g. a dependency pushed
        # earliest_allowed_start into a new day, changing the chunk-cap math
        # after selection). Excluding them lets other tasks keep progressing
        # instead of aborting the whole schedule; cleared once time advances.
        unschedulable_now: set[int] = set()
        
        # Keep scheduling until all work is done
        while any(duration > timedelta(0) for duration in remaining_work.values()):
            # Find the next task to schedule
            task_to_schedule = self._select_next_task(
                incomplete_tasks,
                remaining_work,
                current_time,
                blocked_time,
                completion_times,
                chunks_scheduled_today,
                unschedulable_now
            )
            
            if task_to_schedule is None:
                # No tasks left to schedule
                break
            
            ref_to_task = {t.ref: t for t in incomplete_tasks if t.ref}
            earliest_allowed_start = self._compute_earliest_allowed_start(
                task_to_schedule, current_time, completion_times, ref_to_task
            )
            
            # Determine chunk size based on execution mode
            desired_chunk = self._determine_desired_chunk(
                task_to_schedule,
                remaining_work[id(task_to_schedule)],
                current_time,
                earliest_allowed_start,
                blocked_time,
                chunks_scheduled_today
            )
            
            # Schedule a segment of this chunk
            segment, next_time = self._schedule_task_chunk(
                task=task_to_schedule,
                earliest_start=earliest_allowed_start,
                blocked_time=blocked_time,
                desired_chunk_duration=desired_chunk,
                remaining_duration=remaining_work[id(task_to_schedule)]
            )
            
            if segment:
                start, end = segment
                segments_by_task[id(task_to_schedule)].append((task_to_schedule, start, end))
                segment_duration = end - start
                remaining_work[id(task_to_schedule)] -= segment_duration

                # Track chunk completion
                day_date = start.date()
                chunks_scheduled_today[id(task_to_schedule)][day_date] += 1

                if remaining_work[id(task_to_schedule)] <= timedelta(0):
                    completion_times[id(task_to_schedule)] = end
                
                current_time = next_time
                unschedulable_now.clear()
            else:
                unschedulable_now.add(id(task_to_schedule))
        
        # Convert segments to ScheduledTask objects with proper metadata
        scheduled, conflicts = self._build_scheduled_tasks(segments_by_task)
        
        return scheduled, conflicts

    def _select_next_task(
        self,
        tasks: list[Task],
        remaining_work: dict[str, timedelta],
        current_time: datetime,
        blocked_time: list[TimeBlock],
        completion_times: Optional[dict[str, datetime]] = None,
        chunks_scheduled_today: Optional[dict[str, dict]] = None,
        excluded_task_ids: Optional[set[int]] = None
    ) -> Optional[Task]:
        """
        Select the next task to schedule using urgency-aware logic.
        
        Strategy:
        1. Find tasks with remaining work
        2. Check if dependencies are satisfied
        3. For each task, calculate how urgent it is (time until deadline vs time needed)
        4. Prioritize tasks that:
           - Have all dependencies completed
           - Have deadlines that are becoming critical
           - Can place a valid chunk now under mode rules
           - Can fit without violating other critical deadlines
        5. Fall back to sorted order if no urgency differentiation
        """
        if completion_times is None:
            completion_times = {}
        
        if chunks_scheduled_today is None:
            chunks_scheduled_today = defaultdict(lambda: defaultdict(int))

        if excluded_task_ids is None:
            excluded_task_ids = set()

        ref_to_task = {t.ref: t for t in tasks if t.ref}
        
        candidates = [
            t for t in tasks
            if remaining_work.get(id(t), timedelta(0)) > timedelta(0)
            and id(t) not in excluded_task_ids
        ]
        
        if not candidates:
            return None
        
        def deps_satisfied(task: Task) -> bool:
            if not task.depends_on:
                return True
            for dep_ref in task.depends_on:
                dep_task = ref_to_task.get(dep_ref)
                if not dep_task:
                    continue
                if id(dep_task) not in completion_times:
                    return False
            return True
        
        def can_place_chunk(task: Task) -> bool:
            earliest_start = self._compute_earliest_allowed_start(
                task, current_time, completion_times, ref_to_task
            )
            chunk = self._determine_desired_chunk(
                task,
                remaining_work.get(id(task), timedelta(0)),
                current_time,
                earliest_start,
                blocked_time,
                chunks_scheduled_today
            )
            return chunk > timedelta(0)
        
        available = [
            t for t in candidates 
            if deps_satisfied(t) and can_place_chunk(t)
        ]
        
        if not available:
            return None
        
        urgency_scores = []
        for task in available:
            score = self._calculate_urgency(task, current_time, remaining_work.get(id(task), timedelta(0)), blocked_time)
            urgency_scores.append((score, task))
        
        urgency_scores.sort(key=lambda x: x[0])
        
        for urgency, task in urgency_scores:
            if self._is_safe_to_schedule_chunk(task, current_time, remaining_work, available, blocked_time):
                return task
        
        return urgency_scores[0][1] if urgency_scores else None
    
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
        - Document priority rank (soft nudge -- see PRIORITY_BIAS_STEP_SECONDS)
        """
        if not task.effective_deadline:
            # No deadline - least urgent, use a large number plus duration
            base_score = 1e10 + remaining_duration.total_seconds()
        else:
            # Time until deadline
            time_until_deadline = task.effective_deadline - current_time

            if time_until_deadline <= timedelta(0):
                # Already past deadline - highly urgent
                base_score = -1e9 + time_until_deadline.total_seconds()
            else:
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

        return base_score + self._priority_bias(task)

    def _priority_bias(self, task: Task) -> float:
        """Soft urgency-score nudge from the task's source-document priority.

        Since lower urgency scores are picked first, a lower Task.priority
        number (rank 1 = highest) adds a smaller amount here, making the task
        look more urgent. Unranked (None) tasks are treated as one tier below
        any realistic explicit rank (UNRANKED_PRIORITY_BIAS_RANK), so they're
        always nudged less urgent than a ranked document's tasks, matching
        DocumentConfig.priority's "unset = lowest priority" semantics. This
        only ever shifts ordering among tasks that are otherwise comparably
        urgent (or have no deadline at all) -- it's added after all
        deadline-driven urgency math, and is small enough relative to real
        deadline slack that it cannot make a merely higher-priority document's
        task preempt another task's genuinely critical deadline.
        """
        rank = task.priority if task.priority is not None else UNRANKED_PRIORITY_BIAS_RANK
        return rank * PRIORITY_BIAS_STEP_SECONDS

    def _compute_earliest_allowed_start(
        self,
        task: Task,
        current_time: datetime,
        completion_times: dict[str, datetime],
        ref_to_task: dict[str, Task]
    ) -> datetime:
        """Resolve the earliest a task may start given its dependencies' completion times."""
        earliest_allowed_start = current_time
        for dep_ref in task.depends_on:
            dep_task = ref_to_task.get(dep_ref)
            if dep_task and id(dep_task) in completion_times:
                earliest_allowed_start = max(earliest_allowed_start, completion_times[id(dep_task)])
        return earliest_allowed_start

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
        
        Other tasks whose deadline is already unreachable as of current_time -- i.e. it
        would be missed even with zero further delay -- are excluded from this check.
        They've already lost regardless of what we schedule next, so they shouldn't be
        able to veto scheduling of a task whose deadline is still reachable.
        
        This version reasons about a typical chunk, not the entire remaining task.
        Returns True if safe to schedule, False if it would endanger critical deadlines.
        """
        # Estimate when a typical chunk of this task would finish if scheduled now
        task_remaining = remaining_work.get(id(task), timedelta(0))
        typical_chunk = self._determine_desired_chunk(
            task, task_remaining, current_time, current_time, blocked_time, {}
        )
        estimated_end = self._estimate_completion_time(current_time, typical_chunk, blocked_time)

        # Check each other task with a deadline
        for other_task in all_tasks:
            if other_task is task:
                continue

            if not other_task.effective_deadline:
                continue

            other_remaining = remaining_work.get(id(other_task), timedelta(0))
            if other_remaining <= timedelta(0):
                continue

            # Skip tasks that are already unreachable even with zero further
            # delay -- they're a lost cause independent of this decision, so
            # they shouldn't block scheduling of a task that can still make it.
            time_needed_from_now = self._estimate_duration_with_blocks(
                current_time, other_remaining, blocked_time
            )
            if time_needed_from_now > other_task.effective_deadline - current_time:
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

    def _is_safe_to_schedule_chunk(
        self,
        task: Task,
        current_time: datetime,
        remaining_work: dict[str, timedelta],
        all_tasks: list[Task],
        blocked_time: list[TimeBlock]
    ) -> bool:
        """Alias for _is_safe_to_schedule for clarity in chunk-aware context."""
        return self._is_safe_to_schedule(task, current_time, remaining_work, all_tasks, blocked_time)

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

    def _determine_desired_chunk(
        self,
        task: Task,
        remaining_work: timedelta,
        current_time: datetime,
        earliest_start: datetime,
        blocked_time: list[TimeBlock],
        chunks_scheduled_today: dict[str, dict]
    ) -> timedelta:
        """
        Determine the desired chunk size for a task based on execution mode.
        
        Returns the duration to try to schedule, or timedelta(0) if chunk cap exceeded
        without deadline pressure.
        """
        if remaining_work <= timedelta(0):
            return timedelta(0)
        
        day_date = earliest_start.date()
        chunks_today = chunks_scheduled_today.get(id(task), {}).get(day_date, 0)
        
        if task.execution_mode == "atomic":
            # Atomic: try to schedule entire remaining work as one chunk
            return remaining_work
        
        elif task.execution_mode == "flex":
            # Flex: intelligent chunking with daily cap
            if remaining_work <= WHOLE_TASK_THRESHOLD:
                # Small task: schedule as one chunk
                return remaining_work
            
            # Large task: chunk it
            # Check daily cap
            if chunks_today >= DEFAULT_MAX_CHUNKS_PER_DAY:
                # Cap reached; can schedule smaller chunk if deadline pressure exists
                if task.effective_deadline:
                    time_until_deadline = task.effective_deadline - current_time
                    if time_until_deadline <= timedelta(minutes=120):  # Deadline in 2 hours or less
                        return min(remaining_work, DEFAULT_MIN_CHUNK)
                # No deadline pressure; defer to next day
                return timedelta(0)
            
            # Within daily cap; schedule target chunk
            if remaining_work < DEFAULT_TARGET_CHUNK:
                return remaining_work
            elif remaining_work < DEFAULT_MIN_CHUNK:
                return remaining_work
            else:
                return DEFAULT_TARGET_CHUNK
        
        elif task.execution_mode == "contiguous_preferred":
            # Contiguous preferred: larger chunk first, then fallback
            if remaining_work <= WHOLE_TASK_THRESHOLD:
                return remaining_work
            
            # Check daily cap
            if chunks_today >= DEFAULT_MAX_CHUNKS_PER_DAY:
                if task.effective_deadline:
                    time_until_deadline = task.effective_deadline - current_time
                    if time_until_deadline <= timedelta(minutes=120):
                        return min(remaining_work, DEFAULT_MIN_CHUNK)
                return timedelta(0)
            
            # Try larger chunk (1.5x target)
            larger_chunk = DEFAULT_TARGET_CHUNK + timedelta(minutes=45)
            if remaining_work >= larger_chunk:
                return larger_chunk
            elif remaining_work >= DEFAULT_TARGET_CHUNK:
                return DEFAULT_TARGET_CHUNK
            elif remaining_work >= DEFAULT_MIN_CHUNK:
                return remaining_work
            else:
                return remaining_work
        
        # Fallback (should not reach)
        return remaining_work

    def _schedule_task_chunk(
        self,
        task: Task,
        earliest_start: datetime,
        blocked_time: list[TimeBlock],
        desired_chunk_duration: timedelta,
        remaining_duration: timedelta
    ) -> tuple[Optional[tuple[datetime, datetime]], datetime]:
        """
        Schedule one chunk of a task, using blocked-time-aware segmentation.
        
        This method determines how much work to place (the chunk), then uses
        the existing blocked-time segmentation logic to place it across blocks.
        
        Returns ((start, end), next_available_time) or (None, next_available_time)
        """
        if desired_chunk_duration <= timedelta(0):
            # No chunk to place; skip to next available time after any blocks
            current = earliest_start
            while True:
                next_block = self._find_next_block(current, blocked_time)
                if next_block is None or next_block.start >= current:
                    break
                current = next_block.end
            return None, current
        
        # How much work can we actually place?
        actual_chunk = min(desired_chunk_duration, remaining_duration)
        
        # Use existing segment scheduling to handle blocked time
        return self._schedule_task_segment(
            task=task,
            earliest_start=earliest_start,
            blocked_time=blocked_time,
            remaining_duration=actual_chunk
        )

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

            # Merge segments that immediately follow one another with no
            # actual gap in between. The chunking policy for flex/
            # contiguous_preferred modes can pick the same task again on
            # the very next iteration with no interruption in between; that's
            # not a real split and shouldn't be shown as separate "parts".
            merged_segments = [segments[0]]
            for seg_task, seg_start, seg_end in segments[1:]:
                prev_task, prev_start, prev_end = merged_segments[-1]
                if seg_start <= prev_end:
                    merged_segments[-1] = (prev_task, prev_start, max(prev_end, seg_end))
                else:
                    merged_segments.append((seg_task, seg_start, seg_end))
            segments = merged_segments

            task = segments[0][0]
            final_end = segments[-1][2]
            is_multi_segment = len(segments) > 1
            total_scheduled = sum((end - start for _, start, end in segments), timedelta())
            is_partial = total_scheduled < task.estimated_duration

            # Check violations based on final end time
            violates_user = self._violates_deadline(final_end, task.deadline_user)
            violates_external = self._violates_deadline(final_end, task.deadline_external)
            
            # Generate conflict messages if violations exist
            # Build source context (document title + tab name)
            source_parts = []
            if task.document_title:
                source_parts.append(f"[{task.document_title}]")
            if task.section:
                source_parts.append(f"• {task.section}")
            source_context = " ".join(source_parts) if source_parts else ""
            
            if violates_user and task.deadline_user:
                conflict_msg = (
                    f"Task '{task.title}' ends at {final_end.strftime('%Y-%m-%d %H:%M')} "
                    f"but user deadline is {task.deadline_user.strftime('%Y-%m-%d %H:%M')}"
                )
                if source_context:
                    conflict_msg = f"{source_context} — {conflict_msg}"
                conflicts.append(conflict_msg)
            
            if violates_external and task.deadline_external:
                conflict_msg = (
                    f"Task '{task.title}' ends at {final_end.strftime('%Y-%m-%d %H:%M')} "
                    f"but external deadline is {task.deadline_external.strftime('%Y-%m-%d %H:%M')}"
                )
                if source_context:
                    conflict_msg = f"{source_context} — {conflict_msg}"
                conflicts.append(conflict_msg)
            
            # Create ScheduledTask for each segment
            for idx, (_, start, end) in enumerate(segments, start=1):
                try:
                    scheduled_task = ScheduledTask(
                        task=task,
                        start=start,
                        end=end,
                        violates_deadline_user=violates_user,
                        violates_deadline_external=violates_external,
                        is_segment=is_multi_segment,
                        segment_index=idx if is_multi_segment else None,
                        total_segments=len(segments) if is_multi_segment else None,
                        is_partial=is_partial,
                    )
                except ValidationError as e:
                    raise ValueError(
                        f"Failed to schedule task '{task.title}' (id={task.id}): {e}"
                    ) from e
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

        # Build source context (document title + tab name)
        source_parts = []
        if task.document_title:
            source_parts.append(task.document_title)
        if task.section:
            source_parts.append(task.section)
        
        source_context = " • ".join(source_parts) if source_parts else ""

        if scheduled_task.violates_deadline_user:
            conflict_msg = (
                f"Task '{task.title}' ends at {scheduled_task.end.strftime('%Y-%m-%d %H:%M')} "
                f"but user deadline is {task.deadline_user.strftime('%Y-%m-%d %H:%M')}"
            )
            if source_context:
                conflict_msg = f"{source_context} — {conflict_msg}"
            conflicts.append(conflict_msg)

        if scheduled_task.violates_deadline_external:
            conflict_msg = (
                f"Task '{task.title}' ends at {scheduled_task.end.strftime('%Y-%m-%d %H:%M')} "
                f"but external deadline is {task.deadline_external.strftime('%Y-%m-%d %H:%M')}"
            )
            if source_context:
                conflict_msg = f"{source_context} — {conflict_msg}"
            conflicts.append(conflict_msg)

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
