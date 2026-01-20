"""Command implementations for the chronix CLI."""

from datetime import datetime, timezone, timedelta, date
from typing import Optional
from pathlib import Path
import json

from chronix.integrations.google_docs.client import GoogleDocsClient
from chronix.integrations.google_docs.parser import GoogleDocsParser
from chronix.core.todo import TodoDeriver
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


class ChronixContext:
    """Shared context for chronix commands."""

    def __init__(self):
        self.projects: list[ProjectTodoList] = []
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


def sync_command(args: list[str]) -> int:
    """
    Sync command: Fetch and parse all configured project documents.
    
    Usage: sync
    """
    try:
        console.print("[dim]Starting sync...[/dim]")

        # Load configuration
        from chronix.config import ChronixConfig

        try:
            config = ChronixConfig.load_or_default()
        except Exception as e:
            print_error(f"Failed to load configuration: {e}")
            console.print("Run [cyan]chronix config init[/cyan] to create a default configuration.")
            return 1

        document_ids = config.google_docs.document_ids
        if not document_ids:
            print_warning("No documents configured in your config file.")
            console.print(f"Edit [cyan]{ChronixConfig.get_default_path()}[/cyan] and add document_ids to sync.")
            return 1

        # Initialize client and authenticate
        client = _context._ensure_google_client()
        console.print("[dim]Authenticating with Google Docs...[/dim]")

        if not client.authenticate():
            print_error("Authentication failed. Please check your credentials.")
            return 1

        console.print("[dim]✓ Authenticated successfully[/dim]")

        # Fetch and parse documents
        projects = []
        parser = GoogleDocsParser()
        deriver = TodoDeriver()

        for doc_id in document_ids:
            console.print(f"[dim]Fetching document {doc_id}...[/dim]")

            try:
                doc = client.fetch_document(doc_id)
                doc_structure = parser.parse_document(doc)

                project_name = doc_structure.title

                tasks = deriver.derive_todo_list(doc_structure.to_dict())

                project_todo = ProjectTodoList(
                    project_name=project_name,
                    tasks=tasks,
                    document_id=doc_id
                )
                projects.append(project_todo)

                console.print(f"  [green]✓[/green] [bold]{project_name}[/bold]: [cyan]{len(tasks)}[/cyan] tasks")

            except Exception as e:
                console.print(f"  [red]✗[/red] Failed to fetch document {doc_id}: {e}")
                continue

        # Update context
        _context.projects = projects
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
            completed_tasks=completed_tasks
        )

        return 0

    except Exception as e:
        print_error(f"Sync failed: {e}")
        return 1


def today_command(args: list[str]) -> int:
    """
    Today command: Display today's scheduled tasks.

    Usage: today
    """
    try:
        if not _context.projects:
            print_warning("No projects loaded. Run 'sync' first.")
            return 1

        console.print("[dim]Generating today's schedule...[/dim]")

        # Load configuration
        from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window
        from zoneinfo import ZoneInfo

        config = _context.config or ChronixConfig.load_or_default()
        tz = ZoneInfo(config.scheduling.timezone)

        # Aggregate all tasks
        aggregator = TaskAggregator()
        aggregated_tasks = aggregator.aggregate(_context.projects)
        task_pool = aggregator.get_task_pool(aggregated_tasks)

        # Filter incomplete tasks only
        incomplete_tasks = [t for t in task_pool if not t.completed]

        # Get today's date and time
        now = datetime.now(tz)
        today = now.date()

        # Get work window from config
        work_start, work_end = get_work_window(config, today)

        # If it's already past work start, use current time
        if now > work_start:
            work_start = now

        # Ensure work_end doesn't go past midnight (limit to current day)
        end_of_today = datetime.combine(
            today,
            datetime.max.time(),
            tzinfo=tz
        ).replace(hour=23, minute=59, second=59)

        if work_end > end_of_today:
            work_end = end_of_today

        # Get blocked time from configuration
        blocked_time = config_to_time_blocks(config, today)

        # Filter blocked time to only include blocks within work hours
        blocked_time = [
            block for block in blocked_time
            if block.start < work_end and block.end > work_start
        ]

        # Schedule tasks
        scheduler = SchedulingEngine()
        day_schedule = scheduler.schedule_tasks(
            tasks=incomplete_tasks,
            start_time=work_start,
            blocked_time=blocked_time
        )
        
        # Filter scheduled tasks to only include segments within today
        # This prevents showing tasks that spill into tomorrow
        today_scheduled_tasks = [
            st for st in day_schedule.scheduled_tasks
            if st.start.date() == today
        ]
        
        # Update the day_schedule with filtered tasks
        day_schedule = DaySchedule(
            date=day_schedule.date,
            scheduled_tasks=today_scheduled_tasks,
            blocked_time=day_schedule.blocked_time,
            conflicts=day_schedule.conflicts
        )

        # Display schedule
        print_schedule_header(day_schedule.date, work_start, work_end, config.scheduling.timezone)
        
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
    
    except Exception as e:
        print_error(f"Failed to generate schedule: {e}")
        import traceback
        traceback.print_exc()
        return 1


