"""Visual formatting utilities for the Chronix CLI."""

from datetime import datetime, timedelta
from typing import Optional

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from chronix.core.models import Task, ScheduledTask, TimeBlock

# Global console instance
console = Console()

# Below this terminal width, a two-column primary/secondary split is too
# cramped to be readable (each column would be under ~45 columns, which
# wraps task titles and deadline lines badly), so callers fall back to a
# stacked, single-column rendering of each track in turn.
MIN_SPLIT_WIDTH = 100


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
    completed_tasks: int,
    sync_results: list = None
):
    """Print the sync summary with clean formatting, including per-document outcomes if provided."""
    console.print()
    
    if sync_results:
        success_count = sum(1 for r in sync_results if r.outcome.value == "success")
        not_found_count = sum(1 for r in sync_results if r.outcome.value == "not_found")
        failed_count = sum(1 for r in sync_results if r.outcome.value == "failed_after_retries")
        
        if failed_count > 0 or not_found_count > 0:
            console.print("⚠  [bold yellow]Sync completed with issues[/bold yellow]")
        else:
            console.print("✓ [bold green]Sync complete![/bold green]")
        console.print()
        
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="dim")
        table.add_column("Value", style="bold cyan")
        
        table.add_row("Projects", f"{success_count} synced")
        if not_found_count > 0:
            table.add_row("", f"{not_found_count} not found")
        if failed_count > 0:
            table.add_row("", f"{failed_count} failed after retries")
        
        if num_projects > 0:
            table.add_row("Total tasks", str(total_tasks))
            table.add_row("Incomplete", str(incomplete_tasks))
            table.add_row("Completed", str(completed_tasks))
        
        console.print(table)
        
        if failed_count > 0 or not_found_count > 0:
            console.print()
            console.print("[dim]Document outcomes:[/dim]")
            for result in sync_results:
                if result.outcome.value == "success":
                    status_icon = "[green]✓[/green]"
                elif result.outcome.value == "not_found":
                    status_icon = "[yellow]⊘[/yellow]"
                else:
                    status_icon = "[red]✗[/red]"
                
                outcome_text = result.outcome.value.replace("_", " ")
                console.print(f"  {status_icon} {result.document_label()}: {outcome_text}")
                if result.error and result.outcome.value != "not_found":
                    console.print(f"      [dim]{result.error}[/dim]")
    else:
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
    data: Optional[any] = None,
    display_tz=None,
    target_console: Optional[Console] = None,
):
    """Print a single timeline segment.

    `target_console` lets callers redirect output to an offscreen Console
    (e.g. when building the split primary/secondary view) instead of the
    shared module-level `console`. Defaults to `console` when not given.
    """
    out = target_console if target_console is not None else console
    # Convert to display timezone if provided
    if display_tz and start.tzinfo:
        start = start.astimezone(display_tz)
    if display_tz and end.tzinfo:
        end = end.astimezone(display_tz)
    
    time_range = f"{start.strftime('%H:%M')} – {end.strftime('%H:%M')}"
    
    if segment_type == 'task':
        _print_task_segment(index, time_range, data, display_tz=display_tz, target_console=out)
    elif segment_type == 'blocked':
        _print_blocked_segment(index, time_range, data, target_console=out)
    elif segment_type == 'paused':
        _print_paused_segment(index, time_range, data, target_console=out)
    elif segment_type == 'empty':
        _print_empty_segment(index, time_range, target_console=out)


