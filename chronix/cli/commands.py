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
    print_conflicts,
    print_task_details,
    print_task_position,
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


def _generate_today_schedule(time_override: Optional[str] = None) -> tuple[DaySchedule, datetime, datetime]:
    """
    Generate today's schedule (shared logic for `today` and `calendar` commands).
    
    Returns:
        Tuple of (day_schedule, work_start, work_end)
        
    Raises:
        RuntimeError: If no projects loaded
        ValueError: If time override format is invalid
    """
    if not _context.projects:
        raise RuntimeError("No projects loaded. Run 'sync' first.")
    
    from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window, get_work_windows
    
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
    task_pool = aggregator.get_task_pool(aggregated_tasks)
    incomplete_tasks = [t for t in task_pool if not t.completed]
    
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

    # Get blocked time from config
    blocked_time = config_to_time_blocks(config, today)

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
    
    # Schedule tasks
    filtered_blocked = [
        block for block in blocked_time
        if block.start < work_end and block.end > work_start
    ]
    
    scheduler = SchedulingEngine()
    day_schedule = scheduler.schedule_tasks(
        tasks=incomplete_tasks,
        start_time=work_start,
        blocked_time=filtered_blocked
    )
    
    # Filter to today's tasks
    today_scheduled_tasks = [
        st for st in day_schedule.scheduled_tasks
        if st.start.date() == today
    ]
    
    day_schedule = DaySchedule(
        date=day_schedule.date,
        scheduled_tasks=today_scheduled_tasks,
        blocked_time=day_schedule.blocked_time,
        conflicts=day_schedule.conflicts
    )
    
    return day_schedule, work_start, work_end


class ChronixContext:
    """Shared context for chronix commands."""

    def __init__(self):
        self.projects: list[ProjectTodoList] = []
        self.ad_hoc_meetings: list = []
        self.last_sync: Optional[datetime] = None
        self.google_client: Optional[GoogleDocsClient] = None
        self.config: Optional['ChronixConfig'] = None

    def _ensure_google_client(self) -> GoogleDocsClient:
        """Lazy initialize Google Docs client."""
        if self.google_client is None:
            self.google_client = GoogleDocsClient()
        return self.google_client


# Global context instance
_context = ChronixContext()


def _resolve_document_token(token: str, config: 'ChronixConfig') -> Optional[str]:
    """
    Resolve a token (alias or document_id) to its canonical document_id.

    Returns the document_id if the token matches a configured alias or ID, else None.
    """
    return config.google_docs.resolve(token)


