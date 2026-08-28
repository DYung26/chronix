"""Local file implementation of the TaskWriter interface."""

from datetime import timezone
from pathlib import Path
from typing import Any

from chronix.core.metadata import KEY_ID, parse_metadata
from chronix.core.models import Task, generate_task_id
from chronix.core.writer import NewTask, TaskNotFoundError, TaskUpdate, TaskWriter
from chronix.integrations.google_docs.writer import _apply_update_to_task_line, format_task_line
from chronix.integrations.local_files.parser import _CHECKBOX_PATTERN, _SECTION_PATTERN


class LocalFileTaskWriter(TaskWriter):
    """Writes tasks to a local plain-text task file by rewriting its lines.

    ``document_id`` is the file's path (as returned by LocalFileClient). The
    ``tab``/section token on NewTask, when set, matches a "## Section" heading
    line; if unset or unmatched, the task is appended to the first section.
    """

    def __init__(self, tz: timezone = timezone.utc):
        self._tz = tz

    def create_task(self, document_id: str, task: NewTask) -> None:
        path = Path(document_id)
        lines = _read_lines(path)
        task_line = f"- [ ] {format_task_line(task, self._tz)}"
        insert_at = _find_section_insertion_point(lines, task.tab)
        lines.insert(insert_at, task_line)
        _write_lines(path, lines)

    def update_task(self, document_id: str, task_id: str, update: TaskUpdate) -> None:
        path = Path(document_id)
        lines = _read_lines(path)
        index = _find_task_line_index(lines, task_id)
        if index is None:
            raise TaskNotFoundError(task_id)

        checked, current_text = _split_checkbox_line(lines[index])
        new_text = _apply_update_to_task_line(current_text, update, self._tz)
        if update.completed is not None:
            checked = update.completed

        lines[index] = _format_checkbox_line(checked, new_text)

        if update.has_description_change():
            _replace_description(lines, index, update.description)

        _write_lines(path, lines)

    def delete_task(self, document_id: str, task_id: str) -> None:
        path = Path(document_id)
        lines = _read_lines(path)
        index = _find_task_line_index(lines, task_id)
        if index is None:
            raise TaskNotFoundError(task_id)

        end = _description_block_end(lines, index)
        del lines[index:end]
        _write_lines(path, lines)

    def backfill_missing_ids(
        self,
        document_id: str,
        tasks: list[Task],
        source_data: Any = None,
    ) -> list[Task]:
        tasks_needing_ids = [t for t in tasks if t.id is None]
        if not tasks_needing_ids:
            return tasks

        path = Path(document_id)
        lines = source_data if source_data is not None else _read_lines(path)

        title_cursor: dict[str, int] = {}
        id_map: dict[int, str] = {}
        for line_index, line in enumerate(lines):
            checked_and_text = _try_split_checkbox_line(line)
            if checked_and_text is None:
                continue
            _, text = checked_and_text
            if " ::: " not in text:
                continue
            title, meta_str = text.split(" ::: ", 1)
            title = title.strip()
            if parse_metadata(meta_str).get(KEY_ID):
                continue

            matches = [t for t in tasks_needing_ids if t.title == title]
            cursor = title_cursor.get(title, 0)
            if cursor >= len(matches):
                continue
            title_cursor[title] = cursor + 1

            task_id = generate_task_id()
            lines[line_index] = _insert_id_into_line(lines[line_index], task_id)
            id_map[id(matches[cursor])] = task_id

        _write_lines(path, lines)
        return [
            task.model_copy(update={"id": id_map[id(task)]}) if id(task) in id_map else task
            for task in tasks
        ]


def _read_lines(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Local task file not found: {path}")
    return path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _split_checkbox_line(line: str) -> tuple[bool, str]:
    result = _try_split_checkbox_line(line)
    if result is None:
        raise ValueError(f"Not a checkbox line: {line!r}")
    return result


def _try_split_checkbox_line(line: str) -> tuple[bool, str] | None:
    match = _CHECKBOX_PATTERN.match(line.strip())
    if not match:
        return None
    checked, text = match.groups()
    return checked != "", text.strip()


def _format_checkbox_line(checked: bool, text: str) -> str:
    marker = "x" if checked else " "
    return f"- [{marker}] {text}"


def _insert_id_into_line(line: str, task_id: str) -> str:
    checked, text = _split_checkbox_line(line)
    title, meta_str = text.split(" ::: ", 1)
    return _format_checkbox_line(checked, f"{title.strip()} ::: {KEY_ID}={task_id}; {meta_str}")


def _find_task_line_index(lines: list[str], task_id: str) -> int | None:
    for index, line in enumerate(lines):
        result = _try_split_checkbox_line(line)
        if result is None:
            continue
        _, text = result
        if " ::: " not in text:
            continue
        meta_str = text.split(" ::: ", 1)[1]
        if parse_metadata(meta_str).get(KEY_ID) == task_id:
            return index
    return None


def _find_section_insertion_point(lines: list[str], section_token: str | None) -> int:
    """Find the line index to insert a new task at.

    If ``section_token`` matches a "## Section" heading (case-insensitive),
    the task is inserted at the end of that section's checkbox block.
    Otherwise, the task is inserted at the end of the first checkbox block
    found, or appended to the end of the file if none exists.
    """
    target_section_index = _find_section_heading(lines, section_token) if section_token else None

    if target_section_index is not None:
        return _end_of_section_checkbox_block(lines, target_section_index)

    for index, line in enumerate(lines):
        if _CHECKBOX_PATTERN.match(line.strip()):
            return _end_of_checkbox_run(lines, index)

    return len(lines)


def _find_section_heading(lines: list[str], section_token: str) -> int | None:
    token_lower = section_token.strip().lower()
    for index, line in enumerate(lines):
        match = _SECTION_PATTERN.match(line)
        if match and match.group(1).strip().lower() == token_lower:
            return index
    return None


def _end_of_section_checkbox_block(lines: list[str], section_heading_index: int) -> int:
    for index in range(section_heading_index + 1, len(lines)):
        if _SECTION_PATTERN.match(lines[index]):
            return _end_of_checkbox_run(lines, section_heading_index + 1, before=index)
    return _end_of_checkbox_run(lines, section_heading_index + 1)


def _end_of_checkbox_run(lines: list[str], start: int, before: int | None = None) -> int:
    end = before if before is not None else len(lines)
    last_checkbox = None
    for index in range(start, end):
        if _CHECKBOX_PATTERN.match(lines[index].strip()):
            last_checkbox = index
    if last_checkbox is None:
        return end
    return _description_block_end(lines, last_checkbox)


def _description_block_end(lines: list[str], task_line_index: int) -> int:
    end = task_line_index + 1
    while end < len(lines) and lines[end][:1].isspace() and lines[end].strip():
        end += 1
    return end


def _replace_description(lines: list[str], task_line_index: int, description: str | None) -> None:
    end = _description_block_end(lines, task_line_index)
    del lines[task_line_index + 1:end]
    if description:
        description_lines = [f"    {line}" for line in description.split("\n")]
        lines[task_line_index + 1:task_line_index + 1] = description_lines