def _print_task_segment(index: int, time_range: str, scheduled_task, display_tz=None, target_console: Optional[Console] = None):
    """Print a scheduled task segment."""
    out = target_console if target_console is not None else console
    task = scheduled_task.task
    
    # Build violation indicators
    violations = []
    if scheduled_task.violates_deadline_user:
        violations.append("⚠️")
    if scheduled_task.violates_deadline_external:
        violations.append("🔴")
    violation_str = " ".join(violations)
    
    # Main task line with segment indicator if applicable
    task_line = Text()
    task_line.append(f"{index:2}. ", style="dim")
    task_line.append(f"{time_range}  ", style="bold cyan")
    task_line.append("📋 ", style="")
    task_line.append(task.title, style="bold white")
    
    # Add segment indicator if task is split, and/or a partial-completion note
    if scheduled_task.is_segment:
        segment_label = f" (part {scheduled_task.segment_index}/{scheduled_task.total_segments})"
        task_line.append(segment_label, style="dim italic")
    if scheduled_task.is_partial:
        task_line.append(" (continues later)", style="dim italic")
    
    if violation_str:
        task_line.append(f" {violation_str}", style="")
    
    out.print(task_line)
    
    # Source line (document title + tab name)
    source_parts = []
    if task.document_title:
        source_parts.append(f"[{task.document_title}]")
    if task.section:
        source_parts.append(f"• {task.section}")
    
    if source_parts:
        source_text = " ".join(source_parts)
        # Use Text object to avoid Rich markup interpretation
        source_line = Text("    " + source_text, style="dim")
        out.print(source_line)
    elif task.project:
        # Fallback to project if no document/section info
        project_text = f"    [{task.project}]"
        project_line = Text(project_text, style="dim")
        out.print(project_line)
    
    # Duration and ID line
    duration_str = format_duration(task.estimated_duration)
    out.print(f"    [dim]Duration:[/dim] {duration_str} [dim]|[/dim] [dim]ID:[/dim] [yellow]{task.id}[/yellow]")
    
    # Deadline line
    deadline_to_show = None
    deadline_type = None
    
    if task.deadline_external:
        deadline_to_show = task.deadline_external
        deadline_type = "External"
    elif task.deadline_user:
        deadline_to_show = task.deadline_user
        deadline_type = "User"
    
    if deadline_to_show:
        if display_tz and deadline_to_show.tzinfo:
            deadline_to_show = deadline_to_show.astimezone(display_tz)
        deadline_str = deadline_to_show.strftime('%Y-%m-%d %H:%M')
        style = "red" if violations else ""
        if style:
            out.print(f"    [dim]{deadline_type} deadline:[/dim] [{style}]{deadline_str}[/{style}]")
        else:
            out.print(f"    [dim]{deadline_type} deadline:[/dim] {deadline_str}")
    
    out.print()


def _print_blocked_segment(index: int, time_range: str, block: TimeBlock, target_console: Optional[Console] = None):
    """Print a blocked time segment."""
    out = target_console if target_console is not None else console
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
    
    out.print(blocked_line)


def _print_paused_segment(index: int, time_range: str, block: TimeBlock, target_console: Optional[Console] = None):
    """Print a recurring block that's paused for this session.

    Greyed out and struck through: it no longer occupies any time (tasks may
    be scheduled straight through it), this is purely a visual reminder that
    it would normally have been here.
    """
    out = target_console if target_console is not None else console
    label = block.label or block.kind

    emoji = "🚫"
    if block.kind == "break":
        emoji = "☕"
    elif block.kind == "sleep":
        emoji = "😴"
    elif block.kind == "meeting":
        emoji = "📅"

    paused_line = Text()
    paused_line.append(f"{index:2}. ", style="dim")
    paused_line.append(f"{time_range}  ", style="dim strike")
    paused_line.append(f"{emoji} ", style="dim")
    paused_line.append(f"{label} (paused)", style="dim italic strike")

    out.print(paused_line)


def _print_empty_segment(index: int, time_range: str, target_console: Optional[Console] = None):
    """Print an empty time segment."""
    out = target_console if target_console is not None else console
    empty_line = Text()
    empty_line.append(f"{index:2}. ", style="dim")
    empty_line.append(f"{time_range}  ", style="dim")
    empty_line.append("(empty)", style="dim italic")
    
    out.print(empty_line)


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