def sync_command(args: list[str]) -> int:
    """
    Sync command: Fetch and parse configured project documents.
    
    Usage: sync [id|alias ...]
    
    With no argument: syncs all configured documents (continues on document-level failures)
    With one or more id/alias tokens: syncs only those documents (all must be configured)
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

        all_document_ids = config.google_docs.document_ids
        if not all_document_ids:
            print_warning("No documents configured in your config file.")
            console.print(f"Edit [cyan]{ChronixConfig.get_default_path()}[/cyan] and add document_ids to sync.")
            return 1

        # If specific tokens were requested, resolve and validate all before fetching any
        if tokens:
            unknown = [t for t in tokens if _resolve_document_token(t, config) is None]
            if unknown:
                for token in unknown:
                    print_error(f"Unknown document '{token}'")
                configured_labels = [
                    config.google_docs.format_document_label(doc_id)
                    for doc_id in all_document_ids
                ]
                console.print("Configured documents:")
                for label in configured_labels:
                    console.print(f"  [cyan]{label}[/cyan]")
                return 1
            # Resolve tokens to canonical document IDs (deduplicated, preserving order)
            seen: set[str] = set()
            document_ids: list[str] = []
            for t in tokens:
                resolved = _resolve_document_token(t, config)
                if resolved not in seen:
                    seen.add(resolved)
                    document_ids.append(resolved)
        else:
            document_ids = all_document_ids

        # Initialize client and authenticate (global failure)
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

        # Sync each document with retry logic
        from chronix.cli.sync_helpers import _sync_single_document_with_retries
        
        projects = []
        all_meetings = []
        results = []

        for doc_id in document_ids:
            alias = config.google_docs.get_alias(doc_id)
            result, project, meetings = _sync_single_document_with_retries(doc_id, client, alias=alias)
            results.append(result)
            
            if result.outcome.value == "success":
                projects.append(project)
                all_meetings.extend(meetings)

        # Update context: merge or replace
        if tokens:
            # Partial sync: merge into existing context
            if _context.projects:
                synced_doc_ids = {p.project_context.document_id for p in projects}
                updated_projects = [
                    p for p in _context.projects
                    if p.project_context.document_id not in synced_doc_ids
                ]
                updated_projects.extend(projects)
                _context.projects = updated_projects
                _context.ad_hoc_meetings.extend(all_meetings)
            else:
                _context.projects = projects
                _context.ad_hoc_meetings = all_meetings
                print_warning("Synced specific documents with no prior context.")
                console.print("[dim]For complete task aggregation across all documents, run:[/dim]")
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
            num_projects=len(projects),
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

    Usage: today [HH:MM]
    
    Optional HH:MM argument specifies the start time for scheduling today.
    If not provided, uses current time. Times are in 24-hour format.
    """
    try:
        # Validate arguments
        if len(args) > 1:
            print_error(f"today command takes at most 1 argument, got {len(args)}")
            return 1
        
        time_override = args[0] if args else None
        
        console.print("[dim]Generating today's schedule...[/dim]")
        
        day_schedule, work_start, work_end = _generate_today_schedule(time_override)
        
        # Display schedule
        print_schedule_header(day_schedule.date, work_start, work_end, "UTC")
        
        _display_continuous_timeline(day_schedule, work_start, work_end)
        
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

    Usage: calendar [HH:MM] [--force]
    
    Optional HH:MM argument specifies the start time for scheduling today.
    --force flag allows overwriting conflicting non-Chronix calendar events.
    """
    try:
        # Parse arguments
        time_override = None
        force = False
        
        for arg in args:
            if arg == '--force':
                force = True
            elif arg.startswith('--'):
                print_error(f"Unknown flag: {arg}")
                return 1
            else:
                if time_override is not None:
                    print_error(f"calendar command takes at most 1 time argument")
                    return 1
                time_override = arg
        
        console.print("[dim]Generating and syncing today's schedule to Google Calendar...[/dim]")
        
        day_schedule, work_start, work_end = _generate_today_schedule(time_override)
        
        # Sync to Google Calendar
        from chronix.integrations.google_calendar import CalendarSyncService
        sync_service = CalendarSyncService()
        
        sync_result = sync_service.sync(
            day_schedule=day_schedule,
            sync_start=work_start,
            sync_end=work_end,
            force=force
        )
        
        if not sync_result.success:
            if sync_result.conflicts:
                print_error("Calendar sync failed due to conflicting events:")
                for conflict in sync_result.conflicts:
                    print_error(f"  - {conflict.calendar_event_title} ({conflict.calendar_event_start} - {conflict.calendar_event_end})")
                    print_error(f"    conflicts with {conflict.chronix_task_title}")
                print_info("Rerun with --force to overwrite, or resolve conflicts manually.")
            else:
                print_error(f"Calendar sync failed: {sync_result.error_message}")
            return 1
        
        # Print sync summary
        print_success(f"Calendar sync completed:")
        print_info(f"  Created: {sync_result.created_count} events")
        print_info(f"  Updated: {sync_result.updated_count} events")
        print_info(f"  Deleted: {sync_result.deleted_count} events")
        print_info(f"  Shortened: {sync_result.shortened_count} events")
        print()
        
        # Display schedule (same as today command)
        print_schedule_header(day_schedule.date, work_start, work_end, "UTC")
        
        _display_continuous_timeline(day_schedule, work_start, work_end)
        
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
        print_error(f"Failed to sync calendar: {str(e)}")
        return 1
    except Exception as e:
        print_error(f"Failed to sync calendar: {e}")
        import traceback
        traceback.print_exc()
        return 1


def schedule_command(args: list[str]) -> int:
    """
    Schedule command: Display schedule for multiple days.

    Usage: schedule [days]
    
    If days is not specified, schedules all tasks until completion (no day limit).
    If days is specified, limits scheduling to that number of days.
    """
    try:
        if not _context.projects:
            print_warning("No projects loaded. Run 'sync' first.")
            return 1

        # Parse number of days
        num_days: int | None = None  # None means unlimited
        if args:
            if len(args) > 1:
                print_error("Usage: schedule [days] - too many arguments")
                return 1
            try:
                num_days = int(args[0])
                if num_days < 1:
                    print_error("Number of days must be positive")
                    return 1
            except ValueError:
                print_error(f"Invalid number of days: {args[0]}")
                return 1

        if num_days is None:
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
        task_pool = aggregator.get_task_pool(aggregated_tasks)

        # Filter incomplete tasks only
        incomplete_tasks = [t for t in task_pool if not t.completed]

        # Get current time
        now = datetime.now(tz)
        
        # Adjust start time if we're past work start today
        first_day_start, _ = get_work_window(config, now.date())
        start_time = max(now, first_day_start)
        
        # Schedule continuously across all days
        scheduler = SchedulingEngine()
        
        def get_daily_blocked_time(day_date: date) -> list:
            """Get blocked time for a specific day."""
            blocked = config_to_time_blocks(config, day_date)
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
        
        schedules_by_day = scheduler.schedule_continuous(
            tasks=incomplete_tasks,
            start_time=start_time,
            num_days=num_days,
            daily_blocked_time_fn=get_daily_blocked_time
        )
        
        # Display each day's schedule with continuous task numbering
        all_conflicts = []
        task_counter = 1  # Global task counter across all days (starts at 1)
        for day_offset, day_date in enumerate(sorted(schedules_by_day.keys())):
            # Break after num_days if specified
            if num_days is not None and day_offset >= num_days:
                break
            
            if day_date not in schedules_by_day:
                continue
            
            day_schedule = schedules_by_day[day_date]
            
            # Get work window for display
            work_start, work_end = get_work_window(config, day_date)
            if day_offset == 0 and now > work_start:
                work_start = now
            
            # Display separator between days
            if day_offset > 0:
                console.print("\n" + "─" * 60 + "\n")
            
            print_schedule_header(day_schedule.date, work_start, work_end, config.scheduling.timezone)
            task_counter = _display_continuous_timeline(day_schedule, work_start, work_end, start_index=task_counter)
            
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


def documents_command(args: list[str]) -> int:
    """
    Documents command: List all configured documents.
    
    Usage: documents
    """
    if args:
        print_error("documents command takes no arguments")
        return 1
    
    try:
        from chronix.config import ChronixConfig
        
        config = ChronixConfig.load_or_default()
        documents = config.google_docs.documents
        
        if not documents:
            print_warning("No documents configured in your config file.")
            console.print(f"Edit [cyan]{ChronixConfig.get_default_path()}[/cyan] and add document_ids.")
            return 0
        
        console.print()
        console.print("[bold]Configured documents:[/bold]")
        console.print()
        
        # Get document titles from context if available (from previous sync)
        doc_titles = {}
        if _context.projects:
            doc_titles = {
                p.project_context.document_id: p.project_context.project_name
                for p in _context.projects
                if p.project_context.document_id
            }
        
        for doc_config in documents:
            doc_id = doc_config.document_id
            title = doc_titles.get(doc_id, "(not synced yet)")
            if doc_config.alias:
                console.print(f"  [cyan]{doc_config.alias}[/cyan] [dim]({doc_id})[/dim]  {title}")
            else:
                console.print(f"  [cyan]{doc_id}[/cyan]  {title}")
        
        console.print()
        console.print(f"Use [cyan]sync <id|alias> [id|alias ...][/cyan] to sync specific documents")
        console.print()
        return 0
    
    except Exception as e:
        print_error(f"Failed to list documents: {e}")
        return 1


def tabs_command(args: list[str]) -> int:
    """
    Tabs command: List the tabs in a document, for use with add's --tab flag.

    Usage: tabs <id|alias>
    """
    if len(args) != 1:
        print_error("Usage: tabs <id|alias>")
        return 1

    doc_token = args[0]

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    doc_id = _resolve_document_token(doc_token, config)
    if doc_id is None:
        print_error(f"Unknown document: '{doc_token}'")
        return 1

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


def help_command(args: list[str]) -> int:
    """
    Help command: Show available commands.
    
    Usage: help
    """
    console.print()
    console.print("[bold]Available commands:[/bold]")
    console.print()
    
    commands_table = [
        ("add <duration> <title> [--doc <id|alias>] [--tab <title|id>]", "Create a new task in a Google Docs document"),
        ("calendar [HH:MM] [--force]", "Sync today's schedule to Google Calendar"),
        ("config <cmd>", "Manage configuration (init, show, path, validate)"),
        ("deadline <task_id> <ISO|-> [--user] [--doc <id|alias>]", "Set external deadline; --user sets user deadline instead"),
        ("delete <task_id> [--doc <id|alias>]", "Delete a task from its document"),
        ("documents", "List all configured documents with aliases"),
        ("tabs <id|alias>", "List the tabs in a document (for add's --tab flag)"),
        ("done <task_id> [--doc <id|alias>]", "Complete a task, recording actual duration from sessions"),
        ("pause <task_id> [--doc <id|alias>]", "Close the current work session"),
        ("resume <task_id> [--doc <id|alias>]", "Open a new work session starting now"),
        ("duration <task_id> <dur> [--doc <id|alias>]", "Change a task's estimated duration (e.g. 2h, 30m)"),
        ("explain <task_id>", "Show details and scheduling info for a task"),
        ("meta <task_id> [k=v ...] [--remove k] [--doc <id|alias>]", "Set or remove arbitrary metadata fields"),
        ("mode <task_id> <mode> [--doc <id|alias>]", "Set execution mode (atomic|flex|contiguous_preferred)"),
        ("rename <task_id> <title> [--doc <id|alias>]", "Rename a task"),
        ("schedule [days]", "Display multi-day schedule (default: unlimited days)"),
        ("sync", "Fetch and parse all configured documents"),
        ("sync <id|alias> [...]", "Sync one or more specific documents by ID or alias"),
        ("today [HH:MM]", "Display today's scheduled tasks from optional start time"),
        ("undone <task_id> [--doc <id|alias>]", "Mark a task as incomplete"),
        ("update <task_id> [flags] [--doc <id|alias>]", "Update fields: --title --duration --external-deadline --user-deadline --mode --meta --remove-meta"),
        ("clear / cls", "Clear the terminal screen"),
        ("help", "Show this help message"),
        ("exit / quit", "Exit the interactive shell"),
    ]
    
    for cmd, desc in commands_table:
        console.print(f"  [cyan]{cmd:52}[/cyan] [dim]{desc}[/dim]")
    
    console.print()
    console.print("[bold]--doc resolution:[/bold]")
    console.print("  In this interactive shell, --doc is optional for task commands once a")
    console.print("  document has been synced; chronix resolves it from the task's ID automatically.")
    console.print("  In one-shot mode (chronix <cmd> ...), done/pause/resume require --doc")
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
    Add command: Create a new task in a Google Docs document.

    Usage: add <duration> <title> [--doc <id|alias>] [--tab <title|id>]

    Duration examples: 2h, 30m, 2hours, 30minutes

    Without --tab, the task is inserted into the first tab that has a
    TASKS section (matching prior behavior).
    """
    doc_token: Optional[str] = None
    tab_token: Optional[str] = None
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--doc" and i + 1 < len(args):
            doc_token = args[i + 1]
            i += 2
        elif args[i] == "--tab" and i + 1 < len(args):
            tab_token = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            print_error(f"Unknown flag: {args[i]}")
            return 1
        else:
            remaining.append(args[i])
            i += 1

    if len(remaining) < 2:
        print_error("Usage: add <duration> <title> [--doc <id|alias>] [--tab <title|id>]")
        return 1

    duration_str = remaining[0]
    title = " ".join(remaining[1:])

    try:
        duration = _parse_add_duration(duration_str)
    except ValueError as e:
        print_error(str(e))
        return 1

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    all_doc_ids = config.google_docs.document_ids
    if not all_doc_ids:
        print_error("No documents configured. Run 'chronix config init' to set up.")
        return 1

    if doc_token is not None:
        doc_id = _resolve_document_token(doc_token, config)
        if doc_id is None:
            print_error(f"Unknown document: '{doc_token}'")
            return 1
    elif len(all_doc_ids) == 1:
        doc_id = all_doc_ids[0]
    else:
        print_error("Multiple documents configured. Specify one with --doc <id|alias>")
        for doc in config.google_docs.documents:
            label = f"{doc.alias} ({doc.document_id})" if doc.alias else doc.document_id
            console.print(f"  [cyan]{label}[/cyan]")
        return 1

    from chronix.core.models import generate_task_id
    from chronix.core.writer import NewTask
    from chronix.integrations.google_docs.writer import GoogleDocsTaskWriter

    try:
        client = _context._ensure_google_client()
        writer = GoogleDocsTaskWriter(auth_strategy=client.auth_strategy)
        task_id = generate_task_id()
        writer.create_task(doc_id, NewTask(title=title, duration=duration, id=task_id, tab=tab_token))
        _resync_document(doc_id, config)
        print_success(f"Task added: {title} ({duration_str}) [id={task_id}]")
        return 0
    except Exception as e:
        print_error(f"Failed to add task: {e}")
        return 1


