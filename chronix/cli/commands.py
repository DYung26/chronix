"""Command implementations for the chronix CLI."""

from datetime import datetime, timezone, timedelta, date, time
from typing import Optional
from pathlib import Path
import json
from zoneinfo import ZoneInfo

from chronix.integrations.google_docs.client import GoogleDocsClient
from chronix.integrations.google_docs.parser import GoogleDocsParser
from chronix.core.todo import TodoDeriver, parse_document_meetings, EXCLUDED_TAB_TITLES
from chronix.core.aggregation import ProjectTodoList, TaskAggregator
from chronix.core.scheduler import SchedulingEngine, create_time_block
from chronix.core.models import Task, DaySchedule
from chronix.cli.formatting import (
    console,
    format_duration,
    print_sync_summary,
    print_schedule_header,
    print_timeline_segment,
    print_timeline_footer,
    print_dual_timeline,
    print_conflicts,
    print_task_details,
    print_task_position,
    print_document_overview,
    print_task_table,
    print_page_footer,
    print_error,
    print_warning,
    print_success,
    print_info,
)


def parse_clock_time(value: str) -> time:
    """
    Parse HH:MM time format from string.
    
    Args:
        value: Time string in HH:MM 24-hour format
        
    Returns:
        time object
        
    Raises:
        ValueError: If format is invalid or values are out of range
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM in 24-hour format.")
    
    parts = value.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM in 24-hour format.")
    
    # Enforce exactly 2 digits for both hour and minute
    if len(parts[0]) != 2 or len(parts[1]) != 2:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM in 24-hour format.")
    
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid time '{value}'. Expected HH:MM in 24-hour format.")
    
    if hour < 0 or hour > 23:
        raise ValueError(f"Invalid time '{value}'. Hour must be between 00 and 23.")
    
    if minute < 0 or minute > 59:
        raise ValueError(f"Invalid time '{value}'. Minute must be between 00 and 59.")
    
    return time(hour=hour, minute=minute)


def resolve_today_start_datetime(
    time_override: Optional[str],
    today_date: date,
    tz: ZoneInfo
) -> datetime:
    """
    Resolve the effective start datetime for today's schedule.
    
    Args:
        time_override: Optional HH:MM time string, or None for current time
        today_date: Today's date in the configured timezone
        tz: Timezone to use
        
    Returns:
        timezone-aware datetime for the schedule start point
        
    Raises:
        ValueError: If time_override format is invalid
    """
    if time_override is None:
        now = datetime.now(tz)
        return now
    
    parsed_time = parse_clock_time(time_override)
    return datetime.combine(today_date, parsed_time, tzinfo=tz)


def _generate_today_schedule(time_override: Optional[str] = None) -> tuple[DaySchedule, datetime, datetime, list]:
    """
    Generate today's schedule (shared logic for `today` and `calendar` commands).

    Returns:
        Tuple of (day_schedule, work_start, work_end, paused_blocks). paused_blocks
        are the recurring config blocks (sleep/break/meeting) that fall today but
        are paused for this session via `blocks pause` -- they no longer occupy
        any time (tasks may be scheduled through them), but are returned so the
        caller can still show them, greyed out, at their original time in the
        timeline for visibility.

    Raises:
        RuntimeError: If no projects loaded
        ValueError: If time override format is invalid
    """
    day_schedule, secondary_schedule, work_start, work_end, paused_blocks = _generate_today_schedules(time_override)
    return day_schedule, work_start, work_end, paused_blocks


def _generate_today_schedules(
    time_override: Optional[str] = None,
) -> tuple[DaySchedule, DaySchedule, datetime, datetime, list]:
    """
    Generate today's primary and secondary track schedules.

    The two tracks are scheduled independently: tasks are partitioned by
    resolve_track (see chronix.core.tracks) into a primary pool and a
    secondary pool, then each pool is run through its own SchedulingEngine
    pass over the *same* work window and blocked time (sleep, meetings,
    off-hours). Neither pass is aware of the other's placements -- a
    secondary-track task can and will be scheduled at a time that overlaps a
    primary-track task, since the whole point of the secondary track is
    "things you could plausibly run alongside whatever the primary track has
    you doing" (e.g. in a second terminal pane), not a second set of hands
    fighting the primary track for the same minutes.

    Returns:
        Tuple of (primary_schedule, secondary_schedule, work_start, work_end,
        paused_blocks). paused_blocks are the recurring config blocks
        (sleep/break/meeting) that fall today but are paused for this session
        via `blocks pause` -- they no longer occupy any time in either track,
        but are returned so the caller can still show them, greyed out, at
        their original time in the timeline for visibility.

    Raises:
        RuntimeError: If no projects loaded
        ValueError: If time override format is invalid
    """
    if not _context.projects:
        raise RuntimeError("No projects loaded. Run 'sync' first.")
    
    from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window, get_work_windows
    from chronix.core.tracks import partition_by_track
    
    config = _context.config or ChronixConfig.load_or_default()
    tz = ZoneInfo(config.scheduling.timezone)
    
    # Validate time format early if provided
    if time_override is not None:
        try:
            parse_clock_time(time_override)
        except ValueError as e:
            raise ValueError(str(e))
    
    # Aggregate all tasks
    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)
    _warn_on_conflicts(aggregator)
    task_pool = aggregator.get_task_pool(aggregated_tasks)
    incomplete_tasks = [t for t in task_pool if not t.completed]
    primary_tasks, secondary_tasks = partition_by_track(incomplete_tasks)
    
    # Get today's date and time
    now = datetime.now(tz)
    today = now.date()

    # Get all work windows from config
    all_windows = get_work_windows(config, today)
    work_start = all_windows[0][0]
    work_end = all_windows[-1][1]

    # Determine effective start time
    if time_override is not None:
        work_start = resolve_today_start_datetime(time_override, today, tz)
    else:
        if now > work_start:
            work_start = now

    # Limit work_end to end of day
    end_of_today = datetime.combine(
        today,
        datetime.max.time(),
        tzinfo=tz
    ).replace(hour=23, minute=59, second=59)

    if work_end > end_of_today:
        work_end = end_of_today

    # Get blocked time from config, honoring any session-level pauses. Blocks
    # that are paused this session are split off separately: they no longer
    # occupy any time, but are still returned so the caller can display them
    # (greyed out) at their original slot.
    all_today_config_blocks = config_to_time_blocks(config, today)
    blocked_time = [b for b in all_today_config_blocks if _is_block_active(b)]
    paused_blocks = [b for b in all_today_config_blocks if not _is_block_active(b)]

    # Add gap blocks between work windows so the scheduler skips non-work periods
    for i in range(len(all_windows) - 1):
        gap_start = all_windows[i][1]
        gap_end = all_windows[i + 1][0]
        if gap_start < gap_end:
            from chronix.core.models import TimeBlock
            blocked_time.append(TimeBlock(start=gap_start, end=gap_end, kind="blocked", label="off hours"))

    # Add ad-hoc meetings as blocked time
    for meeting in _context.ad_hoc_meetings:
        if meeting.start.date() == today:
            blocked_time.append(meeting.to_time_block())
    
    # Schedule tasks -- both tracks share the exact same blocked time and
    # work window, and are scheduled with two independent engine runs.
    filtered_blocked = [
        block for block in blocked_time
        if block.start < work_end and block.end > work_start
    ]
    
    scheduler = SchedulingEngine()

    def _run_and_filter_to_today(tasks: list[Task]) -> DaySchedule:
        result = scheduler.schedule_tasks(
            tasks=tasks,
            start_time=work_start,
            blocked_time=filtered_blocked
        )
        today_scheduled_tasks = [
            st for st in result.scheduled_tasks
            if st.start.date() == today
        ]
        return DaySchedule(
            date=result.date,
            scheduled_tasks=today_scheduled_tasks,
            blocked_time=result.blocked_time,
            conflicts=result.conflicts,
        )

    primary_schedule = _run_and_filter_to_today(primary_tasks)
    secondary_schedule = _run_and_filter_to_today(secondary_tasks)

    # Only surface paused blocks that actually overlap the displayed window,
    # same as the filtering already applied to real blocked time above.
    paused_blocks = [
        b for b in paused_blocks
        if b.start < work_end and b.end > work_start
    ]

    return primary_schedule, secondary_schedule, work_start, work_end, paused_blocks


class ChronixContext:
    """Shared context for chronix commands."""

    def __init__(self):
        self.projects: list[ProjectTodoList] = []
        self.ad_hoc_meetings: list = []
        self.last_sync: Optional[datetime] = None
        self.google_client: Optional[GoogleDocsClient] = None
        self.config: Optional['ChronixConfig'] = None
        # Recurring config blocks (sleep/break/meeting) the user has paused
        # for this session only, keyed by their lowercased label (or kind if
        # unlabeled). Never written back to config.toml.
        self.disabled_blocks: set[str] = set()

    def _ensure_google_client(self) -> GoogleDocsClient:
        """Lazy initialize Google Docs client."""
        if self.google_client is None:
            self.google_client = GoogleDocsClient()
        return self.google_client


# Global context instance
_context = ChronixContext()


def _block_config_key(block_config) -> str:
    """Canonical session-pause key for a configured recurring time block."""
    return (block_config.label or block_config.kind).strip().lower()


def _is_block_active(time_block) -> bool:
    """Whether a domain TimeBlock built from config should still apply this session."""
    key = (time_block.label or time_block.kind).strip().lower()
    return key not in _context.disabled_blocks


def _resolve_project_token(token: str, config: 'ChronixConfig') -> Optional[str]:
    """
    Resolve a token (project name) to the project's canonical name.

    Returns the project name if the token matches a configured project,
    else None.
    """
    project = config.find_project(token)
    return project.name if project is not None else None


def _configured_tz(config: 'ChronixConfig') -> ZoneInfo:
    """The app's configured scheduling timezone, used to read/write naive
    metadata datetimes in Google Docs as-is (see chronix.core.metadata)."""
    return ZoneInfo(config.scheduling.timezone)


def _warn_on_conflicts(aggregator: TaskAggregator) -> None:
    """Print a warning if the most recent aggregate() call found sync conflicts.

    A conflict is a task id that appeared from two or more sources with
    disagreeing content (see chronix.core.aggregation._tasks_conflict).
    Conflicted tasks are excluded from get_task_pool's scheduling output
    entirely rather than picked between arbitrarily or double-scheduled, so
    every scheduling command must call this after aggregate() to make that
    exclusion visible -- otherwise a real task would simply, silently stop
    appearing in `today`/`schedule` with no indication why.
    """
    conflicts = aggregator.get_conflicts()
    if not conflicts:
        return
    count = len(conflicts)
    task_word = "task" if count == 1 else "tasks"
    ids = ", ".join(c.task_id for c in conflicts)
    print_warning(
        f"{count} {task_word} disagree across sources and were excluded from scheduling: {ids}"
    )
    console.print("[dim]Run 'conflicts' to see and resolve them.[/dim]")


def sync_command(args: list[str]) -> int:
    """
    Sync command: Fetch and parse configured projects (each may have a Google Docs
    source, a local file source, or both).

    Usage: sync [project ...]

    With no argument: syncs every configured project (continues on
    project-level failures). With one or more project name tokens: syncs
    only those projects (all must be configured). A project with two
    sources has both fetched together, so aggregation can merge/dedupe them
    correctly (see chronix.core.aggregation.TaskAggregator.aggregate).
    """
    try:
        tokens = args if args else None

        console.print("[dim]Starting sync...[/dim]")

        # Load configuration (global failure)
        from chronix.config import ChronixConfig

        try:
            config = ChronixConfig.load_or_default()
        except Exception as e:
            print_error(f"Failed to load configuration: {e}")
            console.print("Run [cyan]chronix config init[/cyan] to create a default configuration.")
            return 1

        if not config.projects:
            print_warning("No projects configured in your config file.")
            console.print(f"Edit [cyan]{ChronixConfig.get_default_path()}[/cyan] and add projects to sync.")
            return 1

        # If specific tokens were requested, resolve and validate all before fetching any
        if tokens:
            unknown = [t for t in tokens if config.find_project(t) is None]
            if unknown:
                for token in unknown:
                    print_error(f"Unknown project '{token}'")
                console.print("Configured projects:")
                for project in config.projects:
                    console.print(f"  [cyan]{project.label()}[/cyan]")
                return 1
            # Resolve tokens to canonical project names (deduplicated, preserving order)
            seen: set[str] = set()
            project_names: list[str] = []
            for t in tokens:
                resolved = config.find_project(t).name
                if resolved not in seen:
                    seen.add(resolved)
                    project_names.append(resolved)
        else:
            project_names = [p.name for p in config.projects]

        sources_to_sync = [s for s in config.all_sources() if s.project_name in project_names]

        # Authenticate the Google Docs client only if at least one Docs source
        # is actually in scope -- local-files-only syncs need no auth step.
        needs_google_auth = any(s.type == "google_docs" for s in sources_to_sync)
        if needs_google_auth:
            client = _context._ensure_google_client()
            console.print("[dim]Authenticating with Google Docs...[/dim]")
            try:
                if not client.authenticate():
                    print_error("Authentication failed. Please check your credentials.")
                    console.print("[dim]Hint: Ensure OAuth credentials are properly configured.[/dim]")
                    return 1
            except Exception as auth_error:
                print_error(f"Authentication error: {auth_error}")
                return 1
            console.print("[dim]✓ Authenticated successfully[/dim]")

        # Sync each source with retry logic
        from chronix.cli.sync_helpers import _sync_single_source_with_retries
        from chronix.integrations.factory import get_client

        tz = _configured_tz(config)
        projects = []
        all_meetings = []
        results = []

        for source in sources_to_sync:
            source_client = _context._ensure_google_client() if source.type == "google_docs" else get_client(source)
            result, project, meetings = _sync_single_source_with_retries(source, source_client, tz=tz)
            results.append(result)

            if result.outcome.value == "success":
                projects.append(project)
                all_meetings.extend(meetings)

        # Update context: merge or replace, keyed by project_id (a project's
        # two sources both carry the same project_id, so replacing "this
        # project's entries" removes both old ProjectTodoLists together
        # rather than leaving a stale one from the other source behind).
        if tokens:
            # Partial sync: merge into existing context
            if _context.projects:
                synced_project_ids = {p.project_context.project_id for p in projects}
                updated_projects = [
                    p for p in _context.projects
                    if p.project_context.project_id not in synced_project_ids
                ]
                updated_projects.extend(projects)
                _context.projects = updated_projects
                _context.ad_hoc_meetings.extend(all_meetings)
            else:
                _context.projects = projects
                _context.ad_hoc_meetings = all_meetings
                print_warning("Synced specific projects with no prior context.")
                console.print("[dim]For complete task aggregation across all projects, run:[/dim]")
                console.print("[cyan]  chronix sync[/cyan]")
        else:
            # Full sync: replace context entirely
            _context.projects = projects
            _context.ad_hoc_meetings = all_meetings

        _context.last_sync = datetime.now(timezone.utc)
        _context.config = config

        # Summary
        total_tasks = sum(len(p.tasks) for p in projects)
        incomplete_tasks = sum(
            len([t for t in p.tasks if not t.completed]) 
            for p in projects
        )
        completed_tasks = sum(
            len([t for t in p.tasks if t.completed]) 
            for p in projects
        )

        print_sync_summary(
            num_projects=len(project_names),
            total_tasks=total_tasks,
            incomplete_tasks=incomplete_tasks,
            completed_tasks=completed_tasks,
            sync_results=results
        )

        return 0

    except Exception as e:
        print_error(f"Sync failed: {e}")
        return 1


def today_command(args: list[str]) -> int:
    """
    Today command: Display today's scheduled tasks.

    Usage: today [HH:MM] [--split]
    
    Optional HH:MM argument specifies the start time for scheduling today.
    If not provided, uses current time. Times are in 24-hour format.

    --split shows the primary and secondary (parallel) tracks side by side
    instead of only the primary track. Falls back to a stacked rendering
    automatically on narrow terminals.
    """
    try:
        split = "--split" in args
        args = [a for a in args if a != "--split"]

        # Validate arguments
        if len(args) > 1:
            print_error(f"today command takes at most 1 argument, got {len(args)}")
            return 1
        
        time_override = args[0] if args else None
        
        console.print("[dim]Generating today's schedule...[/dim]")
        
        if split:
            primary_schedule, secondary_schedule, work_start, work_end, paused_blocks = _generate_today_schedules(time_override)

            print_schedule_header(primary_schedule.date, work_start, work_end, str(work_start.tzinfo))
            print_dual_timeline(primary_schedule, secondary_schedule, work_start, work_end, paused_blocks=paused_blocks)

            all_conflicts = primary_schedule.conflicts + secondary_schedule.conflicts
            if all_conflicts:
                print_conflicts(all_conflicts)

            total_duration = sum(
                (st.end - st.start for st in primary_schedule.scheduled_tasks + secondary_schedule.scheduled_tasks),
                timedelta()
            )
            print_timeline_footer(
                total_duration=total_duration,
                num_scheduled=len(primary_schedule.scheduled_tasks) + len(secondary_schedule.scheduled_tasks),
                num_conflicts=len(all_conflicts)
            )
            return 0

        day_schedule, work_start, work_end, paused_blocks = _generate_today_schedule(time_override)
        
        # Display schedule
        print_schedule_header(day_schedule.date, work_start, work_end, str(work_start.tzinfo))
        
        _display_continuous_timeline(day_schedule, work_start, work_end, paused_blocks=paused_blocks)
        
        # Show conflicts
        if day_schedule.conflicts:
            print_conflicts(day_schedule.conflicts)
        
        # Summary
        total_duration = sum(
            (st.end - st.start for st in day_schedule.scheduled_tasks),
            timedelta()
        )
        
        print_timeline_footer(
            total_duration=total_duration,
            num_scheduled=len(day_schedule.scheduled_tasks),
            num_conflicts=len(day_schedule.conflicts)
        )
        
        return 0
    
    except (RuntimeError, ValueError) as e:
        print_error(f"Failed to generate schedule: {str(e)}")
        return 1
    except Exception as e:
        print_error(f"Failed to generate schedule: {e}")
        import traceback
        traceback.print_exc()
        return 1
def calendar_command(args: list[str]) -> int:
    """
    Calendar command: Sync today's schedule to Google Calendar.

    Usage: calendar [HH:MM] [--force] [--split]
    
    Optional HH:MM argument specifies the start time for scheduling today.
    --force flag allows overwriting conflicting non-Chronix calendar events.

    Without --split, only the primary track is synced and displayed (same
    as today's plain `today`). With --split, the secondary track is synced
    too -- as a distinctly colored (yellow/Banana) set of events, since they
    can legitimately overlap primary-track events in time -- and both
    tracks are displayed side by side afterward (same as `today --split`).
    """
    try:
        # Parse arguments
        time_override = None
        force = False
        split = False
        
        for arg in args:
            if arg == '--force':
                force = True
            elif arg == '--split':
                split = True
            elif arg.startswith('--'):
                print_error(f"Unknown flag: {arg}")
                return 1
            else:
                if time_override is not None:
                    print_error(f"calendar command takes at most 1 time argument")
                    return 1
                time_override = arg
        
        console.print("[dim]Generating and syncing today's schedule to Google Calendar...[/dim]")
        
        primary_schedule, secondary_schedule, work_start, work_end, paused_blocks = _generate_today_schedules(time_override)
        
        # Sync to Google Calendar -- primary track only unless --split was
        # given, in which case the secondary track is synced too (as
        # distinctly colored events -- see CalendarSyncService.sync's
        # `track` parameter). Each track is synced independently, so a
        # secondary-track task placed at the same time as a primary-track
        # one is not treated as a conflict with itself; reconciliation of
        # existing Chronix events is also scoped per track, so syncing one
        # track never touches or deletes the other's already-synced events.
        from chronix.integrations.google_calendar import CalendarSyncService
        sync_service = CalendarSyncService()
        
        primary_result = sync_service.sync(
            day_schedule=primary_schedule,
            sync_start=work_start,
            sync_end=work_end,
            force=force,
            track="primary",
        )

        if not primary_result.success:
            if primary_result.conflicts:
                print_error("Calendar sync failed due to conflicting events:")
                for conflict in primary_result.conflicts:
                    print_error(f"  - {conflict.calendar_event_title} ({conflict.calendar_event_start} - {conflict.calendar_event_end})")
                    print_error(f"    conflicts with {conflict.chronix_task_title}")
                print_info("Rerun with --force to overwrite, or resolve conflicts manually.")
            else:
                print_error(f"Calendar sync failed: {primary_result.error_message}")
            return 1

        secondary_result = None
        if split:
            secondary_result = sync_service.sync(
                day_schedule=secondary_schedule,
                sync_start=work_start,
                sync_end=work_end,
                force=force,
                track="secondary",
            )

            if not secondary_result.success:
                if secondary_result.conflicts:
                    print_error("Calendar sync failed due to conflicting events (secondary track):")
                    for conflict in secondary_result.conflicts:
                        print_error(f"  - {conflict.calendar_event_title} ({conflict.calendar_event_start} - {conflict.calendar_event_end})")
                        print_error(f"    conflicts with {conflict.chronix_task_title}")
                    print_info("Rerun with --force to overwrite, or resolve conflicts manually.")
                else:
                    print_error(f"Calendar sync failed (secondary track): {secondary_result.error_message}")
                return 1
        
        # Print sync summary (combined across both tracks if --split was used)
        created = primary_result.created_count + (secondary_result.created_count if secondary_result else 0)
        updated = primary_result.updated_count + (secondary_result.updated_count if secondary_result else 0)
        deleted = primary_result.deleted_count + (secondary_result.deleted_count if secondary_result else 0)
        shortened = primary_result.shortened_count + (secondary_result.shortened_count if secondary_result else 0)

        print_success(f"Calendar sync completed:")
        print_info(f"  Created: {created} events")
        print_info(f"  Updated: {updated} events")
        print_info(f"  Deleted: {deleted} events")
        print_info(f"  Shortened: {shortened} events")
        print()
        
        # Display schedule -- mirrors `today`/`today --split` exactly, using
        # whatever was actually synced above (secondary_schedule only
        # matters here when --split triggered its sync).
        print_schedule_header(primary_schedule.date, work_start, work_end, str(work_start.tzinfo))

        if split:
            all_conflicts = primary_schedule.conflicts + secondary_schedule.conflicts
            print_dual_timeline(primary_schedule, secondary_schedule, work_start, work_end, paused_blocks=paused_blocks)

            if all_conflicts:
                print_conflicts(all_conflicts)

            total_duration = sum(
                (st.end - st.start for st in primary_schedule.scheduled_tasks + secondary_schedule.scheduled_tasks),
                timedelta()
            )
            print_timeline_footer(
                total_duration=total_duration,
                num_scheduled=len(primary_schedule.scheduled_tasks) + len(secondary_schedule.scheduled_tasks),
                num_conflicts=len(all_conflicts)
            )
            return 0

        _display_continuous_timeline(primary_schedule, work_start, work_end, paused_blocks=paused_blocks)
        
        # Show conflicts
        if primary_schedule.conflicts:
            print_conflicts(primary_schedule.conflicts)
        
        # Summary
        total_duration = sum(
            (st.end - st.start for st in primary_schedule.scheduled_tasks),
            timedelta()
        )
        
        print_timeline_footer(
            total_duration=total_duration,
            num_scheduled=len(primary_schedule.scheduled_tasks),
            num_conflicts=len(primary_schedule.conflicts)
        )
        
        return 0
    
    except (RuntimeError, ValueError) as e:
        print_error(f"Failed to sync calendar: {str(e)}")
        return 1
    except Exception as e:
        print_error(f"Failed to sync calendar: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _parse_day_range(token: str) -> Optional[tuple[int, int]]:
    """Parse a '<start>-<end>' token into 1-indexed, inclusive day offsets.

    Returns None if the token has no hyphen (i.e. isn't a range at all).
    Raises ValueError if it has a hyphen but is otherwise malformed.
    """
    if "-" not in token:
        return None
    parts = token.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid day range '{token}'. Use format: <start>-<end>, e.g. 3-5")
    try:
        start_day, end_day = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"Invalid day range '{token}'. Use format: <start>-<end>, e.g. 3-5")
    if start_day < 1 or end_day < 1:
        raise ValueError("Day range values must be positive")
    if start_day > end_day:
        raise ValueError(f"Invalid day range '{token}': start day must not exceed end day")
    return start_day, end_day


def schedule_command(args: list[str]) -> int:
    """
    Schedule command: Display schedule for multiple days.

    Usage: schedule [days] [--split]
           schedule <start>-<end>
           schedule from <day> [to <count>]

    schedule [days]: schedules all tasks from today; days limits how many
    days are shown (unlimited if omitted).

    schedule <start>-<end>: runs the same continuous simulation as
    `schedule <end>`, since later days depend on what real depletion
    happened on earlier days, but only displays days <start>..<end>
    (1-indexed, today is day 1), renumbering the task list for that range.

    schedule from <day> [to <count>]: forecasts what the schedule would
    look like if day <day> were the starting point, using the full current
    backlog as-is rather than simulating depletion of the days before it.
    <count>, if given, limits how many forecasted days are shown.

    --split shows the primary and secondary (parallel) tracks side by side
    for each day instead of only the primary track. Only supported with the
    plain `schedule [days]` form; combine with a day range or forecast by
    running `today --split` for a single day instead.
    """
    usage = "Usage: schedule [days] [--split] | schedule <start>-<end> | schedule from <day> [to <count>]"

    try:
        if not _context.projects:
            print_warning("No projects loaded. Run 'sync' first.")
            return 1

        split = "--split" in args
        args = [a for a in args if a != "--split"]

        num_days: int | None = None
        range_start: Optional[int] = None
        range_end: Optional[int] = None
        forecast_day: Optional[int] = None
        forecast_count: Optional[int] = None

        if args and args[0] == "from":
            if split:
                print_error("--split is not supported with 'schedule from'. Use 'today --split' for a single day.")
                return 1
            if len(args) not in (2, 4):
                print_error(usage)
                return 1
            try:
                forecast_day = int(args[1])
            except ValueError:
                print_error(f"Invalid day: '{args[1]}'")
                return 1
            if forecast_day < 1:
                print_error("Day must be positive")
                return 1
            if len(args) == 4:
                if args[2] != "to":
                    print_error(usage)
                    return 1
                try:
                    forecast_count = int(args[3])
                except ValueError:
                    print_error(f"Invalid day count: '{args[3]}'")
                    return 1
                if forecast_count < 1:
                    print_error("Day count must be positive")
                    return 1
        elif args:
            if len(args) > 1:
                print_error(usage)
                return 1
            try:
                day_range = _parse_day_range(args[0])
            except ValueError as e:
                print_error(str(e))
                return 1
            if day_range is not None:
                if split:
                    print_error("--split is not supported with a day range. Use 'today --split' for a single day.")
                    return 1
                range_start, range_end = day_range
                num_days = range_end
            else:
                try:
                    num_days = int(args[0])
                except ValueError:
                    print_error(f"Invalid number of days: {args[0]}")
                    return 1
                if num_days < 1:
                    print_error("Number of days must be positive")
                    return 1

        if forecast_day is not None:
            console.print(f"[dim]Generating forecast from day {forecast_day}...[/dim]")
        elif num_days is None:
            console.print(f"[dim]Generating unlimited schedule...[/dim]")
        else:
            console.print(f"[dim]Generating {num_days}-day schedule...[/dim]")

        # Load configuration
        from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window, get_work_windows
        from zoneinfo import ZoneInfo

        config = _context.config or ChronixConfig.load_or_default()
        tz = ZoneInfo(config.scheduling.timezone)

        # Aggregate all tasks
        aggregator = TaskAggregator()
        aggregated_tasks = aggregator.aggregate(_context.projects)
        _warn_on_conflicts(aggregator)
        task_pool = aggregator.get_task_pool(aggregated_tasks)

        # Filter incomplete tasks only
        incomplete_tasks = [t for t in task_pool if not t.completed]

        if split:
            from chronix.core.tracks import partition_by_track
            primary_tasks, secondary_tasks = partition_by_track(incomplete_tasks)

        # Get current time
        now = datetime.now(tz)

        if forecast_day is not None:
            # Forecast mode ignores depletion on days before forecast_day:
            # start the simulation directly at that day's work window using
            # the full current backlog, rather than running days 1..N-1 first.
            forecast_date = now.date() + timedelta(days=forecast_day - 1)
            forecast_work_start, _ = get_work_window(config, forecast_date)
            if forecast_day == 1 and now > forecast_work_start:
                start_time = now
            else:
                start_time = forecast_work_start
            continuous_num_days = forecast_count
        else:
            # Adjust start time if we're past work start today
            first_day_start, _ = get_work_window(config, now.date())
            start_time = max(now, first_day_start)
            continuous_num_days = num_days

        # Schedule continuously across all days
        scheduler = SchedulingEngine()
        
        def get_daily_blocked_time(day_date: date) -> list:
            """Get blocked time for a specific day."""
            blocked = [
                b for b in config_to_time_blocks(config, day_date)
                if _is_block_active(b)
            ]
            # Add gap blocks between work windows
            day_windows = get_work_windows(config, day_date)
            for i in range(len(day_windows) - 1):
                gap_start = day_windows[i][1]
                gap_end = day_windows[i + 1][0]
                if gap_start < gap_end:
                    from chronix.core.models import TimeBlock
                    blocked.append(TimeBlock(start=gap_start, end=gap_end, kind="blocked", label="off hours"))
            # Add ad-hoc meetings for this day
            for meeting in _context.ad_hoc_meetings:
                if meeting.start.date() == day_date:
                    blocked.append(meeting.to_time_block())
            return blocked

        def get_daily_paused_blocks(day_date: date) -> list:
            """Get this session's paused recurring blocks for a specific day, for display only."""
            return [
                b for b in config_to_time_blocks(config, day_date)
                if not _is_block_active(b)
            ]

        if split:
            primary_schedules_by_day = scheduler.schedule_continuous(
                tasks=primary_tasks,
                start_time=start_time,
                num_days=continuous_num_days,
                daily_blocked_time_fn=get_daily_blocked_time
            )
            secondary_schedules_by_day = scheduler.schedule_continuous(
                tasks=secondary_tasks,
                start_time=start_time,
                num_days=continuous_num_days,
                daily_blocked_time_fn=get_daily_blocked_time
            )

            all_conflicts = []
            first_displayed = True
            all_days = sorted(set(primary_schedules_by_day.keys()) | set(secondary_schedules_by_day.keys()))
            for day_offset, day_date in enumerate(all_days):
                if num_days is not None and day_offset >= num_days:
                    break

                work_start, work_end = get_work_window(config, day_date)
                if day_offset == 0 and now > work_start:
                    work_start = now

                if not first_displayed:
                    console.print("\n" + "─" * 60 + "\n")
                first_displayed = False

                day_paused_blocks = [
                    b for b in get_daily_paused_blocks(day_date)
                    if b.start < work_end and b.end > work_start
                ]

                from chronix.core.models import DaySchedule as _DaySchedule
                primary_day = primary_schedules_by_day.get(day_date) or _DaySchedule(date=day_date, scheduled_tasks=[], blocked_time=[], conflicts=[])
                secondary_day = secondary_schedules_by_day.get(day_date) or _DaySchedule(date=day_date, scheduled_tasks=[], blocked_time=[], conflicts=[])

                print_schedule_header(day_date, work_start, work_end, config.scheduling.timezone)
                print_dual_timeline(primary_day, secondary_day, work_start, work_end, paused_blocks=day_paused_blocks)

                if primary_day.conflicts:
                    all_conflicts.extend(primary_day.conflicts)
                if secondary_day.conflicts:
                    all_conflicts.extend(secondary_day.conflicts)

            if all_conflicts:
                console.print("\n" + "─" * 60 + "\n")
                print_conflicts(all_conflicts)

            return 0

        schedules_by_day = scheduler.schedule_continuous(
            tasks=incomplete_tasks,
            start_time=start_time,
            num_days=continuous_num_days,
            daily_blocked_time_fn=get_daily_blocked_time
        )
        
        # Display each day's schedule with continuous task numbering
        all_conflicts = []
        task_counter = 1  # Global task counter across displayed days (starts at 1)
        first_displayed = True
        for day_offset, day_date in enumerate(sorted(schedules_by_day.keys())):
            day_number = day_offset + 1  # 1-indexed offset from the simulation's start

            if forecast_day is not None:
                if forecast_count is not None and day_offset >= forecast_count:
                    break
            else:
                if num_days is not None and day_offset >= num_days:
                    break
                if range_start is not None and day_number < range_start:
                    continue
            
            if day_date not in schedules_by_day:
                continue
            
            day_schedule = schedules_by_day[day_date]
            
            # Get work window for display
            work_start, work_end = get_work_window(config, day_date)
            if day_offset == 0 and now > work_start:
                work_start = now
            
            # Display separator between days
            if not first_displayed:
                console.print("\n" + "─" * 60 + "\n")
            first_displayed = False
            
            day_paused_blocks = [
                b for b in get_daily_paused_blocks(day_date)
                if b.start < work_end and b.end > work_start
            ]

            print_schedule_header(day_schedule.date, work_start, work_end, config.scheduling.timezone)
            task_counter = _display_continuous_timeline(
                day_schedule, work_start, work_end,
                start_index=task_counter, paused_blocks=day_paused_blocks
            )
            
            # Collect conflicts
            if day_schedule.conflicts:
                all_conflicts.extend(day_schedule.conflicts)
        
        # Show all conflicts at the end
        if all_conflicts:
            console.print("\n" + "─" * 60 + "\n")
            print_conflicts(all_conflicts)
        
        return 0
    
    except Exception as e:
        print_error(f"Failed to generate multi-day schedule: {e}")
        import traceback
        traceback.print_exc()
        return 1


def explain_command(args: list[str]) -> int:
    """
    Explain command: Show details about a specific task.
    
    Usage: explain <task_id>
    """
    try:
        if not args:
            print_warning("Usage: explain <task_id>")
            return 1

        task_id = args[0]

        if not _context.projects:
            print_warning("No projects loaded. Run 'sync' first.")
            return 1
        
        # Find the task
        aggregator = TaskAggregator()
        aggregated_tasks = aggregator.aggregate(_context.projects)
        
        task = None
        project_context = None

        for agg_task in aggregated_tasks:
            if agg_task.task.id == task_id:
                task = agg_task.task
                project_context = agg_task.project_context
                break
        
        if not task:
            print_error(f"Task with ID '{task_id}' not found.")
            return 1
        
        # Display task details
        print_task_details(task, project_context)
        
        # Explain scheduling position
        task_pool = aggregator.get_task_pool(aggregated_tasks)
        incomplete_tasks = [t for t in task_pool if not t.completed]
        
        try:
            position = incomplete_tasks.index(task) + 1
            print_task_position(task, position, len(incomplete_tasks))
        
        except ValueError:
            console.print("[dim]Task is completed or not in the active queue[/dim]")
            console.print()
        
        return 0
    
    except Exception as e:
        print_error(f"Failed to explain task: {e}")
        return 1


def conflicts_command(args: list[str]) -> int:
    """
    Conflicts command: Show tasks that disagree across sources.

    Usage: conflicts

    A conflict happens when the same task id appears from two or more
    configured sources (e.g. a Google Docs document and a local file) with
    different content -- title, duration, deadline, completion state, etc.
    Conflicted tasks are excluded from `today`/`schedule` until resolved:
    edit or delete one of the versions shown here (with --source <name>
    to target a specific source), then re-run `sync`.
    """
    if args:
        print_error("conflicts command takes no arguments")
        return 1

    if not _context.projects:
        print_warning("No projects loaded. Run 'sync' first.")
        return 1

    aggregator = TaskAggregator()
    aggregator.aggregate(_context.projects)
    conflicts = aggregator.get_conflicts()

    if not conflicts:
        print_success("No sync conflicts.")
        return 0

    console.print()
    console.print(f"[bold]{len(conflicts)} task(s) disagree across sources:[/bold]")
    console.print()

    for conflict in conflicts:
        console.print(f"[yellow]Task id: {conflict.task_id}[/yellow]")
        for version in conflict.versions:
            label = version.project_context.document_label()
            console.print(f"  [cyan]{label}[/cyan] ({version.project_context.source})")
            console.print(f"    title:      {version.task.title}")
            console.print(f"    duration:   {format_duration(version.task.estimated_duration)}")
            console.print(f"    completed:  {version.task.completed}")
            if version.task.deadline_external:
                console.print(f"    ext. deadline: {version.task.deadline_external}")
            if version.task.deadline_user:
                console.print(f"    user deadline: {version.task.deadline_user}")
        console.print()

    console.print("[dim]Resolve by editing or deleting one version (--source <name> selects the source), then run 'sync'.[/dim]")
    console.print()
    return 0


def projects_command(args: list[str]) -> int:
    """
    Projects command: List all configured projects and their source(s).

    Usage: projects
    """
    if args:
        print_error("projects command takes no arguments")
        return 1

    try:
        from chronix.config import ChronixConfig

        config = ChronixConfig.load_or_default()

        if not config.projects:
            print_warning("No projects configured in your config file.")
            console.print(f"Edit [cyan]{ChronixConfig.get_default_path()}[/cyan] and add projects.")
            return 0

        console.print()
        console.print("[bold]Configured projects:[/bold]")
        console.print()

        # Show explicitly ranked projects first (lowest rank number first,
        # i.e. highest priority), then unranked ones in their configured order.
        ranked = sorted(
            config.projects,
            key=lambda p: p.priority if p.priority is not None else float("inf")
        )

        synced_project_ids = {p.project_context.project_id for p in _context.projects}

        for project in ranked:
            priority_str = f"[yellow]P{project.priority}[/yellow] " if project.priority is not None else ""
            synced_str = "" if project.name in synced_project_ids else " [dim](not synced yet)[/dim]"
            console.print(f"  {priority_str}[cyan]{project.label()}[/cyan]{synced_str}")
            for source in project.sources:
                source_id = source.document_id if source.type == "google_docs" else source.file_path
                type_str = "local" if source.type == "local_files" else "google_docs"
                console.print(f"      [dim]({type_str})[/dim] {source_id}")

        console.print()
        console.print(f"Use [cyan]sync <name> [name ...][/cyan] to sync specific projects")
        console.print("[dim]Priority (lower = higher) is set per project via `priority` in config.toml.[/dim]")
        console.print()
        return 0

    except Exception as e:
        print_error(f"Failed to list projects: {e}")
        return 1


def project_command(args: list[str]) -> int:
    """
    Project command: Show a project's task list, paginated.

    Usage: project <name> [--page N] [--per-page N] [--status incomplete|complete|all]

    Defaults to incomplete tasks, 20 per page. Shows tasks aggregated across
    all of the project's sources (see chronix.core.aggregation.TaskAggregator),
    so a task appearing in only one of two configured sources still shows up
    here. Requires the project to already be synced (the REPL syncs at
    startup; one-shot mode syncs it automatically before running this
    command).
    """
    DEFAULT_PER_PAGE = 20
    VALID_STATUS = ("incomplete", "complete", "all")

    usage = "Usage: project <name> [--page N] [--per-page N] [--status incomplete|complete|all]"

    page = 1
    per_page = DEFAULT_PER_PAGE
    status = "incomplete"
    remaining: list[str] = []

    i = 0
    while i < len(args):
        if args[i] == "--page" and i + 1 < len(args):
            try:
                page = int(args[i + 1])
            except ValueError:
                print_error(f"Invalid page number: '{args[i + 1]}'")
                return 1
            if page < 1:
                print_error("Page number must be positive")
                return 1
            i += 2
        elif args[i] == "--per-page" and i + 1 < len(args):
            try:
                per_page = int(args[i + 1])
            except ValueError:
                print_error(f"Invalid --per-page value: '{args[i + 1]}'")
                return 1
            if per_page < 1:
                print_error("--per-page must be positive")
                return 1
            i += 2
        elif args[i] == "--status" and i + 1 < len(args):
            status = args[i + 1]
            if status not in VALID_STATUS:
                print_error(f"Invalid status '{status}'. Valid: incomplete, complete, all")
                return 1
            i += 2
        elif args[i].startswith("--"):
            print_error(f"Unknown flag: {args[i]}")
            return 1
        else:
            remaining.append(args[i])
            i += 1

    if len(remaining) != 1:
        print_error(usage)
        return 1

    project_token = remaining[0]

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    project_name = _resolve_project_token(project_token, config)
    if project_name is None:
        print_error(f"Unknown project: '{project_token}'")
        return 1

    project_todos = [
        p for p in _context.projects if p.project_context.project_id == project_name
    ]
    if not project_todos:
        print_warning(f"Project not synced yet. Run 'sync {project_token}' first.")
        return 1

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(project_todos)
    all_tasks = [agg.task for agg in aggregated_tasks]

    incomplete_count = sum(1 for t in all_tasks if not t.completed)
    completed_count = len(all_tasks) - incomplete_count

    print_document_overview(
        document_label=project_todos[0].project_context.document_label(),
        total_tasks=len(all_tasks),
        incomplete_count=incomplete_count,
        completed_count=completed_count,
    )

    if status == "incomplete":
        filtered = [t for t in all_tasks if not t.completed]
    elif status == "complete":
        filtered = [t for t in all_tasks if t.completed]
    else:
        filtered = all_tasks

    start = (page - 1) * per_page
    page_tasks = filtered[start:start + per_page]

    if not page_tasks and filtered:
        print_error(f"Page {page} is out of range ({len(filtered)} {status} task(s) total).")
        return 1

    print_task_table(page_tasks)
    print_page_footer(page=page, per_page=per_page, total=len(filtered), status_label=status)

    return 0


def tabs_command(args: list[str]) -> int:
    """
    Tabs command: List the tabs in a project's Google Docs source, for use with add's --tab flag.

    Usage: tabs <name>

    Only applies to Google Docs sources -- local files have no tab concept.
    """
    if len(args) != 1:
        print_error("Usage: tabs <name>")
        return 1

    doc_token = args[0]

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    source = config.resolve_source(doc_token)
    if source is None:
        print_error(f"Unknown project: '{doc_token}'")
        return 1
    if source.type != "google_docs":
        print_error(f"'{doc_token}' is a local file, which has no tabs. Tabs only apply to Google Docs sources.")
        return 1
    doc_id = source.source_id

    try:
        client = _context._ensure_google_client()
        console.print("[dim]Authenticating with Google Docs...[/dim]")
        if not client.authenticate():
            print_error("Authentication failed. Please check your credentials.")
            return 1
        doc = client.fetch_document(doc_id)
    except Exception as e:
        print_error(f"Failed to fetch document: {e}")
        return 1

    structure = GoogleDocsParser().parse_document(doc)

    if not structure.tabs:
        print_warning("No tabs found in this document.")
        return 0

    console.print()
    console.print(f"[bold]Tabs in {config.google_docs.format_document_label(doc_id)}:[/bold]")
    console.print()

    for tab in structure.tabs:
        title = tab.title or "(untitled)"
        notes = []
        if tab.checkbox_list_id is None:
            notes.append("no TASKS section")
        if title.strip().lower() in EXCLUDED_TAB_TITLES:
            notes.append("excluded from auto-select")
        suffix = f" [dim]({', '.join(notes)})[/dim]" if notes else ""
        console.print(f"  [cyan]{title}[/cyan] [dim]({tab.tab_id})[/dim]{suffix}")

    console.print()
    console.print("Use a tab's title (or ID) with [cyan]add ... --tab <title|id>[/cyan]")
    console.print()
    return 0


def blocks_command(args: list[str]) -> int:
    """
    Blocks command: list or pause/resume recurring config time blocks for this session.

    Usage: blocks
           blocks pause <label|kind>
           blocks resume <label|kind>|all

    Lists (or toggles) the sleep windows, breaks, and recurring meetings
    defined in config.toml. Pausing a block stops it from being treated as
    blocked time in `today`, `schedule`, and `calendar` for the rest of this
    session only — config.toml itself is never modified, and the block
    reactivates on the next session (or via 'blocks resume').

    A block is identified by its label if it has one (e.g. "Netflix"),
    otherwise by its kind (e.g. "sleep"). Pausing by kind pauses every
    block of that kind that has no label of its own.
    """
    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    all_block_configs = (
        config.scheduling.sleep_windows
        + config.scheduling.breaks
        + config.scheduling.meetings
    )

    if not args:
        if not all_block_configs:
            print_info("No recurring blocks configured (sleep windows, breaks, or meetings).")
            return 0

        console.print()
        console.print("[bold]Recurring time blocks:[/bold]")
        console.print()
        for block in all_block_configs:
            name = block.label or block.kind.capitalize()
            paused = _block_config_key(block) in _context.disabled_blocks
            status = "[yellow]paused[/yellow]" if paused else "[green]active[/green]"
            time_range = f"{block.start_time.strftime('%H:%M')}-{block.end_time.strftime('%H:%M')}"
            console.print(f"  [cyan]{name}[/cyan] [dim]({block.kind}, {time_range})[/dim] — {status}")
        console.print()
        console.print("[dim]Pausing only affects this session; config.toml is untouched.[/dim]")
        console.print()
        return 0

    usage = "Usage: blocks | blocks pause <label|kind> | blocks resume <label|kind>|all"
    action = args[0]
    if action not in ("pause", "resume") or len(args) < 2:
        print_error(usage)
        return 1

    # Labels can contain spaces (e.g. "Weekly Planning"), so join the rest of
    # the args rather than taking args[1] alone. No quoting needed.
    raw_target = " ".join(args[1:]).strip()
    target = raw_target.lower()

    if action == "resume" and target == "all":
        count = len(_context.disabled_blocks)
        _context.disabled_blocks.clear()
        print_success(f"Resumed {count} paused block(s).")
        return 0

    if not any(_block_config_key(b) == target for b in all_block_configs):
        print_error(f"No configured block matches '{raw_target}'.")
        console.print("[dim]Run 'blocks' to see configured block names.[/dim]")
        return 1

    if action == "pause":
        _context.disabled_blocks.add(target)
        print_success(f"Paused '{raw_target}' for this session.")
    else:
        _context.disabled_blocks.discard(target)
        print_success(f"Resumed '{raw_target}'.")

    return 0


def help_command(args: list[str]) -> int:
    """
    Help command: Show available commands.
    
    Usage: help
    """
    console.print()
    console.print("[bold]Available commands:[/bold]")
    console.print()
    
    commands_table = [
        ("add [<duration> <title>] [flags]", "Create a task; run with no args for the interactive form"),
        ("blocks | blocks pause <label|kind> | blocks resume <label|kind>|all", "List or pause/resume recurring config time blocks for this session"),
        ("calendar [HH:MM] [--force] [--split]", "Sync primary track to Google Calendar; --split also syncs secondary (colored distinctly)"),
        ("config <cmd>", "Manage configuration (init, show, path, validate, reload)"),
        ("conflicts", "Show tasks that disagree across sources, excluded from scheduling until resolved"),
        ("deadline <task_id> <ISO|-> [--user] [--source <name>]", "Set external deadline; --user sets user deadline instead"),
        ("deadlines <task_id>|--source <name>|--all [--dry-run]", "Backfill deadline_computed (exactly one scope required)"),
        ("delete [<task_id>] [--source <name>]", "Delete a task; run with no args to be prompted and confirm"),
        ("projects", "List all configured projects and their source(s)"),
        ("project <name> [--page N] [--per-page N] [--status s]", "Show a project's task list, paginated"),
        ("tabs <name>", "List the tabs in a project's Google Docs source (for add's --tab flag)"),
        ("done [<task_id>] [--source <name>]", "Complete a task; run with no args to be prompted"),
        ("pause [<task_id>] [--source <name>]", "Close the current work session; run with no args to be prompted"),
        ("resume [<task_id>] [--source <name>]", "Open a new work session starting now; run with no args to be prompted"),
        ("duration <task_id> <dur> [--source <name>]", "Change a task's estimated duration (e.g. 2h, 30m)"),
        ("explain <task_id>", "Show details and scheduling info for a task"),
        ("meta [<task_id>] [k=v ...] [--remove k] [--source <name>]", "Set or remove metadata; run with no args to be prompted"),
        ("mode <task_id> <mode> [--source <name>]", "Set execution mode (atomic|flex|contiguous_preferred)"),
        ("track <task_id> <auto|primary|secondary> [--source <name>]", "Set which timeline (primary/secondary) a task belongs to"),
        ("rename <task_id> <title> [--source <name>]", "Rename a task"),
        ("schedule [days] | <start>-<end> | from <day> [to <count>]", "Display multi-day schedule, a day range, or a forecast from a future day"),
        ("sync", "Fetch and parse all configured projects"),
        ("sync <name> [...]", "Sync one or more specific projects by name"),
        ("today [HH:MM] [--split]", "Display today's scheduled tasks; --split shows primary/secondary tracks side by side"),
        ("undone [<task_id>] [--source <name>]", "Mark a task as incomplete; run with no args to be prompted"),
        ("update [<task_id>] [flags] [--source <name>]", "Update fields; run with no args (or id alone) for the interactive form"),
        ("clear / cls", "Clear the terminal screen"),
        ("help", "Show this help message"),
        ("exit / quit", "Exit the interactive shell"),
    ]
    
    for cmd, desc in commands_table:
        console.print(f"  [cyan]{cmd:52}[/cyan] [dim]{desc}[/dim]")
    
    console.print()
    console.print("[bold]Interactive forms:[/bold]")
    console.print("  add/update/meta open a full-screen form when required arguments are")
    console.print("  omitted; delete/done/undone/pause/resume prompt for a task ID on a")
    console.print("  plain line. Tab/↓ and Shift+Tab/↑ move between form fields, Ctrl+S")
    console.print("  submits, Esc cancels without making changes.")
    console.print()
    console.print("[bold]--source resolution:[/bold]")
    console.print("  In this interactive shell, --source is optional for task commands once a")
    console.print("  project has been synced; chronix resolves it from the task's ID automatically.")
    console.print("  In one-shot mode (chronix <cmd> ...), done/pause/resume require --source")
    console.print("  explicitly, since there's no prior sync to resolve it from.")
    console.print()
    console.print("[bold]Configuration:[/bold]")
    console.print(f"  [dim]Config file:[/dim] ~/.config/chronix/config.toml")
    console.print(f"  [dim]Run[/dim] [cyan]chronix config init[/cyan] [dim]to create a default configuration[/dim]")
    console.print()
    
    return 0


def _format_duration(duration: timedelta) -> str:
    """Format a timedelta as a human-readable string (legacy compatibility)."""
    return format_duration(duration)


def _parse_add_duration(value: str) -> timedelta:
    """Parse a duration string from the add command into a timedelta.

    Accepts: 2h, 30m, 2hours, 2hour, 30minutes, 30minute
    """
    import re
    v = value.strip().lower()
    if re.fullmatch(r'\d+h', v):
        return timedelta(hours=int(v[:-1]))
    if re.fullmatch(r'\d+m', v):
        return timedelta(minutes=int(v[:-1]))
    m = re.fullmatch(r'(\d+)hours?', v)
    if m:
        return timedelta(hours=int(m.group(1)))
    m = re.fullmatch(r'(\d+)minutes?', v)
    if m:
        return timedelta(minutes=int(m.group(1)))
    raise ValueError(
        f"Invalid duration '{value}'. Use: 2h, 30m, 2hours, 30minutes"
    )


def add_command(args: list[str]) -> int:
    """
    Add command: Create a new task in a project's source.

    Usage: add <duration> <title> [--source <name>] [--tab <title|id>] [--description <text>]
                [--external-deadline <ISO>] [--user-deadline <ISO>] [--mode <mode>]
                [--track <auto|primary|secondary>] [--ref <ref>] [--deps <ref1,ref2,...>]

    Duration examples: 2h, 30m, 2hours, 30minutes

    --source selects a project's source (its Google Docs document or local
    file); required when a project has both. There's no auto-detection
    here (unlike edit commands), since a brand-new task has no existing
    location to detect from.

    Without --tab, the task is inserted into the first tab that has a
    TASKS section (matching prior behavior). --tab only applies to a
    google_docs source.

    --description inserts the given text as an indented block immediately
    below the task line (a Tab-indented paragraph in Google Docs), separate
    from the ` ::: ` metadata line. Multiple lines can be passed by including
    literal newlines in the shell argument, or entered via the interactive form.

    Called with no arguments at all, opens a full-screen interactive form for
    every field instead. A flag given without its value (e.g. `add --title`)
    prompts only for that value on a plain line, leaving every other
    already-given flag as typed.
    """
    from chronix.core.metadata import parse_deadline
    from chronix.core.todo import TaskParser

    if not args:
        return _add_command_interactive()

    source_token: Optional[str] = None
    tab_token: Optional[str] = None
    description: Optional[str] = None
    external_deadline_str: Optional[str] = None
    user_deadline_str: Optional[str] = None
    mode: Optional[str] = None
    track: Optional[str] = None
    ref: Optional[str] = None
    deps: Optional[str] = None
    remaining: list[str] = []

    from chronix.cli.interactive_prompts import prompt_value

    i = 0
    while i < len(args):
        arg = args[i]
        has_value = i + 1 < len(args) and not (args[i + 1].startswith("--") and len(args[i + 1]) > 2)
        if arg == "--source":
            source_token = args[i + 1] if has_value else prompt_value("Source (project name)")
            i += 2 if has_value else 1
        elif arg == "--tab":
            tab_token = args[i + 1] if has_value else prompt_value("Tab (title or ID)")
            i += 2 if has_value else 1
        elif arg == "--description":
            description = args[i + 1] if has_value else prompt_value("Description")
            i += 2 if has_value else 1
        elif arg == "--external-deadline":
            external_deadline_str = args[i + 1] if has_value else prompt_value("External deadline (ISO-8601)")
            i += 2 if has_value else 1
        elif arg == "--user-deadline":
            user_deadline_str = args[i + 1] if has_value else prompt_value("User deadline (ISO-8601)")
            i += 2 if has_value else 1
        elif arg == "--mode":
            mode = args[i + 1] if has_value else prompt_value("Mode (atomic/flex/contiguous_preferred)")
            i += 2 if has_value else 1
        elif arg == "--track":
            track = args[i + 1] if has_value else prompt_value("Track (auto/primary/secondary)")
            i += 2 if has_value else 1
        elif arg == "--ref":
            ref = args[i + 1] if has_value else prompt_value("Ref")
            i += 2 if has_value else 1
        elif arg == "--deps":
            deps = args[i + 1] if has_value else prompt_value("Deps (comma-separated refs)")
            i += 2 if has_value else 1
        elif arg.startswith("--"):
            print_error(f"Unknown flag: {arg}")
            return 1
        else:
            remaining.append(arg)
            i += 1

    if len(remaining) < 2:
        print_error("Usage: add <duration> <title> [flags] (or 'add' alone for the interactive form)")
        return 1

    duration_str = remaining[0]
    title = " ".join(remaining[1:])

    try:
        duration = _parse_add_duration(duration_str)
    except ValueError as e:
        print_error(str(e))
        return 1

    if mode is not None and mode not in TaskParser.VALID_MODES:
        print_error(f"Invalid mode '{mode}'. Valid: atomic, flex, contiguous_preferred")
        return 1
    if track is not None and track not in TaskParser.VALID_TRACKS:
        print_error(f"Invalid track '{track}'. Valid: auto, primary, secondary")
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    tz = _configured_tz(config)
    try:
        external_deadline = parse_deadline(external_deadline_str, tz) if external_deadline_str else None
        user_deadline = parse_deadline(user_deadline_str, tz) if user_deadline_str else None
    except ValueError as e:
        print_error(str(e))
        return 1

    all_sources = config.all_sources()
    if not all_sources:
        print_error("No sources configured. Run 'chronix config init' to set up.")
        return 1

    if source_token is not None:
        source = config.resolve_source(source_token)
        if source is None:
            print_error(f"Unknown source: '{source_token}'")
            return 1
    elif len(all_sources) == 1:
        source = all_sources[0]
    else:
        print_error("Multiple sources configured. Specify one with --source <name>")
        for s in all_sources:
            console.print(f"  [cyan]{s.label()}[/cyan]")
        return 1

    from chronix.core.models import generate_task_id
    from chronix.core.writer import NewTask

    try:
        writer = _get_task_writer(tz, source_type=source.type)
        task_id = generate_task_id()
        writer.create_task(
            source.source_id,
            NewTask(
                title=title,
                duration=duration,
                id=task_id,
                tab=tab_token,
                description=description,
                external_deadline=external_deadline,
                user_deadline=user_deadline,
                mode=mode,
                track=track,
                ref=ref,
                depends=deps,
            ),
        )
        _resync_project_sources(source.project_name, config)
        print_success(f"Task added: {title} ({duration_str}) [id={task_id}]")
        return 0
    except Exception as e:
        print_error(f"Failed to add task: {e}")
        return 1


def _add_command_interactive() -> int:
    """Full-screen interactive form for creating a task, invoked by `add` with no args.

    Resolves the target project/source/tab up front (same rules as the
    flagged path: a single configured source auto-selected, multiple
    requires a choice), then opens the shared TaskForm with every field
    starting empty. When more than one source is configured, the project is
    chosen first, and then, only if that project itself has two sources,
    the source picker is scoped to that project's own sources rather than
    every source in config. Submitting creates the task; cancelling
    (Escape) aborts with no changes made.
    """
    from chronix.core.metadata import parse_deadline
    from chronix.core.models import generate_task_id
    from chronix.core.todo import TaskParser
    from chronix.core.writer import NewTask
    from chronix.cli.interactive_form import TaskForm, build_task_form_fields
    from chronix.cli.interactive_prompts import prompt_value

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    all_sources = config.all_sources()
    if not all_sources:
        print_error("No sources configured. Run 'chronix config init' to set up.")
        return 1

    if len(all_sources) == 1:
        source = all_sources[0]
    else:
        project_labels = [p.label() for p in config.projects if p.sources]
        console.print("[cyan]Configured projects:[/cyan] " + ", ".join(project_labels))
        project_token = prompt_value("Project (name)")
        if not project_token:
            print_warning("Cancelled: a project is required.")
            return 1
        project = config.find_project(project_token)
        if project is None:
            print_error(f"Unknown project: '{project_token}'")
            return 1
        project_sources = config.sources_for_project(project.name)
        if not project_sources:
            print_error(f"Project '{project.name}' has no sources configured.")
            return 1
        if len(project_sources) == 1:
            source = project_sources[0]
        else:
            labels = [s.label() for s in project_sources]
            console.print("[cyan]This project has two sources:[/cyan] " + ", ".join(labels))
            source_token = prompt_value("Source (project name)")
            if not source_token:
                print_warning("Cancelled: a source is required.")
                return 1
            resolved = config.resolve_source(source_token)
            source = resolved if resolved in project_sources else None
            if source is None:
                print_error(f"Unknown source: '{source_token}'")
                return 1

    fields = build_task_form_fields(include_tab_field=True)
    form = TaskForm(title="Add Task", fields=fields)
    result = form.run()
    if result is None:
        print_info("Cancelled. No task was created.")
        return 1

    tz = _configured_tz(config)
    try:
        duration = _parse_add_duration(result["duration"])
        external_deadline = parse_deadline(result["external_deadline"], tz) if result["external_deadline"].strip() else None
        user_deadline = parse_deadline(result["user_deadline"], tz) if result["user_deadline"].strip() else None
    except ValueError as e:
        print_error(str(e))
        return 1

    mode = result["mode"].strip() or None
    track = result["track"].strip() or None
    if mode is not None and mode not in TaskParser.VALID_MODES:
        print_error(f"Invalid mode '{mode}'. Valid: atomic, flex, contiguous_preferred")
        return 1
    if track is not None and track not in TaskParser.VALID_TRACKS:
        print_error(f"Invalid track '{track}'. Valid: auto, primary, secondary")
        return 1

    try:
        writer = _get_task_writer(tz, source_type=source.type)
        task_id = generate_task_id()
        writer.create_task(
            source.source_id,
            NewTask(
                title=result["title"],
                duration=duration,
                id=task_id,
                tab=result["tab"].strip() or None,
                description=result["description"].strip() or None,
                external_deadline=external_deadline,
                user_deadline=user_deadline,
                mode=mode,
                track=track,
                ref=result["ref"].strip() or None,
                depends=result["deps"].strip() or None,
            ),
        )
        _resync_project_sources(source.project_name, config)
        print_success(f"Task added: {result['title']} [id={task_id}]")
        return 0
    except Exception as e:
        print_error(f"Failed to add task: {e}")
        return 1


def _display_continuous_timeline(day_schedule, work_start: datetime, work_end: datetime, start_index: int = 1, paused_blocks: Optional[list] = None) -> int:
    """
    Display a continuous timeline including tasks, blocked time, and empty slots.
    
    Args:
        day_schedule: The day's schedule to display
        work_start: Start of work day
        work_end: End of work day
        start_index: Starting index for task numbering (for continuous numbering across days)
        paused_blocks: Recurring config blocks paused for this session that fall
            within work_start/work_end. These are display-only: they never occupy
            time (tasks may be scheduled straight through them) but are still shown,
            greyed out, at their original slot so it's visible that a block used to
            be there. They're layered onto the timeline after it's built, so they
            never affect gap-filling or task scheduling.
    
    Returns:
        The next index to use for the next day (last_index + 1)
    """
    from chronix.core.models import DaySchedule
    
    # Build a list of all time segments
    segments = []
    
    # Add scheduled tasks
    for scheduled_task in day_schedule.scheduled_tasks:
        segments.append({
            'start': scheduled_task.start,
            'end': scheduled_task.end,
            'type': 'task',
            'data': scheduled_task
        })
    
    # Add blocked time
    for block in day_schedule.blocked_time:
        segments.append({
            'start': block.start,
            'end': block.end,
            'type': 'blocked',
            'data': block
        })
    
    # Sort by start time
    segments.sort(key=lambda x: x['start'])
    
    # Build continuous timeline by filling gaps
    timeline = []
    current_time = work_start
    
    for segment in segments:
        # If there's a gap before this segment, add an empty slot. Segments
        # can overlap (e.g. a short meeting nested inside a longer transit
        # block), so track the furthest point covered so far rather than
        # the end of the most recently processed segment.
        if current_time < segment['start']:
            timeline.append({
                'start': current_time,
                'end': segment['start'],
                'type': 'empty',
                'data': None
            })
        
        # Add the segment
        timeline.append(segment)
        current_time = max(current_time, segment['end'])
    
    # If there's time remaining until work_end, add final empty slot
    if current_time < work_end:
        timeline.append({
            'start': current_time,
            'end': work_end,
            'type': 'empty',
            'data': None
        })

    # Layer paused blocks on top of the already-built timeline, purely for
    # display: they're inserted at their original chronological position but
    # never touched current_time above, so they don't displace or shrink
    # whatever real segment (task/blocked/empty) already covers that slot.
    if paused_blocks:
        for block in paused_blocks:
            timeline.append({
                'start': block.start,
                'end': block.end,
                'type': 'paused',
                'data': block,
            })
        timeline.sort(key=lambda seg: seg['start'])
    
    # Display the timeline
    console.print("[bold]⏰ Today's Timeline[/bold]")
    console.print()
    
    # Get display timezone from work_start
    display_tz = work_start.tzinfo if work_start.tzinfo else None
    
    current_index = start_index
    for segment in timeline:
        print_timeline_segment(
            index=current_index,
            start=segment['start'],
            end=segment['end'],
            segment_type=segment['type'],
            data=segment['data'],
            display_tz=display_tz
        )
        current_index += 1
    
    return current_index


# ---------------------------------------------------------------------------
# Task editing commands
# ---------------------------------------------------------------------------


def _get_task_writer(tz: ZoneInfo, source_type: str = "google_docs"):
    """Return a TaskWriter for the given source type.

    For google_docs, this reuses the shared authenticated client (so a
    single OAuth flow serves every write in the session); local_files needs
    no client/auth at all. Defaults to google_docs so every pre-existing
    call site that hasn't been updated to pass source_type keeps working
    unchanged.
    """
    from chronix.integrations.factory import get_writer, get_writer_for_client
    if source_type == "google_docs":
        client = _context._ensure_google_client()
        return get_writer_for_client(source_type, client, tz=tz)
    return get_writer(source_type, tz=tz)


def _resync_project_sources(project_name: str, config) -> None:
    """Refresh in-memory task state for every source of a project after a write.

    A project with two sources needs both refreshed together after any write
    to either one: merge/conflict logic in TaskAggregator.aggregate only sees
    what's currently in `_context.projects`, so leaving the other source
    stale would let a real conflict go undetected until the next full sync.
    Never raises: a refresh failure here must not be mistaken for the write
    itself failing.
    """
    from chronix.cli.sync_helpers import _sync_single_source_with_retries
    from chronix.integrations.factory import get_client

    sources = config.sources_for_project(project_name)
    if not sources:
        print_warning("Could not refresh local state: project is no longer configured. Run 'sync' to pick up the change.")
        return

    refreshed = []
    for source in sources:
        try:
            client = _context._ensure_google_client() if source.type == "google_docs" else get_client(source)
            result, project, _meetings = _sync_single_source_with_retries(
                source, client, tz=_configured_tz(config)
            )
        except Exception:
            print_warning(f"Could not refresh local state for {source.label()}. Run 'sync' to pick up the change.")
            continue

        if result.outcome.value != "success":
            print_warning(f"Could not refresh local state for {source.label()}. Run 'sync' to pick up the change.")
            continue

        refreshed.append(project)

    if not refreshed:
        return

    _context.projects = [
        p for p in _context.projects if p.project_context.project_id != project_name
    ] + refreshed
    _context.last_sync = datetime.now(timezone.utc)
    _context.config = config


def _find_project_for_task(task_id: str) -> Optional[str]:
    """Return the project_name of the currently-synced project containing task_id.

    Only looks at in-memory `_context.projects`, so this is only useful after
    a sync has populated it (e.g. the REPL's startup sync, or a preceding
    `sync` in the same session). Returns None if not found there.
    """
    for project in _context.projects:
        for task in project.tasks:
            if task.id == task_id:
                return project.project_context.project_name
    return None


def _find_source_for_task(task_id: str, config) -> Optional['SourceRef']:
    """Find which of a task's project's sources actually contains task_id.

    A task belongs to exactly one source even when its project has two (see
    chronix.core.aggregation.TaskAggregator.aggregate: cross-source entries
    merge by id, they don't fan out from one source into the other). This
    searches `_context.projects` -- which holds one ProjectTodoList per
    source -- for the specific ProjectTodoList whose own tasks include
    task_id, then resolves that source's SourceRef from config.
    """
    for project_todo in _context.projects:
        if any(t.id == task_id for t in project_todo.tasks):
            return next(
                (
                    s for s in config.sources_for_project(project_todo.project_context.project_id)
                    if s.type == project_todo.project_context.source
                ),
                None,
            )
    return None


def _resolve_edit_source(task_id: str, source_token: Optional[str], config) -> Optional['SourceRef']:
    """Resolve the source a task-editing command should write to.

    With an explicit --source token, that source wins outright. Otherwise,
    auto-detect by searching the task's project's sources for whichever one
    actually holds task_id (see _find_source_for_task) -- this is the
    settled default for edit commands, since the task's existing location is
    unambiguous and doesn't need the user to specify it. Falls back to the
    single configured source if there's only one and detection found
    nothing (e.g. a task not yet synced into context).
    """
    if source_token is not None:
        return config.resolve_source(source_token)
    found = _find_source_for_task(task_id, config)
    if found is not None:
        return found
    all_sources = config.all_sources()
    if len(all_sources) == 1:
        return all_sources[0]
    return None


def _parse_edit_flags(args: list[str]) -> tuple[Optional[str], list[str]]:
    """Extract --source <token> from args. Returns (source_token, remaining_args)."""
    source_token: Optional[str] = None
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--source" and i + 1 < len(args):
            source_token = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return source_token, remaining


def _reject_flag_as_task_id(task_id: str, usage: str) -> bool:
    """Print an error and return True if task_id looks like a flag, not an id.

    Guards against a missing positional task_id causing the next flag token
    to be silently swallowed as the id (e.g. `update --remove-meta foo ...`
    treating '--remove-meta' as the task_id and shifting every argument
    after it out of place).
    """
    if task_id.startswith("--"):
        print_error(f"Expected <task_id> as the first argument, got flag '{task_id}'.")
        console.print(f"[dim]{usage}[/dim]")
        return True
    return False


def _run_task_update(task_id: str, update, source_token: Optional[str] = None) -> int:
    from chronix.core.writer import TaskNotFoundError
    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    source = _resolve_edit_source(task_id, source_token, config)
    if source is None:
        if not config.all_sources():
            print_error("No sources configured.")
        else:
            print_error("Multiple sources configured for this task's project. Specify one with --source <name>")
            for s in config.all_sources():
                console.print(f"  [cyan]{s.label()}[/cyan]")
        return 1

    try:
        writer = _get_task_writer(_configured_tz(config), source_type=source.type)
        writer.update_task(source.source_id, task_id, update)
        _resync_project_sources(source.project_name, config)
        return 0
    except TaskNotFoundError:
        print_error(f"No task with id='{task_id}' found.")
        return 1
    except Exception as e:
        print_error(f"Update failed: {e}")
        return 1


def _find_duplicate_ref_task(ref_value: str, task_id: str) -> Optional[Task]:
    """Return another task already using ref_value, if any (excluding task_id itself).

    Only checks tasks visible in the current in-memory context, so this is a
    best-effort, fail-fast check ahead of the authoritative validation that
    DependencyValidator performs across the full backlog at scheduling time.
    """
    if not _context.projects:
        return None
    aggregator = TaskAggregator()
    for agg_task in aggregator.aggregate(_context.projects):
        t = agg_task.task
        if t.ref == ref_value and t.id != task_id:
            return t
    return None


def _update_command_interactive(task_id: str, doc_token: Optional[str] = None) -> int:
    """Open a full-screen form prefilled with task_id's current values and apply edits.

    Requires the task to already be visible in `_context.projects` (i.e. a
    prior sync); this mirrors every other edit command's `_find_task_in_context`
    dependency rather than triggering an implicit sync here.
    """
    from chronix.core.metadata import (
        KEY_DEPENDS, KEY_REF, parse_deadline, parse_duration,
        serialize_deadline, serialize_duration,
    )
    from chronix.core.todo import TaskParser
    from chronix.core.writer import TaskUpdate
    from chronix.cli.interactive_form import TaskForm, build_task_form_fields

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1
    tz = _configured_tz(config)

    fields = build_task_form_fields(
        title=task.title,
        duration_str=serialize_duration(task.estimated_duration),
        description=task.description or "",
        external_deadline_str=serialize_deadline(task.deadline_external, tz) if task.deadline_external else "",
        user_deadline_str=serialize_deadline(task.deadline_user, tz) if task.deadline_user else "",
        mode=task.execution_mode,
        track=task.track,
        ref=task.ref or "",
        deps=",".join(task.depends_on),
        include_tab_field=False,
    )
    form = TaskForm(title=f"Update Task ({task_id})", fields=fields)
    result = form.run()
    if result is None:
        print_info("Cancelled. No changes made.")
        return 1

    try:
        duration = parse_duration(result["duration"])
        if duration is None:
            print_error(f"Invalid duration: '{result['duration']}'. Use: 2h, 30m, 2hours, 30minutes")
            return 1
        external_deadline = parse_deadline(result["external_deadline"], tz) if result["external_deadline"].strip() else None
        user_deadline = parse_deadline(result["user_deadline"], tz) if result["user_deadline"].strip() else None
    except ValueError as e:
        print_error(str(e))
        return 1

    mode = result["mode"].strip() or None
    track = result["track"].strip() or None
    if mode is not None and mode not in TaskParser.VALID_MODES:
        print_error(f"Invalid mode '{mode}'. Valid: atomic, flex, contiguous_preferred")
        return 1
    if track is not None and track not in TaskParser.VALID_TRACKS:
        print_error(f"Invalid track '{track}'. Valid: auto, primary, secondary")
        return 1

    new_ref = result["ref"].strip() or None
    if new_ref and new_ref != task.ref:
        conflict = _find_duplicate_ref_task(new_ref, task_id)
        if conflict is not None:
            print_error(
                f"Duplicate ref '{new_ref}': already used by task "
                f"'{conflict.title}' (id={conflict.id})."
            )
            return 1

    update = TaskUpdate(
        title=result["title"],
        duration=duration,
        description=result["description"].strip() or None,
        external_deadline=external_deadline,
        user_deadline=user_deadline,
        mode=mode,
        track=track,
    )
    update.metadata[KEY_REF] = new_ref or ""
    update.metadata[KEY_DEPENDS] = result["deps"].strip()

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' updated.")
    return rc


def update_command(args: list[str]) -> int:
    """
    Update command: Modify one or more fields of a task by its ID.

    Usage: update <task_id> [--title <title>] [--duration <duration>]
                            [--description <text>|-]
                            [--external-deadline <ISO|->] [--user-deadline <ISO|->]
                            [--mode <mode>] [--ref <ref>|-] [--deps <ref1,ref2,...>|-]
                            [--meta <key=value> ...]
                            [--remove-meta <key> ...]
                            [--source <name>]

    At least one field flag must be supplied when task_id and flags are both
    given. Called as `update` alone, prompts for a task_id then opens a
    full-screen interactive form prefilled with that task's current values.
    Called as `update <task_id>` with no flags, skips straight to that form.
    A flag given without its value (e.g. `update abc123 --title`) prompts
    only for that value on a plain line, leaving every other field untouched.
    """
    from chronix.core.metadata import KEY_DEPENDS, KEY_REF, parse_deadline, parse_duration
    from chronix.core.todo import TaskParser
    from chronix.core.writer import TaskUpdate
    from chronix.cli.interactive_prompts import prompt_task_id, prompt_value

    VALID_FLAGS = (
        "--title", "--duration", "--description", "--external-deadline", "--user-deadline",
        "--mode", "--track", "--ref", "--deps", "--meta", "--remove-meta", "--source",
    )

    usage = (
        "Usage: update <task_id> [--title <title>] [--duration <duration>] "
        "[--description <text>|-] "
        "[--external-deadline <ISO|->] [--user-deadline <ISO|->] [--mode <mode>] "
        "[--track <auto|primary|secondary>] "
        "[--ref <ref>|-] [--deps <ref1,ref2,...>|-] [--meta key=value ...] "
        "[--remove-meta key ...] [--source <name>]"
    )

    if not args:
        task_id = prompt_task_id("update")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
        return _update_command_interactive(task_id)

    task_id = args[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    doc_token, flags = _parse_edit_flags(args[1:])

    if not flags:
        return _update_command_interactive(task_id, doc_token)

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1
    tz = _configured_tz(config)

    update = TaskUpdate()
    i = 0
    while i < len(flags):
        flag = flags[i]
        has_value = i + 1 < len(flags) and not (flags[i + 1].startswith("--") and len(flags[i + 1]) > 2)
        if flag == "--title":
            update.title = flags[i + 1] if has_value else prompt_value("Title")
            i += 2 if has_value else 1
        elif flag == "--duration":
            dur_str = flags[i + 1] if has_value else prompt_value("Duration (e.g. 2h, 30m)")
            dur = parse_duration(dur_str) if dur_str else None
            if dur is None:
                print_error(f"Invalid duration: '{dur_str}'. Use: 2h, 30m, 2hours, 30minutes")
                return 1
            update.duration = dur
            i += 2 if has_value else 1
        elif flag == "--description":
            desc_value = flags[i + 1] if has_value else prompt_value("Description (or '-' to clear)")
            update.description = None if desc_value == "-" else desc_value
            i += 2 if has_value else 1
        elif flag == "--external-deadline":
            value = flags[i + 1] if has_value else prompt_value("External deadline (ISO-8601, or '-' to clear)")
            try:
                update.external_deadline = parse_deadline(value, tz)
            except ValueError as e:
                print_error(str(e))
                return 1
            i += 2 if has_value else 1
        elif flag == "--user-deadline":
            value = flags[i + 1] if has_value else prompt_value("User deadline (ISO-8601, or '-' to clear)")
            try:
                update.user_deadline = parse_deadline(value, tz)
            except ValueError as e:
                print_error(str(e))
                return 1
            i += 2 if has_value else 1
        elif flag == "--mode":
            new_mode = flags[i + 1] if has_value else prompt_value("Mode (atomic/flex/contiguous_preferred)")
            if new_mode not in TaskParser.VALID_MODES:
                print_error(f"Invalid mode '{new_mode}'. Valid: atomic, flex, contiguous_preferred")
                return 1
            update.mode = new_mode
            i += 2 if has_value else 1
        elif flag == "--track":
            new_track = flags[i + 1] if has_value else prompt_value("Track (auto/primary/secondary)")
            if new_track not in TaskParser.VALID_TRACKS:
                print_error(f"Invalid track '{new_track}'. Valid: auto, primary, secondary")
                return 1
            update.track = new_track
            i += 2 if has_value else 1
        elif flag == "--ref":
            ref_value = flags[i + 1] if has_value else prompt_value("Ref (or '-' to clear)")
            update.metadata[KEY_REF] = "" if ref_value == "-" else (ref_value or "")
            i += 2 if has_value else 1
        elif flag == "--deps":
            deps_value = flags[i + 1] if has_value else prompt_value("Deps (comma-separated refs, or '-' to clear)")
            update.metadata[KEY_DEPENDS] = "" if deps_value == "-" else (deps_value or "")
            i += 2 if has_value else 1
        elif flag == "--meta":
            if i + 1 >= len(flags):
                print_error("--meta requires key=value (e.g. priority=high)")
                return 1
            pair = flags[i + 1]
            if "=" not in pair:
                print_error(f"--meta value must be key=value, got: '{pair}'")
                return 1
            k, _, v = pair.partition("=")
            update.metadata[k.strip()] = v.strip()
            i += 2
        elif flag == "--remove-meta":
            if i + 1 >= len(flags):
                print_error("--remove-meta requires a key")
                return 1
            update.metadata_remove.append(flags[i + 1])
            i += 2
        else:
            print_error(f"Unknown flag: {flag}. Valid flags: {', '.join(VALID_FLAGS)}")
            return 1

    if not any([
        update.title,
        update.duration,
        update.has_description_change(),
        update.has_external_deadline_change(),
        update.has_user_deadline_change(),
        update.mode,
        update.track,
        update.metadata,
        update.metadata_remove,
        update.completed is not None,
    ]):
        print_error("No fields to update. Provide at least one flag.")
        console.print(f"[dim]{usage}[/dim]")
        return 1

    new_ref = update.metadata.get(KEY_REF)
    if new_ref:
        conflict = _find_duplicate_ref_task(new_ref, task_id)
        if conflict is not None:
            print_error(
                f"Duplicate ref '{new_ref}': already used by task "
                f"'{conflict.title}' (id={conflict.id})."
            )
            return 1

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' updated.")
    return rc


def rename_command(args: list[str]) -> int:
    """
    Rename command: Change the title of a task.

    Usage: rename <task_id> <new title> [--source <name>]
    """
    from chronix.core.writer import TaskUpdate

    usage = "Usage: rename <task_id> <new title> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)
    if len(remaining) < 2:
        print_error(usage)
        return 1

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    new_title = " ".join(remaining[1:])
    update = TaskUpdate(title=new_title)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' renamed to '{new_title}'.")
    return rc


def duration_command(args: list[str]) -> int:
    """
    Duration command: Update the estimated duration of a task.

    Usage: duration <task_id> <duration> [--source <name>]

    Duration examples: 2h, 30m, 2hours, 30minutes
    """
    from chronix.core.metadata import parse_duration
    from chronix.core.writer import TaskUpdate

    usage = "Usage: duration <task_id> <duration> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)
    if len(remaining) < 2:
        print_error(usage)
        return 1

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    dur = parse_duration(remaining[1])
    if dur is None:
        print_error(f"Invalid duration: '{remaining[1]}'. Use: 2h, 30m, 2hours, 30minutes")
        return 1

    task = _find_task_in_context(task_id)
    if task is not None and not task.completed:
        actual = task.compute_actual_duration()
        if dur <= actual:
            print_error(
                f"New duration ({remaining[1]}) must exceed already-logged time "
                f"({format_duration(actual)})."
            )
            return 1

    update = TaskUpdate(duration=dur)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' duration set to '{remaining[1]}'.")
    return rc


def deadline_command(args: list[str]) -> int:
    """
    Deadline command: Set the external or user deadline of a task.

    Usage: deadline <task_id> <ISO-date|-> [--user] [--source <name>]

    Without --user, updates external_deadline.
    With --user, updates user_deadline.
    Use '-' to clear a deadline.
    """
    from chronix.core.metadata import parse_deadline
    from chronix.core.writer import TaskUpdate

    use_user = "--user" in args
    args = [a for a in args if a != "--user"]
    doc_token, remaining = _parse_edit_flags(args)

    usage = "Usage: deadline <task_id> <ISO-date|-> [--user] [--source <name>]"
    if len(remaining) < 2:
        print_error(usage)
        return 1

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    try:
        dt = parse_deadline(remaining[1], _configured_tz(config))
    except ValueError as e:
        print_error(str(e))
        return 1

    update = TaskUpdate()
    if use_user:
        update.user_deadline = dt
    else:
        update.external_deadline = dt

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        kind = "user" if use_user else "external"
        value = remaining[1]
        print_success(f"Task '{task_id}' {kind} deadline set to '{value}'.")
    return rc


def deadlines_command(args: list[str]) -> int:
    """
    Deadlines command: Backfill deadline_computed for tasks with no real deadline.

    Usage: deadlines <task_id> [--dry-run]
           deadlines --source <name> [--dry-run]
           deadlines --all [--dry-run]

    Exactly one scope must be given:
    - <task_id>: backfill only that task.
    - --source <name>: backfill every eligible task in that project.
    - --all: backfill every eligible task across all synced projects.

    There is no bulk default: a task with no deadline may simply not have
    one, so touching more than a single task always requires an explicit
    --source or --all.

    Eligible tasks are incomplete tasks with neither an external nor a user
    deadline. Projection always considers the full synced task pool so
    timing stays realistic across projects, even when only writing back to
    a narrower scope: tasks are ordered oldest-created first and stacked
    sequentially, each claiming a slice of time equal to its
    estimated_duration, starting after the later of now or the latest
    deadline already committed elsewhere in the backlog.

    --dry-run previews the computed deadlines without writing them.
    Re-running this command recomputes and overwrites deadline_computed for
    the tasks in scope; it is not a one-time stamp.
    """
    from chronix.core.deadline_backfill import compute_backlog_deadlines
    from chronix.core.metadata import KEY_DEADLINE_COMPUTED, serialize_deadline
    from chronix.core.writer import TaskUpdate

    usage = "Usage: deadlines <task_id> | --source <name> | --all [--dry-run]"

    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    all_scope = "--all" in args
    args = [a for a in args if a != "--all"]
    doc_token, remaining = _parse_edit_flags(args)

    if len(remaining) > 1:
        print_error(usage)
        return 1

    if remaining and remaining[0].startswith("--"):
        print_error(f"Unknown flag: '{remaining[0]}'")
        console.print(f"[dim]{usage}[/dim]")
        return 1

    scopes_given = sum([bool(remaining), doc_token is not None, all_scope])
    if scopes_given == 0:
        print_error(usage)
        console.print("[dim]Specify a single task, a project with --source, or --all for the whole backlog.[/dim]")
        return 1
    if scopes_given > 1:
        print_error("Specify only one of: <task_id>, --source <name>, --all")
        return 1

    task_id = remaining[0] if remaining else None

    if not _context.projects:
        print_warning("No projects loaded. Run 'sync' first.")
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)
    all_tasks = [agg.task for agg in aggregated_tasks]

    if task_id is not None and not any(t.id == task_id for t in all_tasks):
        print_error(f"Task with ID '{task_id}' not found.")
        return 1

    source = None
    if doc_token is not None:
        source = config.resolve_source(doc_token)
        if source is None:
            print_error(f"Unknown source: '{doc_token}'")
            return 1

    now = datetime.now(timezone.utc)
    results = compute_backlog_deadlines(all_tasks, now)

    if task_id is not None:
        results = [r for r in results if r.task.id == task_id]
    elif source is not None:
        source_task_ids = {
            t.id for p in _context.projects
            if p.project_context.project_id == source.project_name
            for t in p.tasks
        }
        results = [r for r in results if r.task.id in source_task_ids]
    # --all: no further filtering

    if not results:
        print_info(
            "No eligible tasks to backfill (all tasks in scope already have a "
            "real deadline, or none matched the given scope)."
        )
        return 0

    console.print()
    for r in results:
        label = f"[{r.task.document_title}] " if r.task.document_title else ""
        console.print(
            f"  {label}{r.task.title} ({r.task.id}) -> "
            f"{r.deadline.strftime('%Y-%m-%d %H:%M')} UTC"
        )
    console.print()

    if dry_run:
        print_info(f"Dry run: {len(results)} task(s) would be updated. No changes written.")
        return 0

    failures = 0
    for r in results:
        if r.task.id is None:
            print_warning(f"Skipping '{r.task.title}': task has no id yet. Run 'sync' first.")
            failures += 1
            continue
        update = TaskUpdate(metadata={KEY_DEADLINE_COMPUTED: serialize_deadline(r.deadline, _configured_tz(config))})
        rc = _run_task_update(r.task.id, update)
        if rc != 0:
            failures += 1

    updated = len(results) - failures
    print_success(f"Backfilled deadline_computed for {updated} task(s).")
    if failures:
        print_warning(f"{failures} task(s) failed to update.")
    return 1 if failures else 0


def mode_command(args: list[str]) -> int:
    """
    Mode command: Set the execution mode of a task.

    Usage: mode <task_id> <atomic|flex|contiguous_preferred> [--source <name>]
    """
    from chronix.core.writer import TaskUpdate

    VALID_MODES = {"atomic", "flex", "contiguous_preferred"}
    doc_token, remaining = _parse_edit_flags(args)

    usage = "Usage: mode <task_id> <atomic|flex|contiguous_preferred> [--source <name>]"
    if len(remaining) < 2:
        print_error(usage)
        return 1

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    new_mode = remaining[1]
    if new_mode not in VALID_MODES:
        print_error(f"Invalid mode '{new_mode}'. Valid: atomic, flex, contiguous_preferred")
        return 1

    update = TaskUpdate(mode=new_mode)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' mode set to '{new_mode}'.")
    return rc


def track_command(args: list[str]) -> int:
    """
    Track command: Set which independently-scheduled timeline a task belongs to.

    Usage: track <task_id> <auto|primary|secondary> [--source <name>]

    "auto" clears any explicit override and lets chronix infer the track from
    the task's execution mode and duration at scheduling time (see
    chronix.core.tracks.resolve_track). "primary" and "secondary" pin the
    task to that lane regardless of the auto heuristic.
    """
    from chronix.core.writer import TaskUpdate

    VALID_TRACKS = {"auto", "primary", "secondary"}
    doc_token, remaining = _parse_edit_flags(args)

    usage = "Usage: track <task_id> <auto|primary|secondary> [--source <name>]"
    if len(remaining) < 2:
        print_error(usage)
        return 1

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    new_track = remaining[1]
    if new_track not in VALID_TRACKS:
        print_error(f"Invalid track '{new_track}'. Valid: auto, primary, secondary")
        return 1

    update = TaskUpdate(track=new_track)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' track set to '{new_track}'.")
    return rc


def _find_task_in_context(task_id: str) -> Optional[Task]:
    """Return the Task with the given id from the current context, or None."""
    if not _context.projects:
        return None
    from chronix.core.aggregation import TaskAggregator
    aggregator = TaskAggregator()
    for agg_task in aggregator.aggregate(_context.projects):
        if agg_task.task.id == task_id:
            return agg_task.task
    return None


# Fallback lookback when a task has no recorded `created` timestamp (e.g.
# synced before that field existed). Matches the old fixed-window behavior.
_DEFAULT_CALENDAR_SEARCH_LOOKBACK = timedelta(days=14)


def _get_calendar_task_start(task_id: str, created: Optional[datetime] = None) -> Optional[datetime]:
    """Look up the scheduled calendar start time for a task, or return None.

    Searches from the task's creation time through now, so a task's calendar
    event is still found even if it was scheduled well in the past (e.g. an
    overdue task only just salvaged) -- searching to the deadline instead of
    now would miss events created after an already-blown deadline.
    """
    try:
        from chronix.integrations.google_calendar import CalendarSyncService
        sync_service = CalendarSyncService()
        search_end = datetime.now(timezone.utc)
        search_start = created if created is not None else search_end - _DEFAULT_CALENDAR_SEARCH_LOOKBACK
        return sync_service.find_task_scheduled_start(task_id, search_start, search_end)
    except Exception:
        return None


def done_command(args: list[str]) -> int:
    """
    Done command: Mark a task as complete, recording actual work duration.

    Closes any active session, computes actual_duration from all sessions,
    and marks the task complete. If no sessions exist and a calendar event
    is found, a session from the calendar start to now is recorded.

    Usage: done <task_id> [--source <name>]

    Called with no arguments, prompts for a task_id.
    """
    from chronix.core.writer import TaskUpdate
    from chronix.core.models import WorkSession
    from chronix.core.metadata import (
        KEY_ACTIVE_SINCE,
        KEY_ACTUAL_DURATION,
        KEY_SESSIONS,
        serialize_duration,
        serialize_sessions,
    )
    from chronix.cli.interactive_prompts import prompt_task_id

    usage = "Usage: done <task_id> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("mark done")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
    else:
        task_id = remaining[0]
        if _reject_flag_as_task_id(task_id, usage):
            return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    now = datetime.now(timezone.utc)

    update = TaskUpdate(completed=True)

    task = _find_task_in_context(task_id)
    if task is not None:
        sessions = list(task.sessions)

        if task.active_since is not None:
            sessions.append(WorkSession(start=task.active_since, end=now))
            update.metadata_remove.append(KEY_ACTIVE_SINCE)
        elif not sessions:
            calendar_start = _get_calendar_task_start(task_id, created=task.created)
            if calendar_start is not None:
                sessions.append(WorkSession(start=calendar_start, end=now))
            else:
                print_warning(
                    f"No work sessions recorded for '{task_id}' and no scheduled calendar event found. "
                    f"actual_duration will not be set. "
                    f"Run 'calendar' before completing tasks to enable automatic session tracking."
                )

        if sessions:
            actual = sum((s.duration for s in sessions), timedelta())
            update.metadata[KEY_SESSIONS] = serialize_sessions(sessions, _configured_tz(config))
            update.metadata[KEY_ACTUAL_DURATION] = serialize_duration(actual)

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' marked as done.")
    return rc


def pause_command(args: list[str]) -> int:
    """
    Pause command: Close the currently active work session.

    For the first pause, uses the task's scheduled calendar start as the
    session start time. Subsequent pauses use the active_since timestamp
    set by resume.

    Usage: pause <task_id> [--source <name>]

    Called with no arguments, prompts for a task_id.
    """
    from chronix.core.writer import TaskUpdate
    from chronix.core.models import WorkSession
    from chronix.core.metadata import (
        KEY_ACTIVE_SINCE,
        KEY_SESSIONS,
        serialize_sessions,
    )
    from chronix.cli.interactive_prompts import prompt_task_id

    usage = "Usage: pause <task_id> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("pause")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
    else:
        task_id = remaining[0]
        if _reject_flag_as_task_id(task_id, usage):
            return 1

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    if task.is_paused:
        print_error(f"Task '{task_id}' is already paused.")
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    now = datetime.now(timezone.utc)

    if task.active_since is not None:
        session_start = task.active_since
    else:
        session_start = _get_calendar_task_start(task_id, created=task.created)
        if session_start is None:
            print_error(
                f"Task '{task_id}' has no scheduled calendar event. "
                f"Run 'calendar' first to schedule the task."
            )
            return 1

    new_session = WorkSession(start=session_start, end=now)
    sessions = task.sessions + [new_session]
    prospective_actual = sum((s.duration for s in sessions), timedelta())

    update = TaskUpdate(
        metadata={KEY_SESSIONS: serialize_sessions(sessions, _configured_tz(config))},
        metadata_remove=[KEY_ACTIVE_SINCE],
    )

    if prospective_actual >= task.estimated_duration:
        from chronix.core.metadata import parse_duration

        print_warning(
            f"Pausing '{task_id}' now would bring logged time to "
            f"{format_duration(prospective_actual)}, at or past its "
            f"{format_duration(task.estimated_duration)} estimate."
        )
        choice = console.input(
            "Enter a new duration to extend and pause (e.g. 3h), "
            "'done' to mark it complete instead, or leave blank to cancel: "
        ).strip()

        if not choice:
            print_info("Pause cancelled.")
            return 1

        if choice.lower() == "done":
            done_args = [task_id] + (["--source", doc_token] if doc_token else [])
            return done_command(done_args)

        new_duration = parse_duration(choice)
        if new_duration is None:
            print_error(f"Invalid duration: '{choice}'. Use: 2h, 30m, 2hours, 30minutes")
            return 1
        if new_duration <= prospective_actual:
            print_error(
                f"New duration must exceed {format_duration(prospective_actual)} "
                f"(the logged time this pause would produce)."
            )
            return 1
        update.duration = new_duration

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(
            f"Task '{task_id}' paused. "
            f"Session: {format_duration(new_session.duration)}"
        )
    return rc


def resume_command(args: list[str]) -> int:
    """
    Resume command: Begin a new work session starting at the current time.

    Usage: resume <task_id> [--source <name>]

    Called with no arguments, prompts for a task_id.
    """
    from chronix.core.writer import TaskUpdate
    from chronix.core.metadata import KEY_ACTIVE_SINCE, serialize_active_since
    from chronix.cli.interactive_prompts import prompt_task_id

    usage = "Usage: resume <task_id> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("resume")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
    else:
        task_id = remaining[0]
        if _reject_flag_as_task_id(task_id, usage):
            return 1

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    if task.active_since is not None:
        print_error(f"Task '{task_id}' is already active.")
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    now = datetime.now(timezone.utc)
    update = TaskUpdate(metadata={KEY_ACTIVE_SINCE: serialize_active_since(now, _configured_tz(config))})
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' resumed.")
    return rc


def undone_command(args: list[str]) -> int:
    """
    Undone command: Mark a task as incomplete.

    Usage: undone <task_id> [--source <name>]

    Called with no arguments, prompts for a task_id.
    """
    from chronix.core.writer import TaskUpdate
    from chronix.cli.interactive_prompts import prompt_task_id

    usage = "Usage: undone <task_id> [--source <name>]"
    doc_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("mark incomplete")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
    else:
        task_id = remaining[0]
        if _reject_flag_as_task_id(task_id, usage):
            return 1
    update = TaskUpdate(completed=False)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' marked as incomplete.")
    return rc


def _meta_command_interactive(task_id: str, doc_token: Optional[str] = None) -> int:
    """Interactively collect key=value pairs to set, then optional keys to remove.

    Loops on plain line prompts (a key=value pair per line, blank line to
    finish) rather than the full-screen form, since metadata is an open-ended
    set of pairs rather than a fixed field list.
    """
    from chronix.core.writer import TaskUpdate

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    console.print(f"[cyan]Editing metadata for '{task.title}' (id={task_id})[/cyan]")
    console.print("[dim]Enter key=value pairs to set, one per line. Blank line to finish.[/dim]")

    metadata: dict[str, str] = {}
    while True:
        try:
            line = console.input("[cyan]key=value:[/cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            break
        if "=" not in line:
            print_error(f"Expected key=value, got: '{line}'")
            continue
        k, _, v = line.partition("=")
        metadata[k.strip()] = v.strip()

    console.print("[dim]Enter keys to remove, one per line. Blank line to finish.[/dim]")
    metadata_remove: list[str] = []
    while True:
        try:
            line = console.input("[cyan]remove key:[/cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            break
        metadata_remove.append(line)

    if not metadata and not metadata_remove:
        print_info("No metadata changes specified. Cancelled.")
        return 1

    update = TaskUpdate(metadata=metadata, metadata_remove=metadata_remove)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' metadata updated.")
    return rc


def meta_command(args: list[str]) -> int:
    """
    Meta command: Set or remove arbitrary metadata fields on a task.

    Usage: meta <task_id> [key=value ...] [--remove key ...] [--source <name>]

    Examples:
        meta abc123 priority=high area=work
        meta abc123 --remove priority

    Called with no arguments, prompts for a task_id then loops asking for
    key=value pairs to set (blank line to finish), one at a time.
    """
    from chronix.core.writer import TaskUpdate
    from chronix.cli.interactive_prompts import prompt_task_id

    usage = "Usage: meta <task_id> [key=value ...] [--remove key ...] [--source <name>]"

    doc_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("edit metadata for")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
        return _meta_command_interactive(task_id, doc_token)

    task_id = remaining[0]
    if _reject_flag_as_task_id(task_id, usage):
        return 1
    rest = remaining[1:]

    metadata: dict[str, str] = {}
    metadata_remove: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--remove":
            if i + 1 >= len(rest):
                print_error("--remove requires a key")
                return 1
            metadata_remove.append(rest[i + 1])
            i += 2
        elif "=" in rest[i]:
            k, _, v = rest[i].partition("=")
            metadata[k.strip()] = v.strip()
            i += 1
        else:
            print_error(f"Expected key=value or --remove, got: '{rest[i]}'")
            return 1

    if not metadata and not metadata_remove:
        print_error("No metadata changes specified.")
        console.print(f"[dim]{usage}[/dim]")
        return 1

    update = TaskUpdate(metadata=metadata, metadata_remove=metadata_remove)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' metadata updated.")
    return rc


def delete_command(args: list[str]) -> int:
    """
    Delete command: Remove a task from its source.

    Usage: delete <task_id> [--source <name>]

    Called with no arguments, prompts for a task_id, shows the task, and
    asks for confirmation before deleting.
    """
    from chronix.core.writer import TaskNotFoundError
    from chronix.cli.interactive_prompts import confirm, prompt_task_id

    usage = "Usage: delete <task_id> [--source <name>]"
    source_token, remaining = _parse_edit_flags(args)

    if not remaining:
        task_id = prompt_task_id("delete")
        if task_id is None:
            print_warning("Cancelled: a task ID is required.")
            return 1
        task = _find_task_in_context(task_id)
        if task is not None:
            console.print(f"  [yellow]{task.title}[/yellow] [dim](id={task_id})[/dim]")
        if not confirm(f"Delete task '{task_id}'?"):
            print_info("Cancelled.")
            return 1
    else:
        task_id = remaining[0]
        if _reject_flag_as_task_id(task_id, usage):
            return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    source = _resolve_edit_source(task_id, source_token, config)
    if source is None:
        if not config.all_sources():
            print_error("No sources configured.")
        else:
            print_error("Multiple sources configured for this task's project. Specify one with --source <name>")
            for s in config.all_sources():
                console.print(f"  [cyan]{s.label()}[/cyan]")
        return 1

    try:
        writer = _get_task_writer(_configured_tz(config), source_type=source.type)
        writer.delete_task(source.source_id, task_id)
        _resync_project_sources(source.project_name, config)
        print_success(f"Task '{task_id}' deleted.")
        return 0
    except TaskNotFoundError:
        print_error(f"No task with id='{task_id}' found.")
        return 1
    except Exception as e:
        print_error(f"Delete failed: {e}")
        return 1
