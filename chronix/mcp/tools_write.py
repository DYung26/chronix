"""Write MCP tools: task creation, edits, and lifecycle transitions.

These correspond to chronix's "flagged" CLI paths, never the interactive
prompt/form paths -- there's no TTY on the other end of an MCP tool call. A
missing required field is returned as a structured `missing_fields_error`
(see chronix.mcp.errors) rather than prompted for, so the calling model can
ask the user and retry the call with the field filled in.

Each write here refreshes the affected document in `_context` immediately
after writing (mirroring `chronix.cli.commands._resync_document`), so a
`document`/`explain`/`today` call right after reflects the change without a
separate `sync`.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from chronix.cli.commands import (
    _configured_tz,
    _context,
    _find_duplicate_ref_task,
    _find_task_in_context,
    _get_calendar_task_start,
    _get_task_writer,
    _resolve_document_token,
    _resolve_edit_doc,
    _resync_document,
)
from chronix.core.metadata import parse_deadline, parse_duration
from chronix.core.models import WorkSession, generate_task_id
from chronix.core.metadata import (
    KEY_ACTIVE_SINCE,
    KEY_ACTUAL_DURATION,
    KEY_DEADLINE_COMPUTED,
    KEY_DEPENDS,
    KEY_REF,
    KEY_SESSIONS,
    serialize_active_since,
    serialize_deadline,
    serialize_duration,
    serialize_sessions,
)
from chronix.core.todo import TaskParser
from chronix.core.writer import NewTask, TaskNotFoundError, TaskUpdate
from chronix.mcp.errors import command_error, missing_fields_error, not_found_error, validation_error
from chronix.mcp.serializers import serialize_task

_VALID_MODES = TaskParser.VALID_MODES
_VALID_TRACKS = TaskParser.VALID_TRACKS


def _load_config():
    from chronix.config import ChronixConfig
    return _context.config or ChronixConfig.load_or_default()


def _resolve_write_document(document_token: Optional[str], config) -> Optional[str]:
    """Resolve the target document for `add`, honoring a single configured document as default."""
    if document_token is not None:
        return _resolve_document_token(document_token, config)
    all_doc_ids = config.google_docs.document_ids
    return all_doc_ids[0] if len(all_doc_ids) == 1 else None


def add_task(
    duration: str,
    title: str,
    document_token: Optional[str] = None,
    tab: Optional[str] = None,
    description: Optional[str] = None,
    external_deadline: Optional[str] = None,
    user_deadline: Optional[str] = None,
    mode: Optional[str] = None,
    track: Optional[str] = None,
    ref: Optional[str] = None,
    depends: Optional[str] = None,
) -> dict[str, Any]:
    """Create a new task in a Google Docs document.

    `duration` accepts forms like "2h", "30m", "2hours", "30minutes".
    `document_token` (id or alias) is required unless exactly one document is
    configured. `tab` selects the tab by title or ID; without it, the task
    goes into the first tab with a TASKS section. `external_deadline` and
    `user_deadline` are ISO-8601. `mode` is one of atomic/flex/
    contiguous_preferred; `track` is one of auto/primary/secondary.
    """
    try:
        parsed_duration = parse_duration(duration)
    except Exception:
        parsed_duration = None
    if parsed_duration is None:
        return validation_error(f"Invalid duration '{duration}'. Use forms like 2h, 30m, 2hours, 30minutes.", field="duration")

    if mode is not None and mode not in _VALID_MODES:
        return validation_error(f"Invalid mode '{mode}'. Valid: {', '.join(_VALID_MODES)}.", field="mode")
    if track is not None and track not in _VALID_TRACKS:
        return validation_error(f"Invalid track '{track}'. Valid: {', '.join(_VALID_TRACKS)}.", field="track")

    try:
        config = _load_config()
    except Exception as e:
        return command_error(f"Failed to load configuration: {e}")

    tz = _configured_tz(config)
    try:
        parsed_external_deadline = parse_deadline(external_deadline, tz) if external_deadline else None
        parsed_user_deadline = parse_deadline(user_deadline, tz) if user_deadline else None
    except ValueError as e:
        return validation_error(str(e))

    if not config.google_docs.document_ids:
        return validation_error("No documents configured. Run 'chronix config init' to set up.")

    doc_id = _resolve_write_document(document_token, config)
    if doc_id is None:
        if document_token is not None:
            return not_found_error("document", document_token)
        return missing_fields_error(
            "add_task",
            [{
                "field": "document_token",
                "description": "Multiple documents are configured; specify which one by id or alias.",
            }],
        )

    try:
        writer = _get_task_writer(tz)
        task_id = generate_task_id()
        writer.create_task(
            doc_id,
            NewTask(
                title=title,
                duration=parsed_duration,
                id=task_id,
                tab=tab,
                description=description,
                external_deadline=parsed_external_deadline,
                user_deadline=parsed_user_deadline,
                mode=mode,
                track=track,
                ref=ref,
                depends=depends,
            ),
        )
        _resync_document(doc_id, config)
    except Exception as e:
        return command_error(f"Failed to add task: {e}")

    return {"ok": True, "task_id": task_id, "title": title, "document_id": doc_id}


def _resolve_document_for_edit(task_id: str, document_token: Optional[str], config) -> Optional[str]:
    return _resolve_edit_doc(task_id, document_token, config)


def _apply_update(command: str, task_id: str, update: TaskUpdate, document_token: Optional[str], config) -> dict[str, Any]:
    doc_id = _resolve_document_for_edit(task_id, document_token, config)
    if doc_id is None:
        if not config.google_docs.document_ids:
            return validation_error("No documents configured.")
        return missing_fields_error(
            command,
            [{
                "field": "document_token",
                "description": "Multiple documents are configured and the task's document could not be inferred; specify one by id or alias.",
            }],
        )

    try:
        writer = _get_task_writer(_configured_tz(config))
        writer.update_task(doc_id, task_id, update)
        _resync_document(doc_id, config)
    except TaskNotFoundError:
        return not_found_error("task", task_id)
    except Exception as e:
        return command_error(f"Update failed: {e}")

    return {"ok": True, "task_id": task_id, "document_id": doc_id}


def update_task(
    task_id: str,
    document_token: Optional[str] = None,
    title: Optional[str] = None,
    duration: Optional[str] = None,
    description: Optional[str] = None,
    external_deadline: Optional[str] = None,
    user_deadline: Optional[str] = None,
    mode: Optional[str] = None,
    track: Optional[str] = None,
    ref: Optional[str] = None,
    depends: Optional[str] = None,
    metadata: Optional[dict[str, str]] = None,
    metadata_remove: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Update one or more fields of an existing task.

    Only fields explicitly passed are changed. For `description`,
    `external_deadline`, `user_deadline`, `ref`, and `depends`, pass "-" to
    clear that field rather than leave it unchanged. At least one field must
    be provided beyond `task_id`/`document_token`.
    """
    try:
        config = _load_config()
    except Exception as e:
        return command_error(f"Failed to load configuration: {e}")
    tz = _configured_tz(config)

    update = TaskUpdate()
    if title is not None:
        update.title = title
    if duration is not None:
        parsed = parse_duration(duration)
        if parsed is None:
            return validation_error(f"Invalid duration '{duration}'. Use forms like 2h, 30m.", field="duration")
        update.duration = parsed
    if description is not None:
        update.description = None if description == "-" else description
    if external_deadline is not None:
        try:
            update.external_deadline = None if external_deadline == "-" else parse_deadline(external_deadline, tz)
        except ValueError as e:
            return validation_error(str(e), field="external_deadline")
    if user_deadline is not None:
        try:
            update.user_deadline = None if user_deadline == "-" else parse_deadline(user_deadline, tz)
        except ValueError as e:
            return validation_error(str(e), field="user_deadline")
    if mode is not None:
        if mode not in _VALID_MODES:
            return validation_error(f"Invalid mode '{mode}'. Valid: {', '.join(_VALID_MODES)}.", field="mode")
        update.mode = mode
    if track is not None:
        if track not in _VALID_TRACKS:
            return validation_error(f"Invalid track '{track}'. Valid: {', '.join(_VALID_TRACKS)}.", field="track")
        update.track = track
    if ref is not None:
        new_ref = "" if ref == "-" else ref
        if new_ref:
            conflict = _find_duplicate_ref_task(new_ref, task_id)
            if conflict is not None:
                return validation_error(
                    f"Duplicate ref '{new_ref}': already used by task '{conflict.title}' (id={conflict.id}).",
                    field="ref",
                )
        update.metadata[KEY_REF] = new_ref
    if depends is not None:
        update.metadata[KEY_DEPENDS] = "" if depends == "-" else depends
    if metadata:
        update.metadata.update(metadata)
    if metadata_remove:
        update.metadata_remove.extend(metadata_remove)

    if not any([
        update.title, update.duration, update.has_description_change(),
        update.has_external_deadline_change(), update.has_user_deadline_change(),
        update.mode, update.track, update.metadata, update.metadata_remove,
    ]):
        return missing_fields_error(
            "update_task",
            [{
                "field": "any field",
                "description": "At least one of title/duration/description/external_deadline/user_deadline/mode/track/ref/depends/metadata must be provided.",
            }],
        )

    return _apply_update("update_task", task_id, update, document_token, config)