def _display_continuous_timeline(day_schedule, work_start: datetime, work_end: datetime, start_index: int = 1) -> int:
    """
    Display a continuous timeline including tasks, blocked time, and empty slots.
    
    Args:
        day_schedule: The day's schedule to display
        work_start: Start of work day
        work_end: End of work day
        start_index: Starting index for task numbering (for continuous numbering across days)
    
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
        # If there's a gap before this segment, add an empty slot
        if current_time < segment['start']:
            timeline.append({
                'start': current_time,
                'end': segment['start'],
                'type': 'empty',
                'data': None
            })
        
        # Add the segment
        timeline.append(segment)
        current_time = segment['end']
    
    # If there's time remaining until work_end, add final empty slot
    if current_time < work_end:
        timeline.append({
            'start': current_time,
            'end': work_end,
            'type': 'empty',
            'data': None
        })
    
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


def _get_task_writer():
    from chronix.integrations.google_docs.writer import GoogleDocsTaskWriter
    client = _context._ensure_google_client()
    return GoogleDocsTaskWriter(auth_strategy=client.auth_strategy)


def _resync_document(doc_id: str, config) -> None:
    """Refresh in-memory task state for a single document after a write.

    Keeps `_context.projects` consistent with the document just written to,
    so a task just created or edited is immediately visible to subsequent
    commands without requiring an explicit `sync`. Never raises: a refresh
    failure here must not be mistaken for the write itself failing.
    """
    from chronix.cli.sync_helpers import _sync_single_document_with_retries

    try:
        client = _context._ensure_google_client()
        alias = config.google_docs.get_alias(doc_id)
        result, project, _meetings = _sync_single_document_with_retries(doc_id, client, alias=alias)
    except Exception:
        print_warning("Could not refresh local state for this document. Run 'sync' to pick up the change.")
        return

    if result.outcome.value != "success":
        print_warning("Could not refresh local state for this document. Run 'sync' to pick up the change.")
        return

    _context.projects = [
        p for p in _context.projects if p.project_context.document_id != doc_id
    ] + [project]
    _context.last_sync = datetime.now(timezone.utc)
    _context.config = config


def _find_document_for_task(task_id: str) -> Optional[str]:
    """Return the document_id of the currently-synced project containing task_id.

    Only looks at in-memory `_context.projects`, so this is only useful after
    a sync has populated it (e.g. the REPL's startup sync, or a preceding
    `sync` in the same session). Returns None if not found there.
    """
    for project in _context.projects:
        for task in project.tasks:
            if task.id == task_id:
                return project.project_context.document_id
    return None


def _resolve_edit_doc(task_id: str, doc_token: Optional[str], config) -> Optional[str]:
    all_doc_ids = config.google_docs.document_ids
    if not all_doc_ids:
        return None
    if doc_token is not None:
        return _resolve_document_token(doc_token, config)
    found = _find_document_for_task(task_id)
    if found is not None:
        return found
    if len(all_doc_ids) == 1:
        return all_doc_ids[0]
    return None


def _parse_edit_flags(args: list[str]) -> tuple[Optional[str], list[str]]:
    """Extract --doc <token> from args. Returns (doc_token, remaining_args)."""
    doc_token: Optional[str] = None
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--doc" and i + 1 < len(args):
            doc_token = args[i + 1]
            i += 2
        else:
            remaining.append(args[i])
            i += 1
    return doc_token, remaining


def _run_task_update(task_id: str, update, doc_token: Optional[str] = None) -> int:
    from chronix.core.writer import TaskNotFoundError
    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    doc_id = _resolve_edit_doc(task_id, doc_token, config)
    if doc_id is None:
        if not config.google_docs.document_ids:
            print_error("No documents configured.")
        else:
            print_error("Multiple documents configured. Specify one with --doc <id|alias>")
            for doc in config.google_docs.documents:
                label = f"{doc.alias} ({doc.document_id})" if doc.alias else doc.document_id
                console.print(f"  [cyan]{label}[/cyan]")
        return 1

    try:
        writer = _get_task_writer()
        writer.update_task(doc_id, task_id, update)
        _resync_document(doc_id, config)
        return 0
    except TaskNotFoundError:
        print_error(f"No task with id='{task_id}' found.")
        return 1
    except Exception as e:
        print_error(f"Update failed: {e}")
        return 1


def update_command(args: list[str]) -> int:
    """
    Update command: Modify one or more fields of a task by its ID.

    Usage: update <task_id> [--title <title>] [--duration <duration>]
                            [--external-deadline <ISO|->] [--user-deadline <ISO|->]
                            [--mode <mode>] [--meta <key=value> ...]
                            [--remove-meta <key> ...]
                            [--doc <id|alias>]

    At least one field flag must be supplied.
    """
    from chronix.core.metadata import parse_deadline, parse_duration
    from chronix.core.writer import TaskUpdate

    if not args:
        print_error("Usage: update <task_id> [--title ...] [--duration ...] [--external-deadline ...] "
                    "[--user-deadline ...] [--mode ...] [--meta key=value ...] [--remove-meta key ...] "
                    "[--doc <id|alias>]")
        return 1

    task_id = args[0]
    doc_token, flags = _parse_edit_flags(args[1:])

    update = TaskUpdate()
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "--title":
            if i + 1 >= len(flags):
                print_error("--title requires a value")
                return 1
            update.title = flags[i + 1]
            i += 2
        elif flag == "--duration":
            if i + 1 >= len(flags):
                print_error("--duration requires a value")
                return 1
            dur = parse_duration(flags[i + 1])
            if dur is None:
                print_error(f"Invalid duration: '{flags[i + 1]}'")
                return 1
            update.duration = dur
            i += 2
        elif flag == "--external-deadline":
            if i + 1 >= len(flags):
                print_error("--external-deadline requires a value")
                return 1
            try:
                update.external_deadline = parse_deadline(flags[i + 1])
            except ValueError as e:
                print_error(str(e))
                return 1
            i += 2
        elif flag == "--user-deadline":
            if i + 1 >= len(flags):
                print_error("--user-deadline requires a value")
                return 1
            try:
                update.user_deadline = parse_deadline(flags[i + 1])
            except ValueError as e:
                print_error(str(e))
                return 1
            i += 2
        elif flag == "--mode":
            if i + 1 >= len(flags):
                print_error("--mode requires a value")
                return 1
            update.mode = flags[i + 1]
            i += 2
        elif flag == "--meta":
            if i + 1 >= len(flags):
                print_error("--meta requires key=value")
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
            print_error(f"Unknown flag: {flag}")
            return 1

    if not any([
        update.title,
        update.duration,
        update.has_external_deadline_change(),
        update.has_user_deadline_change(),
        update.mode,
        update.metadata,
        update.metadata_remove,
        update.completed is not None,
    ]):
        print_error("No fields to update. Provide at least one flag.")
        return 1

    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' updated.")
    return rc


def rename_command(args: list[str]) -> int:
    """
    Rename command: Change the title of a task.

    Usage: rename <task_id> <new title> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskUpdate

    doc_token, remaining = _parse_edit_flags(args)
    if len(remaining) < 2:
        print_error("Usage: rename <task_id> <new title> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    new_title = " ".join(remaining[1:])
    update = TaskUpdate(title=new_title)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' renamed to '{new_title}'.")
    return rc


def duration_command(args: list[str]) -> int:
    """
    Duration command: Update the estimated duration of a task.

    Usage: duration <task_id> <duration> [--doc <id|alias>]

    Duration examples: 2h, 30m, 2hours, 30minutes
    """
    from chronix.core.metadata import parse_duration
    from chronix.core.writer import TaskUpdate

    doc_token, remaining = _parse_edit_flags(args)
    if len(remaining) < 2:
        print_error("Usage: duration <task_id> <duration> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    dur = parse_duration(remaining[1])
    if dur is None:
        print_error(f"Invalid duration: '{remaining[1]}'")
        return 1

    update = TaskUpdate(duration=dur)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' duration set to '{remaining[1]}'.")
    return rc


def deadline_command(args: list[str]) -> int:
    """
    Deadline command: Set the external or user deadline of a task.

    Usage: deadline <task_id> <ISO-date|-> [--user] [--doc <id|alias>]

    Without --user, updates external_deadline.
    With --user, updates user_deadline.
    Use '-' to clear a deadline.
    """
    from chronix.core.metadata import parse_deadline
    from chronix.core.writer import TaskUpdate

    use_user = "--user" in args
    args = [a for a in args if a != "--user"]
    doc_token, remaining = _parse_edit_flags(args)

    if len(remaining) < 2:
        print_error("Usage: deadline <task_id> <ISO-date|-> [--user] [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    try:
        dt = parse_deadline(remaining[1])
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


def mode_command(args: list[str]) -> int:
    """
    Mode command: Set the execution mode of a task.

    Usage: mode <task_id> <atomic|flex|contiguous_preferred> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskUpdate

    VALID_MODES = {"atomic", "flex", "contiguous_preferred"}
    doc_token, remaining = _parse_edit_flags(args)

    if len(remaining) < 2:
        print_error("Usage: mode <task_id> <atomic|flex|contiguous_preferred> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    new_mode = remaining[1]
    if new_mode not in VALID_MODES:
        print_error(f"Invalid mode '{new_mode}'. Valid: atomic, flex, contiguous_preferred")
        return 1

    update = TaskUpdate(mode=new_mode)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' mode set to '{new_mode}'.")
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


def _get_calendar_task_start(task_id: str) -> Optional[datetime]:
    """Look up the scheduled calendar start time for a task, or return None."""
    try:
        from chronix.integrations.google_calendar import CalendarSyncService
        sync_service = CalendarSyncService()
        search_start = datetime.now(timezone.utc) - timedelta(days=14)
        search_end = datetime.now(timezone.utc)
        return sync_service.find_task_scheduled_start(task_id, search_start, search_end)
    except Exception:
        return None


def done_command(args: list[str]) -> int:
    """
    Done command: Mark a task as complete, recording actual work duration.

    Closes any active session, computes actual_duration from all sessions,
    and marks the task complete. If no sessions exist and a calendar event
    is found, a session from the calendar start to now is recorded.

    Usage: done <task_id> [--doc <id|alias>]
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

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: done <task_id> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    now = datetime.now(timezone.utc)

    update = TaskUpdate(completed=True)

    task = _find_task_in_context(task_id)
    if task is not None:
        sessions = list(task.sessions)

        if task.active_since is not None:
            sessions.append(WorkSession(start=task.active_since, end=now))
            update.metadata_remove.append(KEY_ACTIVE_SINCE)
        elif not sessions:
            calendar_start = _get_calendar_task_start(task_id)
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
            update.metadata[KEY_SESSIONS] = serialize_sessions(sessions)
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

    Usage: pause <task_id> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskUpdate
    from chronix.core.models import WorkSession
    from chronix.core.metadata import (
        KEY_ACTIVE_SINCE,
        KEY_SESSIONS,
        serialize_sessions,
    )

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: pause <task_id> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    if task.is_paused:
        print_error(f"Task '{task_id}' is already paused.")
        return 1

    now = datetime.now(timezone.utc)

    if task.active_since is not None:
        session_start = task.active_since
    else:
        session_start = _get_calendar_task_start(task_id)
        if session_start is None:
            print_error(
                f"Task '{task_id}' has no scheduled calendar event. "
                f"Run 'calendar' first to schedule the task."
            )
            return 1

    new_session = WorkSession(start=session_start, end=now)
    sessions = task.sessions + [new_session]

    update = TaskUpdate(
        metadata={KEY_SESSIONS: serialize_sessions(sessions)},
        metadata_remove=[KEY_ACTIVE_SINCE],
    )
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

    Usage: resume <task_id> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskUpdate
    from chronix.core.metadata import KEY_ACTIVE_SINCE, serialize_active_since

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: resume <task_id> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]

    task = _find_task_in_context(task_id)
    if task is None:
        print_error(f"Task '{task_id}' not found. Run 'sync' first.")
        return 1

    if task.active_since is not None:
        print_error(f"Task '{task_id}' is already active.")
        return 1

    now = datetime.now(timezone.utc)
    update = TaskUpdate(metadata={KEY_ACTIVE_SINCE: serialize_active_since(now)})
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' resumed.")
    return rc


def undone_command(args: list[str]) -> int:
    """
    Undone command: Mark a task as incomplete.

    Usage: undone <task_id> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskUpdate

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: undone <task_id> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
    update = TaskUpdate(completed=False)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' marked as incomplete.")
    return rc


def meta_command(args: list[str]) -> int:
    """
    Meta command: Set or remove arbitrary metadata fields on a task.

    Usage: meta <task_id> [key=value ...] [--remove key ...] [--doc <id|alias>]

    Examples:
        meta abc123 priority=high area=work
        meta abc123 --remove priority
    """
    from chronix.core.writer import TaskUpdate

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: meta <task_id> [key=value ...] [--remove key ...] [--doc <id|alias>]")
        return 1

    task_id = remaining[0]
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
        return 1

    update = TaskUpdate(metadata=metadata, metadata_remove=metadata_remove)
    rc = _run_task_update(task_id, update, doc_token)
    if rc == 0:
        print_success(f"Task '{task_id}' metadata updated.")
    return rc


def delete_command(args: list[str]) -> int:
    """
    Delete command: Remove a task from its document.

    Usage: delete <task_id> [--doc <id|alias>]
    """
    from chronix.core.writer import TaskNotFoundError

    doc_token, remaining = _parse_edit_flags(args)
    if not remaining:
        print_error("Usage: delete <task_id> [--doc <id|alias>]")
        return 1

    task_id = remaining[0]

    try:
        from chronix.config import ChronixConfig
        config = _context.config or ChronixConfig.load_or_default()
    except Exception as e:
        print_error(f"Failed to load configuration: {e}")
        return 1

    doc_id = _resolve_edit_doc(task_id, doc_token, config)
    if doc_id is None:
        if not config.google_docs.document_ids:
            print_error("No documents configured.")
        else:
            print_error("Multiple documents configured. Specify one with --doc <id|alias>")
            for doc in config.google_docs.documents:
                label = f"{doc.alias} ({doc.document_id})" if doc.alias else doc.document_id
                console.print(f"  [cyan]{label}[/cyan]")
        return 1

    try:
        writer = _get_task_writer()
        writer.delete_task(doc_id, task_id)
        _resync_document(doc_id, config)
        print_success(f"Task '{task_id}' deleted.")
        return 0
    except TaskNotFoundError:
        print_error(f"No task with id='{task_id}' found.")
        return 1
    except Exception as e:
        print_error(f"Delete failed: {e}")
        return 1