def schedule_command(args: list[str]) -> int:
    """
    Schedule command: Display schedule for multiple days.

    Usage: schedule [days]
    
    If days is not specified, defaults to 3 days.
    """
    try:
        if not _context.projects:
            print_warning("No projects loaded. Run 'sync' first.")
            return 1

        # Parse number of days
        num_days = 3
        if args:
            try:
                num_days = int(args[0])
                if num_days < 1:
                    print_error("Number of days must be positive")
                    return 1
            except ValueError:
                print_error(f"Invalid number of days: {args[0]}")
                return 1

        console.print(f"[dim]Generating {num_days}-day schedule...[/dim]")

        # Load configuration
        from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window
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
            return config_to_time_blocks(config, day_date)
        
        schedules_by_day = scheduler.schedule_continuous(
            tasks=incomplete_tasks,
            start_time=start_time,
            num_days=num_days,
            daily_blocked_time_fn=get_daily_blocked_time
        )
        
        # Display each day's schedule
        all_conflicts = []
        for day_offset in range(num_days):
            day_date = now.date() + timedelta(days=day_offset)
            
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
            _display_continuous_timeline(day_schedule, work_start, work_end)
            
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


def help_command(args: list[str]) -> int:
    """
    Help command: Show available commands.
    
    Usage: help
    """
    console.print()
    console.print("[bold]Available commands:[/bold]")
    console.print()
    
    commands_table = [
        ("sync", "Fetch and parse all configured project documents"),
        ("today", "Display today's scheduled tasks (until end of day)"),
        ("schedule [days]", "Display multi-day schedule (default: 3 days)"),
        ("explain <task_id>", "Show details and scheduling info for a task"),
        ("config <cmd>", "Manage configuration (init, show, validate)"),
        ("clear / cls", "Clear the terminal screen"),
        ("help", "Show this help message"),
        ("exit / quit", "Exit the interactive shell"),
    ]
    
    for cmd, desc in commands_table:
        console.print(f"  [cyan]{cmd:20}[/cyan] [dim]{desc}[/dim]")
    
    console.print()
    console.print("[bold]Configuration:[/bold]")
    console.print(f"  [dim]Config file:[/dim] ~/.config/chronix/config.toml")
    console.print(f"  [dim]Run[/dim] [cyan]chronix config init[/cyan] [dim]to create a default configuration[/dim]")
    console.print()
    
    return 0


def _format_duration(duration: timedelta) -> str:
    """Format a timedelta as a human-readable string (legacy compatibility)."""
    return format_duration(duration)


def _display_continuous_timeline(day_schedule, work_start: datetime, work_end: datetime) -> None:
    """Display a continuous timeline including tasks, blocked time, and empty slots."""
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
    
    for i, segment in enumerate(timeline, 1):
        print_timeline_segment(
            index=i,
            start=segment['start'],
            end=segment['end'],
            segment_type=segment['type'],
            data=segment['data']
        )