def _build_timeline_renderable(
    day_schedule,
    work_start: datetime,
    work_end: datetime,
    start_index: int = 1,
    paused_blocks: Optional[list] = None,
) -> tuple[Group, int]:
    """Build the timeline as a Rich renderable (Group of Text objects) instead
    of printing it directly, so it can be embedded inside a Panel/column for
    the split primary/secondary view. Mirrors the segment-building logic in
    cli.commands._display_continuous_timeline exactly -- see that function's
    docstring for the segment/gap-filling/paused-block-layering behavior.

    Returns (renderable, next_index) so multi-day split callers can keep
    continuous numbering across days, same as the single-column path.
    """
    segments = []

    for scheduled_task in day_schedule.scheduled_tasks:
        segments.append({
            'start': scheduled_task.start,
            'end': scheduled_task.end,
            'type': 'task',
            'data': scheduled_task,
        })

    for block in day_schedule.blocked_time:
        segments.append({
            'start': block.start,
            'end': block.end,
            'type': 'blocked',
            'data': block,
        })

    segments.sort(key=lambda x: x['start'])

    timeline = []
    current_time = work_start

    for segment in segments:
        if current_time < segment['start']:
            timeline.append({
                'start': current_time,
                'end': segment['start'],
                'type': 'empty',
                'data': None,
            })
        timeline.append(segment)
        current_time = max(current_time, segment['end'])

    if current_time < work_end:
        timeline.append({
            'start': current_time,
            'end': work_end,
            'type': 'empty',
            'data': None,
        })

    if paused_blocks:
        for block in paused_blocks:
            timeline.append({
                'start': block.start,
                'end': block.end,
                'type': 'paused',
                'data': block,
            })
        timeline.sort(key=lambda seg: seg['start'])

    display_tz = work_start.tzinfo if work_start.tzinfo else None

    # force_terminal + explicit color_system are required here: without them
    # a non-tty Console (which this offscreen one is) silently strips all
    # ANSI styling, and Text.from_ansi below would then have nothing to
    # parse -- the captured text would render as unstyled plain text instead
    # of preserving the colors/bold/dim styling the segment printers apply.
    capture = Console(
        width=max(1, (console.width - 4) // 2),
        record=True,
        force_terminal=True,
        color_system="standard",
        file=_NullIO(),
    )
    current_index = start_index
    with capture.capture() as captured:
        for segment in timeline:
            print_timeline_segment(
                index=current_index,
                start=segment['start'],
                end=segment['end'],
                segment_type=segment['type'],
                data=segment['data'],
                display_tz=display_tz,
                target_console=capture,
            )
            current_index += 1

    return Text.from_ansi(captured.get()), current_index


class _NullIO:
    """Discard-everything file-like object for an offscreen Rich Console.

    Console(record=True) still writes to `file` as it goes; the actual
    captured output we care about comes from `capture.capture()` /
    `capture.export_text()`, not from this stream, so its contents are
    never read.
    """

    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass


def print_dual_timeline(
    primary_schedule,
    secondary_schedule,
    work_start: datetime,
    work_end: datetime,
    paused_blocks: Optional[list] = None,
) -> None:
    """Print the primary and secondary track timelines side by side.

    Falls back to a stacked rendering (primary timeline, then secondary
    timeline, each clearly headed) when the terminal is narrower than
    MIN_SPLIT_WIDTH, since a two-column layout below that width wraps task
    titles and deadline lines badly enough to hurt more than it helps.

    paused_blocks (recurring config blocks paused for this session -- see
    cli.commands._display_continuous_timeline) are shown in both columns at
    their original slot, since neither track's real scheduling is affected
    by them either way.
    """
    if console.width < MIN_SPLIT_WIDTH:
        console.print("[bold]⏰ Primary Timeline[/bold]")
        console.print()
        _print_single_timeline(primary_schedule, work_start, work_end, paused_blocks=paused_blocks)
        console.print()
        console.print("[bold]⏰ Secondary Timeline[/bold] [dim](parallel track)[/dim]")
        console.print()
        _print_single_timeline(secondary_schedule, work_start, work_end, paused_blocks=paused_blocks)
        return

    primary_renderable, _ = _build_timeline_renderable(
        primary_schedule, work_start, work_end, paused_blocks=paused_blocks
    )
    secondary_renderable, _ = _build_timeline_renderable(
        secondary_schedule, work_start, work_end, paused_blocks=paused_blocks
    )

    # Columns render pre-wrapped text (each built at half the console width
    # above), so overflow is set to "crop" rather than re-wrapped/folded --
    # re-wrapping already-wrapped lines here would double-wrap and misalign
    # the two columns.
    table = Table(box=None, show_header=True, padding=(0, 1, 0, 0), expand=True)
    table.add_column("⏰ Primary Timeline", ratio=1, overflow="crop", no_wrap=True)
    table.add_column("⏰ Secondary Timeline (parallel track)", ratio=1, overflow="crop", no_wrap=True)
    table.add_row(primary_renderable, secondary_renderable)

    console.print(table)


def _print_single_timeline(day_schedule, work_start: datetime, work_end: datetime, paused_blocks: Optional[list] = None) -> None:
    """Print one track's timeline directly to the shared console (stacked-view helper)."""
    renderable, _ = _build_timeline_renderable(day_schedule, work_start, work_end, paused_blocks=paused_blocks)
    console.print(renderable)


def print_conflicts(conflicts: list[str]):
    """Print deadline conflicts."""
    console.print()
    console.print("[bold yellow]⚠️  Deadline conflicts:[/bold yellow]")
    console.print()
    
    for conflict in conflicts:
        # Use Text object to avoid Rich markup interpretation of brackets
        # Apply yellow color to bullet, rest is normal style
        conflict_line = Text("   ")
        conflict_line.append("•", style="yellow")
        conflict_line.append(" " + conflict)
        console.print(conflict_line)
    
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

    if task.description:
        from rich.markup import escape
        console.print("[bold]📄 Description[/bold]")
        for line in task.description.split("\n"):
            console.print(f"   {escape(line)}")
        console.print()
    
    # Origin section
    console.print("[bold]📂 Origin[/bold]")
    console.print(f"   [dim]Project:[/dim] {project_context.project_name}")
    if task.section:
        console.print(f"   [dim]Section:[/dim] {task.section}")
    console.print(f"   [dim]Source:[/dim] {project_context.source}")
    if project_context and project_context.document_id:
        console.print(f"   [dim]Document:[/dim] [cyan]{project_context.document_label()}[/cyan]")
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
    
    if task.deadline_computed:
        deadline_str = task.deadline_computed.strftime('%Y-%m-%d %H:%M %Z')
        console.print(f"   [dim]Computed deadline:[/dim] {deadline_str}")
    
    if task.effective_deadline:
        deadline_str = task.effective_deadline.strftime('%Y-%m-%d %H:%M %Z')
        console.print(f"   [dim]Effective deadline:[/dim] [bold]{deadline_str}[/bold]")
    console.print()
    
    # Execution section
    console.print("[bold]⚙️  Execution[/bold]")
    console.print(f"   [dim]Mode:[/dim] {task.execution_mode}")
    if task.priority is not None:
        console.print(f"   [dim]Priority:[/dim] P{task.priority} [dim](from source document's config)[/dim]")
    else:
        console.print(f"   [dim]Priority:[/dim] [dim italic]Unranked[/dim italic]")
    console.print(f"   [dim]Ref:[/dim] {task.ref if task.ref else '[dim italic]Not set[/dim italic]'}")
    if task.depends_on:
        console.print(f"   [dim]Depends on:[/dim] {', '.join(task.depends_on)}")
    else:
        console.print(f"   [dim]Depends on:[/dim] [dim italic]None[/dim italic]")
    console.print()
    
    # Status section
    console.print("[bold]📊 Status[/bold]")
    status_str = "[green]✓ Yes[/green]" if task.completed else "[dim]No[/dim]"
    console.print(f"   [dim]Completed:[/dim] {status_str}")
    if task.is_active:
        console.print(f"   [dim]Work session:[/dim] [green]active[/green] [dim](since {task.active_since.strftime('%Y-%m-%d %H:%M %Z')})[/dim]")
    elif task.is_paused:
        console.print(f"   [dim]Work session:[/dim] [yellow]paused[/yellow]")
    else:
        console.print(f"   [dim]Work session:[/dim] [dim italic]None[/dim italic]")
    if task.sessions:
        actual = task.actual_duration if task.actual_duration is not None else task.compute_actual_duration()
        console.print(f"   [dim]Sessions logged:[/dim] {len(task.sessions)} [dim]|[/dim] [dim]Actual duration:[/dim] {format_duration(actual)}")
    console.print()


def print_document_overview(
    document_label: str,
    total_tasks: int,
    incomplete_count: int,
    completed_count: int
):
    """Print the header and task-count summary for a document's task listing."""
    console.print()
    console.print(f"[bold]📄 {document_label}[/bold]")
    console.print()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Label", style="dim")
    table.add_column("Value", style="bold cyan")
    table.add_row("Total tasks", str(total_tasks))
    table.add_row("Incomplete", str(incomplete_count))
    table.add_row("Completed", str(completed_count))
    console.print(table)
    console.print()


def print_task_table(tasks: list[Task]):
    """Print a compact table of tasks for a single page of a document's task listing."""
    table = Table(box=box.SIMPLE_HEAVY)
    table.add_column("ID", style="yellow")
    table.add_column("Title", style="white")
    table.add_column("Duration", style="cyan")
    table.add_column("Deadline", style="dim")
    table.add_column("Mode", style="dim")

    for task in tasks:
        deadline_str = (
            task.effective_deadline.strftime('%Y-%m-%d %H:%M')
            if task.effective_deadline else "-"
        )
        title = task.title if len(task.title) <= 60 else task.title[:57] + "..."
        table.add_row(
            task.id or "-",
            title,
            format_duration(task.estimated_duration),
            deadline_str,
            task.execution_mode,
        )

    console.print(table)


def print_page_footer(page: int, per_page: int, total: int, status_label: str):
    """Print the pagination summary line below a task table."""
    console.print()
    if total == 0:
        console.print(f"[dim]No {status_label} tasks.[/dim]")
        console.print()
        return

    start = (page - 1) * per_page + 1
    end = min(page * per_page, total)
    console.print(f"[dim]Showing {start}-{end} of {total} {status_label} tasks.[/dim]")
    if end < total:
        console.print(f"[dim]Use --page {page + 1} to see more.[/dim]")
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
