"""Visual formatting utilities for the Chronix CLI."""

from datetime import datetime, timedelta
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from chronix.core.models import Task, ScheduledTask, TimeBlock

# Global console instance
console = Console()


def format_duration(duration: timedelta) -> str:
    """Format a timedelta as a human-readable string."""
    total_seconds = int(duration.total_seconds())
    
    if total_seconds < 0:
        return "overdue"
    
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours == 0:
        parts.append(f"{minutes}m")
    
    return " ".join(parts)


def print_sync_summary(
    num_projects: int,
    total_tasks: int,
    incomplete_tasks: int,
    completed_tasks: int
):
    """Print the sync summary with clean formatting."""
    console.print()
    console.print("✓ [bold green]Sync complete![/bold green]")
    console.print()
    
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim")
    table.add_column("Value", style="bold cyan")
    
    table.add_row("Projects", str(num_projects))
    table.add_row("Total tasks", str(total_tasks))
    table.add_row("Incomplete", str(incomplete_tasks))
    table.add_row("Completed", str(completed_tasks))
    
    console.print(table)
    console.print()


def print_schedule_header(date, work_start: datetime, work_end: datetime, timezone_str: str):
    """Print the schedule header."""
    console.print()
    
    title = Text()
    title.append("📅 ", style="")
    title.append(f"Schedule for {date}", style="bold")
    
    console.print(title)
    
    time_range = f"{work_start.strftime('%H:%M')} – {work_end.strftime('%H:%M')}"
    console.print(f"   Work hours: [cyan]{time_range}[/cyan] [dim]({timezone_str})[/dim]")
    console.print()


def print_timeline_segment(
    index: int,
    start: datetime,
    end: datetime,
    segment_type: str,
    data: Optional[any] = None
):
    """Print a single timeline segment."""
    time_range = f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}"
    
    if segment_type == 'task':
        _print_task_segment(index, time_range, data)
    elif segment_type == 'blocked':
        _print_blocked_segment(index, time_range, data)
    elif segment_type == 'empty':
        _print_empty_segment(index, time_range)


def _print_task_segment(index: int, time_range: str, scheduled_task):
    """Print a scheduled task segment."""
    task = scheduled_task.task
    
    # Build violation indicators
    violations = []
    if scheduled_task.violates_deadline_user:
        violations.append("⚠️")
    if scheduled_task.violates_deadline_external:
        violations.append("🔴")
    violation_str = " ".join(violations)
    
    # Main task line
    task_line = Text()
    task_line.append(f"{index:2}. ", style="dim")
    task_line.append(f"{time_range}  ", style="bold cyan")
    task_line.append("📋 ", style="")
    task_line.append(task.title, style="bold white")
    if violation_str:
        task_line.append(f" {violation_str}", style="")
    
    console.print(task_line)
    
    # Origin line (project + section/tab) - escape square brackets for rich markup
    origin_parts = []
    if task.project:
        # Escape square brackets that would be interpreted as markup
        project_display = task.project.replace("[", r"\[").replace("]", r"\]")
        origin_parts.append(f"[{project_display}]")
    if task.section:
        origin_parts.append(f"• {task.section}")
    
    if origin_parts:
        origin_text = " ".join(origin_parts)
        console.print(f"    [dim]{origin_text}[/dim]")
    
    # Duration and ID line
    duration_str = format_duration(task.estimated_duration)
    console.print(f"    [dim]Duration:[/dim] {duration_str} [dim]|[/dim] [dim]ID:[/dim] [yellow]{task.id}[/yellow]")
    
    # Deadline line
    deadline_to_show = None
    deadline_type = None
    
    if task.deadline_user:
        deadline_to_show = task.deadline_user
        deadline_type = "User"
    elif task.deadline_external:
        deadline_to_show = task.deadline_external
        deadline_type = "External"
    
    if deadline_to_show:
        deadline_str = deadline_to_show.strftime('%Y-%m-%d %H:%M')
        style = "red" if violations else ""
        if style:
            console.print(f"    [dim]{deadline_type} deadline:[/dim] [{style}]{deadline_str}[/{style}]")
        else:
            console.print(f"    [dim]{deadline_type} deadline:[/dim] {deadline_str}")
    
    console.print()


def _print_blocked_segment(index: int, time_range: str, block: TimeBlock):
    """Print a blocked time segment."""
    label = block.label or block.kind
    
    # Choose emoji based on kind
    emoji = "🚫"
    style = "dim"
    
    if block.kind == "break":
        emoji = "☕"
        style = "yellow dim"
    elif block.kind == "sleep":
        emoji = "😴"
        style = "blue dim"
    elif block.kind == "meeting":
        emoji = "📅"
        style = "magenta dim"
    
    blocked_line = Text()
    blocked_line.append(f"{index:2}. ", style="dim")
    blocked_line.append(f"{time_range}  ", style="cyan dim")
    blocked_line.append(f"{emoji} ", style="")
    blocked_line.append(label, style=style)
    
    console.print(blocked_line)


def _print_empty_segment(index: int, time_range: str):
    """Print an empty time segment."""
    empty_line = Text()
    empty_line.append(f"{index:2}. ", style="dim")
    empty_line.append(f"{time_range}  ", style="dim")
    empty_line.append("(empty)", style="dim italic")
    
    console.print(empty_line)


