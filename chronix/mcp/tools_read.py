"""Read-only MCP tools: context queries and schedule/project views.

None of these mutate chronix's persisted state. They read from the shared
`chronix.cli.commands._context` singleton, which this MCP server keeps warm
for the lifetime of the process -- see chronix.mcp.server for the sync
model these tools are built around.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from chronix.cli import commands as cli_commands
from chronix.cli.commands import _context, _configured_tz
from chronix.cli.sync_helpers import _sync_single_source_with_retries
from chronix.core.aggregation import TaskAggregator
from chronix.core.deadline_backfill import compute_backlog_deadlines
from chronix.mcp.errors import command_error, not_found_error, validation_error
from chronix.mcp.serializers import (
    serialize_aggregated_task,
    serialize_day_schedule,
    serialize_project_context,
    serialize_task,
)


def sync(project_tokens: Optional[list[str]] = None) -> dict[str, Any]:
    """Fetch and parse configured projects, refreshing the server's in-memory task state.

    With no arguments, syncs every project configured in config.toml and
    replaces the current context entirely. With `project_tokens` (project
    names), syncs only those projects and merges the result into the
    existing context, leaving other already-synced projects untouched. A
    project with two sources (Google Docs and a local file) has both fetched
    together, so aggregation can merge/dedupe them correctly.

    Call this before `today`, `schedule`, `explain`, or `deadlines` if the
    context has never been synced this session, or if changes may have been
    made in a source directly since the last sync. Write tools (add,
    update, done, etc.) refresh their own project automatically and do not
    require a sync first or after.
    """
    from chronix.config import ChronixConfig
    from chronix.integrations.factory import get_client

    try:
        config = ChronixConfig.load_or_default()
    except Exception as e:
        return command_error(f"Failed to load configuration: {e}")

    if not config.projects:
        return validation_error("No projects configured in config.toml.")

    if project_tokens:
        unknown = [t for t in project_tokens if config.find_project(t) is None]
        if unknown:
            return validation_error(f"Unknown project token(s): {', '.join(unknown)}")
        seen: set[str] = set()
        project_names: list[str] = []
        for token in project_tokens:
            resolved = config.find_project(token).name
            if resolved not in seen:
                seen.add(resolved)
                project_names.append(resolved)
    else:
        project_names = [p.name for p in config.projects]

    sources_to_sync = [s for s in config.all_sources() if s.project_name in project_names]

    needs_google_auth = any(s.type == "google_docs" for s in sources_to_sync)
    if needs_google_auth:
        client = _context._ensure_google_client()
        try:
            if not client.authenticate():
                return command_error("Google authentication failed. Check configured credentials.")
        except Exception as e:
            return command_error(f"Authentication error: {e}")

    tz = _configured_tz(config)
    synced_projects = []
    all_meetings = []
    failures = []

    for source in sources_to_sync:
        source_client = _context._ensure_google_client() if source.type == "google_docs" else get_client(source)
        result, project, meetings = _sync_single_source_with_retries(source, source_client, tz=tz)
        if result.outcome.value == "success":
            synced_projects.append(project)
            all_meetings.extend(meetings)
        else:
            failures.append({"source_id": source.source_id, "project_name": source.project_name, "reason": str(result)})

    if project_tokens and _context.projects:
        synced_ids = {p.project_context.project_id for p in synced_projects}
        _context.projects = [
            p for p in _context.projects if p.project_context.project_id not in synced_ids
        ] + synced_projects
        _context.ad_hoc_meetings.extend(all_meetings)
    else:
        _context.projects = synced_projects
        _context.ad_hoc_meetings = all_meetings

    _context.last_sync = datetime.now(timezone.utc)
    _context.config = config

    total_tasks = sum(len(p.tasks) for p in synced_projects)
    incomplete_tasks = sum(len([t for t in p.tasks if not t.completed]) for p in synced_projects)

    return {
        "ok": len(failures) == 0,
        "synced_projects": len(project_names),
        "total_tasks": total_tasks,
        "incomplete_tasks": incomplete_tasks,
        "completed_tasks": total_tasks - incomplete_tasks,
        "failures": failures,
        "last_sync": _context.last_sync.isoformat(),
    }


def today(time_override: Optional[str] = None, split: bool = False) -> dict[str, Any]:
    """Return today's scheduled timeline, built from the currently synced context.

    Requires a prior `sync`. `time_override` (HH:MM, 24-hour) sets the start
    of the scheduling window instead of the current time. `split` also
    schedules and returns the secondary track (tasks that can run alongside
    the primary track) as `secondary_schedule`.
    """
    if not _context.projects:
        return validation_error("No projects loaded. Call sync first.")

    try:
        primary, secondary, work_start, work_end, paused_blocks = cli_commands._generate_today_schedules(
            time_override
        )
    except ValueError as e:
        return validation_error(str(e))
    except RuntimeError as e:
        return command_error(str(e))

    result = {
        "ok": True,
        "work_start": work_start.isoformat(),
        "work_end": work_end.isoformat(),
        "primary_schedule": serialize_day_schedule(primary),
        "paused_blocks": [b.label or b.kind for b in paused_blocks],
    }
    if split:
        result["secondary_schedule"] = serialize_day_schedule(secondary)
    return result


def schedule(
    days: Optional[int] = None,
    day_range_start: Optional[int] = None,
    day_range_end: Optional[int] = None,
    forecast_from_day: Optional[int] = None,
    forecast_count: Optional[int] = None,
) -> dict[str, Any]:
    """Return a multi-day schedule projection from the currently synced context.

    Requires a prior `sync`. With no arguments, schedules the full backlog
    starting today with no day limit. `days` caps how many days are
    returned. `day_range_start`/`day_range_end` run the same continuous
    simulation but only return that 1-indexed day range (today is day 1).
    `forecast_from_day` (optionally with `forecast_count`) instead forecasts
    what the schedule would look like starting at that future day, using the
    full current backlog as-is rather than simulating depletion of the days
    before it.
    """
    if not _context.projects:
        return validation_error("No projects loaded. Call sync first.")

    from zoneinfo import ZoneInfo
    from datetime import date, timedelta as td

    from chronix.config import ChronixConfig, config_to_time_blocks, get_work_window, get_work_windows
    from chronix.core.models import TimeBlock
    from chronix.core.scheduler import SchedulingEngine

    config = _context.config or ChronixConfig.load_or_default()
    tz = ZoneInfo(config.scheduling.timezone)

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)
    task_pool = aggregator.get_task_pool(aggregated_tasks)
    incomplete_tasks = [t for t in task_pool if not t.completed]

    now = datetime.now(tz)

    if forecast_from_day is not None:
        forecast_date = now.date() + td(days=forecast_from_day - 1)
        forecast_work_start, _ = get_work_window(config, forecast_date)
        start_time = now if (forecast_from_day == 1 and now > forecast_work_start) else forecast_work_start
        num_days = forecast_count
    else:
        first_day_start, _ = get_work_window(config, now.date())
        start_time = max(now, first_day_start)
        num_days = day_range_end if day_range_end is not None else days

    def daily_blocked_time(day_date: date) -> list:
        blocked = [b for b in config_to_time_blocks(config, day_date) if cli_commands._is_block_active(b)]
        day_windows = get_work_windows(config, day_date)
        for i in range(len(day_windows) - 1):
            gap_start, gap_end = day_windows[i][1], day_windows[i + 1][0]
            if gap_start < gap_end:
                blocked.append(TimeBlock(start=gap_start, end=gap_end, kind="blocked", label="off hours"))
        for meeting in _context.ad_hoc_meetings:
            if meeting.start.date() == day_date:
                blocked.append(meeting.to_time_block())
        return blocked

    scheduler = SchedulingEngine()
    schedules_by_day = scheduler.schedule_continuous(
        tasks=incomplete_tasks,
        start_time=start_time,
        num_days=num_days,
        daily_blocked_time_fn=daily_blocked_time,
    )

    days_out = []
    for day_offset, day_date in enumerate(sorted(schedules_by_day.keys())):
        day_number = day_offset + 1
        if forecast_from_day is not None:
            if forecast_count is not None and day_offset >= forecast_count:
                break
        else:
            if days is not None and day_offset >= days:
                break
            if day_range_start is not None and day_number < day_range_start:
                continue
        days_out.append({
            "day_number": day_number,
            **serialize_day_schedule(schedules_by_day[day_date]),
        })

    return {"ok": True, "days": days_out}


def explain(task_id: str) -> dict[str, Any]:
    """Return details and scheduling position for a task, from the currently synced context.

    Requires a prior `sync`. `position` (1-indexed) reflects where the task
    sits in the global incomplete-task queue chronix would schedule from;
    it is omitted for completed tasks.
    """
    if not _context.projects:
        return validation_error("No projects loaded. Call sync first.")

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)

    agg_task = next((a for a in aggregated_tasks if a.task.id == task_id), None)
    if agg_task is None:
        return not_found_error("task", task_id)

    task_pool = aggregator.get_task_pool(aggregated_tasks)
    incomplete_tasks = [t for t in task_pool if not t.completed]

    result = serialize_aggregated_task(agg_task)
    if agg_task.task in incomplete_tasks:
        result["position"] = incomplete_tasks.index(agg_task.task) + 1
        result["queue_length"] = len(incomplete_tasks)

    return {"ok": True, "task": result}


def projects() -> dict[str, Any]:
    """List every configured project and its source(s), with sync status if known."""
    from chronix.config import ChronixConfig

    try:
        config = ChronixConfig.load_or_default()
    except Exception as e:
        return command_error(f"Failed to load configuration: {e}")

    synced_project_ids = {p.project_context.project_id for p in _context.projects}

    ranked = sorted(
        config.projects,
        key=lambda p: p.priority if p.priority is not None else float("inf"),
    )

    return {
        "ok": True,
        "projects": [
            {
                "name": project.name,
                "priority": project.priority,
                "synced": project.name in synced_project_ids,
                "sources": [
                    {
                        "type": source.type,
                        "source_id": source.document_id if source.type == "google_docs" else source.file_path,
                    }
                    for source in project.sources
                ],
            }
            for project in ranked
        ],
    }


def project(
    project_token: str,
    page: int = 1,
    per_page: int = 20,
    status: str = "incomplete",
) -> dict[str, Any]:
    """Return a paginated task list for a single synced project.

    `project_token` is the project's configured name. `status` is
    one of "incomplete" (default), "complete", or "all". Tasks are
    aggregated across all of the project's sources (see
    chronix.core.aggregation.TaskAggregator), so a task appearing in only
    one of two configured sources still shows up here. The project must
    already be synced -- call `sync` with this project's token first if it
    hasn't been synced yet this session.
    """
    if status not in ("incomplete", "complete", "all"):
        return validation_error(f"Invalid status '{status}'. Must be incomplete, complete, or all.", field="status")
    if page < 1:
        return validation_error("page must be positive.", field="page")
    if per_page < 1:
        return validation_error("per_page must be positive.", field="per_page")

    from chronix.config import ChronixConfig

    config = _context.config or ChronixConfig.load_or_default()
    project_config = config.find_project(project_token)
    if project_config is None:
        return not_found_error("project", project_token)

    project_todos = [
        p for p in _context.projects if p.project_context.project_id == project_config.name
    ]
    if not project_todos:
        return validation_error(f"Project '{project_token}' not synced yet. Call sync with this project first.")

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(project_todos)
    all_tasks = [agg.task for agg in aggregated_tasks]

    if status == "incomplete":
        filtered = [t for t in all_tasks if not t.completed]
    elif status == "complete":
        filtered = [t for t in all_tasks if t.completed]
    else:
        filtered = all_tasks

    start = (page - 1) * per_page
    page_tasks = filtered[start:start + per_page]

    if not page_tasks and filtered:
        return validation_error(f"Page {page} is out of range ({len(filtered)} {status} task(s) total).")

    incomplete_count = sum(1 for t in all_tasks if not t.completed)
    return {
        "ok": True,
        "project": serialize_project_context(project_todos[0].project_context),
        "total_tasks": len(all_tasks),
        "incomplete_count": incomplete_count,
        "completed_count": len(all_tasks) - incomplete_count,
        "page": page,
        "per_page": per_page,
        "total_matching": len(filtered),
        "tasks": [serialize_task(t) for t in page_tasks],
    }


def tabs(source_token: str) -> dict[str, Any]:
    """List the tabs in a Google Docs source, for use with `add`'s tab argument.

    `source_token` is the project's configured name, resolving to its
    google_docs source. Local file sources have no tab concept. This fetches
    the document directly rather than reading from the synced context, so it
    works even before the project has been synced.
    """
    from chronix.config import ChronixConfig
    from chronix.core.todo import EXCLUDED_TAB_TITLES
    from chronix.integrations.google_docs.parser import GoogleDocsParser

    config = _context.config or ChronixConfig.load_or_default()
    source = config.resolve_source(source_token)
    if source is None:
        return not_found_error("source", source_token)
    if source.type != "google_docs":
        return validation_error(f"'{source_token}' is a local file, which has no tabs. Tabs only apply to Google Docs sources.")
    doc_id = source.source_id

    client = _context._ensure_google_client()
    try:
        if not client.authenticate():
            return command_error("Google authentication failed. Check configured credentials.")
        doc = client.fetch_document(doc_id)
    except Exception as e:
        return command_error(f"Failed to fetch document: {e}")

    structure = GoogleDocsParser().parse_document(doc)

    return {
        "ok": True,
        "tabs": [
            {
                "title": tab.title or None,
                "tab_id": tab.tab_id,
                "has_task_section": tab.checkbox_list_id is not None,
                "excluded_from_auto_select": (tab.title or "").strip().lower() in EXCLUDED_TAB_TITLES,
            }
            for tab in structure.tabs
        ],
    }


def blocks() -> dict[str, Any]:
    """List recurring config time blocks (sleep, breaks, meetings) and this session's pause state.

    Pausing/resuming a block is done via `blocks_pause`/`blocks_resume`, not
    this tool. Pauses only affect this server process's session; config.toml
    is never modified.
    """
    from chronix.config import ChronixConfig

    config = _context.config or ChronixConfig.load_or_default()
    all_blocks = config.scheduling.sleep_windows + config.scheduling.breaks + config.scheduling.meetings

    return {
        "ok": True,
        "blocks": [
            {
                "name": block.label or block.kind.capitalize(),
                "kind": block.kind,
                "start_time": block.start_time.strftime("%H:%M"),
                "end_time": block.end_time.strftime("%H:%M"),
                "paused": cli_commands._block_config_key(block) in _context.disabled_blocks,
            }
            for block in all_blocks
        ],
    }


def deadlines_preview(
    task_id: Optional[str] = None,
    project_token: Optional[str] = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Preview backfilled `deadline_computed` values without writing them.

    Exactly one scope must be given: `task_id` for a single task,
    `project_token` for every eligible task in that project, or
    `all_projects=True` for the whole synced backlog. Eligible tasks are
    incomplete tasks with neither an external nor a user deadline. To
    actually write the previewed values, use `deadlines_apply` with the same
    scope.
    """
    scopes_given = sum([task_id is not None, project_token is not None, all_projects])
    if scopes_given == 0:
        return validation_error("Specify exactly one of task_id, project_token, or all_projects=True.")
    if scopes_given > 1:
        return validation_error("Specify only one of task_id, project_token, or all_projects.")

    if not _context.projects:
        return validation_error("No projects loaded. Call sync first.")

    from chronix.config import ChronixConfig

    config = _context.config or ChronixConfig.load_or_default()

    project_name = None
    if project_token is not None:
        project_config = config.find_project(project_token)
        if project_config is None:
            return not_found_error("project", project_token)
        project_name = project_config.name

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)
    all_tasks = [agg.task for agg in aggregated_tasks]

    if task_id is not None and not any(t.id == task_id for t in all_tasks):
        return not_found_error("task", task_id)

    now = datetime.now(timezone.utc)
    results = compute_backlog_deadlines(all_tasks, now)

    if task_id is not None:
        results = [r for r in results if r.task.id == task_id]
    elif project_name is not None:
        project_task_ids = {
            t.id for p in _context.projects
            if p.project_context.project_id == project_name
            for t in p.tasks
        }
        results = [r for r in results if r.task.id in project_task_ids]

    return {
        "ok": True,
        "preview": [
            {
                "task_id": r.task.id,
                "title": r.task.title,
                "document_title": r.task.document_title,
                "computed_deadline": r.deadline.isoformat(),
            }
            for r in results
        ],
    }