def rename_task(task_id: str, title: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Change a task's title."""
    config = _load_config()
    return _apply_update("rename_task", task_id, TaskUpdate(title=title), document_token, config)


def set_duration(task_id: str, duration: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Change a task's estimated duration (e.g. "2h", "30m"). Must exceed any already-logged work time."""
    parsed = parse_duration(duration)
    if parsed is None:
        return validation_error(f"Invalid duration '{duration}'. Use forms like 2h, 30m.", field="duration")

    task = _find_task_in_context(task_id)
    if task is not None and not task.completed:
        actual = task.compute_actual_duration()
        if parsed <= actual:
            return validation_error(
                f"New duration ({duration}) must exceed already-logged time ({serialize_duration(actual)}).",
                field="duration",
            )

    config = _load_config()
    return _apply_update("set_duration", task_id, TaskUpdate(duration=parsed), document_token, config)


def set_deadline(
    task_id: str,
    deadline: str,
    use_user_deadline: bool = False,
    document_token: Optional[str] = None,
) -> dict[str, Any]:
    """Set a task's external (default) or user deadline. Pass "-" to clear it."""
    config = _load_config()
    tz = _configured_tz(config)
    try:
        parsed = None if deadline == "-" else parse_deadline(deadline, tz)
    except ValueError as e:
        return validation_error(str(e), field="deadline")

    update = TaskUpdate()
    if use_user_deadline:
        update.user_deadline = parsed
    else:
        update.external_deadline = parsed

    return _apply_update("set_deadline", task_id, update, document_token, config)


def set_mode(task_id: str, mode: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Set a task's execution mode: atomic, flex, or contiguous_preferred."""
    if mode not in _VALID_MODES:
        return validation_error(f"Invalid mode '{mode}'. Valid: {', '.join(_VALID_MODES)}.", field="mode")
    config = _load_config()
    return _apply_update("set_mode", task_id, TaskUpdate(mode=mode), document_token, config)


def set_track(task_id: str, track: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Set which independently-scheduled timeline a task belongs to: auto, primary, or secondary."""
    if track not in _VALID_TRACKS:
        return validation_error(f"Invalid track '{track}'. Valid: {', '.join(_VALID_TRACKS)}.", field="track")
    config = _load_config()
    return _apply_update("set_track", task_id, TaskUpdate(track=track), document_token, config)


def set_metadata(
    task_id: str,
    metadata: Optional[dict[str, str]] = None,
    remove_keys: Optional[list[str]] = None,
    document_token: Optional[str] = None,
) -> dict[str, Any]:
    """Set or remove arbitrary key=value metadata fields on a task."""
    if not metadata and not remove_keys:
        return missing_fields_error(
            "set_metadata",
            [{"field": "metadata or remove_keys", "description": "Provide at least one metadata key=value pair to set, or a key to remove."}],
        )
    config = _load_config()
    update = TaskUpdate(metadata=metadata or {}, metadata_remove=remove_keys or [])
    return _apply_update("set_metadata", task_id, update, document_token, config)


def mark_done(task_id: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Mark a task complete, closing any active session and recording actual work duration.

    If no work sessions exist and a scheduled calendar event is found, a
    session from the calendar start to now is recorded automatically.
    """
    config = _load_config()
    tz = _configured_tz(config)
    now = datetime.now(timezone.utc)

    update = TaskUpdate(completed=True)
    task = _find_task_in_context(task_id)
    warning = None

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
                warning = (
                    "No work sessions recorded and no scheduled calendar event found. "
                    "actual_duration was not set."
                )

        if sessions:
            actual = sum((s.duration for s in sessions), timedelta())
            update.metadata[KEY_SESSIONS] = serialize_sessions(sessions, tz)
            update.metadata[KEY_ACTUAL_DURATION] = serialize_duration(actual)

    result = _apply_update("mark_done", task_id, update, document_token, config)
    if result.get("ok") and warning:
        result["warning"] = warning
    return result


def pause_task(task_id: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Close the currently active work session on a task.

    The first pause uses the task's scheduled calendar start as the session
    start time; subsequent pauses use the `resume` timestamp. If this would
    bring logged time to or past the task's estimated duration, this returns
    a structured warning instead of pausing -- call `set_duration` to extend
    first, or `mark_done` to complete it instead.
    """
    task = _find_task_in_context(task_id)
    if task is None:
        return not_found_error("task", task_id)
    if task.is_paused:
        return validation_error(f"Task '{task_id}' is already paused.")

    config = _load_config()
    tz = _configured_tz(config)
    now = datetime.now(timezone.utc)

    if task.active_since is not None:
        session_start = task.active_since
    else:
        session_start = _get_calendar_task_start(task_id, created=task.created)
        if session_start is None:
            return validation_error(
                f"Task '{task_id}' has no scheduled calendar event. Sync a calendar schedule for it first."
            )

    new_session = WorkSession(start=session_start, end=now)
    sessions = task.sessions + [new_session]
    prospective_actual = sum((s.duration for s in sessions), timedelta())

    if prospective_actual >= task.estimated_duration:
        return {
            "ok": False,
            "error": "would_exceed_estimate",
            "message": (
                f"Pausing now would bring logged time to {serialize_duration(prospective_actual)}, "
                f"at or past the {serialize_duration(task.estimated_duration)} estimate."
            ),
            "prospective_logged_duration": serialize_duration(prospective_actual),
            "estimated_duration": serialize_duration(task.estimated_duration),
            "resolution_options": [
                "Call set_duration with a longer duration, then retry pause_task.",
                "Call mark_done instead to complete the task now.",
            ],
        }

    update = TaskUpdate(
        metadata={KEY_SESSIONS: serialize_sessions(sessions, tz)},
        metadata_remove=[KEY_ACTIVE_SINCE],
    )
    result = _apply_update("pause_task", task_id, update, document_token, config)
    if result.get("ok"):
        result["session_duration"] = serialize_duration(new_session.duration)
    return result


def resume_task(task_id: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Begin a new work session on a task, starting now."""
    task = _find_task_in_context(task_id)
    if task is None:
        return not_found_error("task", task_id)
    if task.active_since is not None:
        return validation_error(f"Task '{task_id}' is already active.")

    config = _load_config()
    tz = _configured_tz(config)
    now = datetime.now(timezone.utc)

    update = TaskUpdate(metadata={KEY_ACTIVE_SINCE: serialize_active_since(now, tz)})
    return _apply_update("resume_task", task_id, update, document_token, config)


def mark_undone(task_id: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Mark a completed task as incomplete again."""
    config = _load_config()
    return _apply_update("mark_undone", task_id, TaskUpdate(completed=False), document_token, config)


def delete_task(task_id: str, document_token: Optional[str] = None) -> dict[str, Any]:
    """Permanently remove a task from its document. This cannot be undone."""
    try:
        config = _load_config()
    except Exception as e:
        return command_error(f"Failed to load configuration: {e}")

    doc_id = _resolve_document_for_edit(task_id, document_token, config)
    if doc_id is None:
        if not config.google_docs.document_ids:
            return validation_error("No documents configured.")
        return missing_fields_error(
            "delete_task",
            [{
                "field": "document_token",
                "description": "Multiple documents are configured and the task's document could not be inferred; specify one by id or alias.",
            }],
        )

    try:
        writer = _get_task_writer(_configured_tz(config))
        writer.delete_task(doc_id, task_id)
        _resync_document(doc_id, config)
    except TaskNotFoundError:
        return not_found_error("task", task_id)
    except Exception as e:
        return command_error(f"Delete failed: {e}")

    return {"ok": True, "task_id": task_id, "document_id": doc_id}


def deadlines_apply(
    task_id: Optional[str] = None,
    document_token: Optional[str] = None,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Backfill and write `deadline_computed` for eligible tasks in scope.

    Exactly one scope must be given: `task_id`, `document_token`, or
    `all_projects=True`. Use `deadlines_preview` first with the same scope to
    see what would be written without committing it.
    """
    from chronix.core.aggregation import TaskAggregator
    from chronix.core.deadline_backfill import compute_backlog_deadlines

    scopes_given = sum([task_id is not None, document_token is not None, all_projects])
    if scopes_given == 0:
        return validation_error("Specify exactly one of task_id, document_token, or all_projects=True.")
    if scopes_given > 1:
        return validation_error("Specify only one of task_id, document_token, or all_projects.")

    if not _context.projects:
        return validation_error("No projects loaded. Call sync first.")

    config = _load_config()
    doc_id = None
    if document_token is not None:
        doc_id = _resolve_document_token(document_token, config)
        if doc_id is None:
            return not_found_error("document", document_token)

    aggregator = TaskAggregator()
    aggregated_tasks = aggregator.aggregate(_context.projects)
    all_tasks = [agg.task for agg in aggregated_tasks]

    if task_id is not None and not any(t.id == task_id for t in all_tasks):
        return not_found_error("task", task_id)

    now = datetime.now(timezone.utc)
    results = compute_backlog_deadlines(all_tasks, now)

    if task_id is not None:
        results = [r for r in results if r.task.id == task_id]
    elif doc_id is not None:
        doc_task_ids = {
            t.id for p in _context.projects
            if p.project_context.document_id == doc_id
            for t in p.tasks
        }
        results = [r for r in results if r.task.id in doc_task_ids]

    if not results:
        return {"ok": True, "updated": 0, "message": "No eligible tasks in scope."}

    tz = _configured_tz(config)
    updated_ids: list[str] = []
    failures: list[dict[str, str]] = []
    for r in results:
        if r.task.id is None:
            failures.append({"title": r.task.title, "reason": "task has no id yet; call sync first"})
            continue
        update = TaskUpdate(metadata={KEY_DEADLINE_COMPUTED: serialize_deadline(r.deadline, tz)})
        outcome = _apply_update("deadlines_apply", r.task.id, update, None, config)
        if outcome.get("ok"):
            updated_ids.append(r.task.id)
        else:
            failures.append({"task_id": r.task.id, "reason": str(outcome.get("message") or outcome.get("error"))})

    return {"ok": len(failures) == 0, "updated": len(updated_ids), "updated_task_ids": updated_ids, "failures": failures}


def pause_block(label_or_kind: str) -> dict[str, Any]:
    """Pause a recurring config time block (sleep/break/meeting) for this server session only.

    The block stops counting as blocked time in `today`/`schedule` until
    resumed or the server restarts. config.toml itself is never modified.
    """
    from chronix.cli.commands import _block_config_key

    config = _load_config()
    all_blocks = config.scheduling.sleep_windows + config.scheduling.breaks + config.scheduling.meetings
    target = label_or_kind.strip().lower()

    if not any(_block_config_key(b) == target for b in all_blocks):
        return not_found_error("block", label_or_kind)

    _context.disabled_blocks.add(target)
    return {"ok": True, "paused": label_or_kind}


def resume_block(label_or_kind: str) -> dict[str, Any]:
    """Resume a previously paused recurring config time block, or pass "all" to resume every paused block."""
    if label_or_kind.strip().lower() == "all":
        count = len(_context.disabled_blocks)
        _context.disabled_blocks.clear()
        return {"ok": True, "resumed_count": count}

    from chronix.cli.commands import _block_config_key

    config = _load_config()
    all_blocks = config.scheduling.sleep_windows + config.scheduling.breaks + config.scheduling.meetings
    target = label_or_kind.strip().lower()

    if not any(_block_config_key(b) == target for b in all_blocks):
        return not_found_error("block", label_or_kind)

    _context.disabled_blocks.discard(target)
    return {"ok": True, "resumed": label_or_kind}