def print_timeline_footer(
    total_duration: timedelta,
    num_scheduled: int,
    num_conflicts: int
):
    """Print the schedule summary footer."""
    console.print()
    
    summary = Text()
    summary.append("Total work time: ", style="dim")
    summary.append(format_duration(total_duration), style="bold")
    summary.append("  •  ", style="dim")
    summary.append("Tasks scheduled: ", style="dim")
    summary.append(str(num_scheduled), style="bold")
    
    if num_conflicts > 0:
        summary.append("  •  ", style="dim")
        summary.append("⚠️ Conflicts: ", style="yellow")
        summary.append(str(num_conflicts), style="bold yellow")
    
    console.print(summary)
    console.print()


def print_conflicts(conflicts: list[str]):
    """Print deadline conflicts."""
    console.print()
    console.print("[bold yellow]⚠️  Deadline conflicts:[/bold yellow]")
    console.print()
    
    for conflict in conflicts:
        console.print(f"   [yellow]•[/yellow] {conflict}")
    
    console.print()


def print_task_details(task: Task, project_context):
    """Print detailed task information."""
    console.print()
    
    # Task title
    title_text = Text()
    title_text.append("📝 ", style="")
    title_text.append(f"Task: ", style="dim")
    title_text.append(task.title, style="bold white")
    console.print(title_text)
    console.print(f"   [dim]ID:[/dim] [yellow]{task.id}[/yellow]")
    console.print()
    
    # Origin section
    console.print("[bold]📂 Origin[/bold]")
    console.print(f"   [dim]Project:[/dim] {project_context.project_name}")
    if task.section:
        console.print(f"   [dim]Section:[/dim] {task.section}")
    console.print(f"   [dim]Source:[/dim] {project_context.source}")
    if project_context and project_context.document_id:
        console.print(f"   [dim]Document ID:[/dim] [cyan]{project_context.document_id}[/cyan]")
    console.print()
    
    # Duration & Deadlines section
    console.print("[bold]⏱️  Duration & Deadlines[/bold]")
    console.print(f"   [dim]Estimated duration:[/dim] {format_duration(task.estimated_duration)}")
    
    if task.deadline_user:
        deadline_str = task.deadline_user.strftime('%Y-%m-%d %H:%M %Z')
        console.print(f"   [dim]User deadline:[/dim] {deadline_str}")
    else:
        console.print(f"   [dim]User deadline:[/dim] [dim italic]Not set[/dim italic]")
    
    if task.deadline_external:
        deadline_str = task.deadline_external.strftime('%Y-%m-%d %H:%M %Z')
        console.print(f"   [dim]External deadline:[/dim] {deadline_str}")
    else:
        console.print(f"   [dim]External deadline:[/dim] [dim italic]Not set[/dim italic]")
    
    if task.effective_deadline:
        deadline_str = task.effective_deadline.strftime('%Y-%m-%d %H:%M %Z')
        console.print(f"   [dim]Effective deadline:[/dim] [bold]{deadline_str}[/bold]")
    console.print()
    
    # Status section
    console.print("[bold]📊 Status[/bold]")
    status_str = "[green]✓ Yes[/green]" if task.completed else "[dim]No[/dim]"
    console.print(f"   [dim]Completed:[/dim] {status_str}")
    console.print()


def print_task_position(
    task: Task,
    position: int,
    total_tasks: int
):
    """Print task scheduling position and explanation."""
    console.print("[bold]📍 Scheduling Position[/bold]")
    console.print(f"   [dim]Position in queue:[/dim] [bold cyan]{position}[/bold cyan] [dim]of[/dim] {total_tasks}")
    console.print()
    console.print("   [dim]Explanation:[/dim]")
    console.print(f"   [dim]•[/dim] Tasks are ordered by duration (shortest first), then deadline")
    console.print(f"   [dim]•[/dim] This task has a [cyan]{format_duration(task.estimated_duration)}[/cyan] duration")
    
    if task.effective_deadline:
        from datetime import timezone
        time_until_deadline = task.effective_deadline - datetime.now(timezone.utc)
        console.print(f"   [dim]•[/dim] Time until deadline: [yellow]{format_duration(time_until_deadline)}[/yellow]")
    else:
        console.print(f"   [dim]•[/dim] No deadline set [dim](lower priority)[/dim]")
    
    console.print()


def print_error(message: str):
    """Print an error message."""
    from rich.markup import escape
    console.print(f"[bold red]Error:[/bold red] {escape(message)}")


def print_warning(message: str):
    """Print a warning message."""
    from rich.markup import escape
    console.print(f"[yellow]⚠️[/yellow]  {escape(message)}")


def print_success(message: str):
    """Print a success message."""
    from rich.markup import escape
    console.print(f"[green]✓[/green] {escape(message)}")


def print_info(message: str):
    """Print an informational message."""
    from rich.markup import escape
    console.print(f"[cyan]ℹ[/cyan]  {escape(message)}")


def print_section_header(text: str):
    """Print a section header."""
    console.print()
    console.print(f"[bold]{text}[/bold]")
    console.print()
