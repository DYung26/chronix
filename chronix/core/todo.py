"""Task parsing and TODO list derivation from structured document content."""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from chronix.core.metadata import (
    KEY_ACTIVE_SINCE,
    KEY_ACTUAL_DURATION,
    KEY_CREATED,
    KEY_DEADLINE_COMPUTED,
    KEY_DEPENDS,
    KEY_DURATION,
    KEY_ESTIMATE,
    KEY_EXTERNAL_DEADLINE,
    KEY_ID,
    KEY_MODE,
    KEY_TRACK,
    KEY_REF,
    KEY_SESSIONS,
    KEY_USER_DEADLINE,
    parse_active_since,
    parse_created,
    parse_deadline,
    parse_duration,
    parse_metadata,
    parse_sessions,
)
from chronix.core.models import AdHocMeeting, Task, WorkSession


EXCLUDED_TAB_TITLES: frozenset[str] = frozenset(["todo"])


class TaskParseError(Exception):
    """Raised when task metadata cannot be parsed."""

    def __init__(
        self,
        message: str,
        raw_text: Optional[str] = None,
        field: Optional[str] = None,
        value: Optional[str] = None,
    ):
        self.message = message
        self.raw_text = raw_text
        self.field = field
        self.value = value
        super().__init__(message)

    def __str__(self) -> str:
        parts = [self.message]
        if self.field:
            parts.append(f"Field: {self.field}")
        if self.value is not None:
            parts.append(f"Value: {repr(self.value)}")
        if self.raw_text:
            text = self.raw_text if len(self.raw_text) <= 100 else self.raw_text[:97] + "..."
            parts.append(f"Raw text: {repr(text)}")
        return " | ".join(parts)

    def __repr__(self) -> str:
        return (
            f"TaskParseError(message={self.message!r}, "
            f"raw_text={self.raw_text!r}, "
            f"field={self.field!r}, "
            f"value={self.value!r})"
        )


class TaskParser:
    """Parses task lines with metadata into Task domain objects."""

    METADATA_PATTERN = re.compile(r'^(.*?)\s*:::\s*(.+)$')
    TASK_IDENTIFIER = "TASKS ::: id; estimate; actual_duration; sessions; active_since; external_deadline; user_deadline; deadline_computed; ref; deps; mode; track; created"
    VALID_MODES = {"atomic", "flex", "contiguous_preferred"}
    VALID_TRACKS = {"auto", "primary", "secondary"}

    def __init__(self, tz: timezone = timezone.utc):
        """`tz` is the timezone naive metadata datetimes are assumed to already be in.

        Should be the app's configured `scheduling.timezone`, since Google Docs
        is treated as the source of truth for those naive values as-is.
        """
        self.tz = tz

    def parse_task_line(
        self,
        paragraph: dict,
        checkbox_list_id: str | None,
        source: str = "google_docs",
    ) -> Optional[Task]:
        """Parse a paragraph into a Task if it contains valid task metadata.

        A paragraph is considered a task only if:
        1. It has a bullet field
        2. The bullet.list_id matches the document's checkbox_list_id
        3. It is NOT the identifier line itself
        4. It matches the task metadata pattern
        """
        bullet = paragraph.get('bullet')
        if bullet is None:
            return None
        if checkbox_list_id is None:
            return None
        if bullet.get('list_id') != checkbox_list_id:
            return None

        text = paragraph['text'].strip()
        if not text:
            return None

        if text == self.TASK_IDENTIFIER:
            return None

        match = self.METADATA_PATTERN.match(text)
        if not match:
            return None

        title = match.group(1).strip()
        metadata_str = match.group(2).strip()
        kv = parse_metadata(metadata_str)
        task_kwargs = self._parse_kv_metadata(kv, text)

        completed = bullet.get('has_strikethrough', False)
        task_kwargs.update({'title': title, 'completed': completed, 'source': source})
        return Task(**task_kwargs)

    def _parse_kv_metadata(self, kv: dict[str, str], raw_text: str) -> dict:
        """Build task kwargs from a key=value metadata dict."""
        estimate_str = kv.get(KEY_ESTIMATE) or kv.get(KEY_DURATION, "")
        duration = parse_duration(estimate_str)
        if duration is None:
            raise TaskParseError(
                message=f"Invalid estimate: '{estimate_str}'",
                raw_text=raw_text,
                field=KEY_ESTIMATE,
                value=estimate_str,
            )

        try:
            external_deadline = parse_deadline(kv.get(KEY_EXTERNAL_DEADLINE, "-"), self.tz)
        except ValueError as exc:
            raise TaskParseError(
                message=str(exc), raw_text=raw_text, field=KEY_EXTERNAL_DEADLINE
            ) from exc

        try:
            user_deadline = parse_deadline(kv.get(KEY_USER_DEADLINE, "-"), self.tz)
        except ValueError as exc:
            raise TaskParseError(
                message=str(exc), raw_text=raw_text, field=KEY_USER_DEADLINE
            ) from exc

        try:
            deadline_computed = parse_deadline(kv.get(KEY_DEADLINE_COMPUTED, "-"), self.tz)
        except ValueError as exc:
            raise TaskParseError(
                message=str(exc), raw_text=raw_text, field=KEY_DEADLINE_COMPUTED
            ) from exc

        mode = kv.get(KEY_MODE)
        if mode and mode not in self.VALID_MODES:
            raise TaskParseError(
                message=f"Invalid execution mode: '{mode}'. "
                        f"Valid modes: atomic, flex, contiguous_preferred",
                raw_text=raw_text,
                field=KEY_MODE,
                value=mode,
            )

        track = kv.get(KEY_TRACK)
        if track and track not in self.VALID_TRACKS:
            raise TaskParseError(
                message=f"Invalid track: '{track}'. "
                        f"Valid tracks: auto, primary, secondary",
                raw_text=raw_text,
                field=KEY_TRACK,
                value=track,
            )

        ref = kv.get(KEY_REF) or None
        depends_raw = kv.get(KEY_DEPENDS) or kv.get("depends", "")
        depends_on = [d.strip() for d in depends_raw.split(",") if d.strip()] if depends_raw else []

        created_raw = kv.get(KEY_CREATED, "")
        created = parse_created(created_raw, self.tz) if created_raw else None

        sessions: list[WorkSession] = []
        sessions_raw = kv.get(KEY_SESSIONS, "")
        if sessions_raw:
            for start, end in parse_sessions(sessions_raw, self.tz):
                try:
                    sessions.append(WorkSession(start=start, end=end))
                except ValueError:
                    continue

        actual_duration: Optional[timedelta] = None
        actual_duration_raw = kv.get(KEY_ACTUAL_DURATION, "")
        if actual_duration_raw:
            actual_duration = parse_duration(actual_duration_raw)

        active_since: Optional[datetime] = None
        active_since_raw = kv.get(KEY_ACTIVE_SINCE, "")
        if active_since_raw:
            active_since = parse_active_since(active_since_raw, self.tz)

        kwargs: dict = {
            "id": kv.get(KEY_ID) or None,
            "estimated_duration": duration,
            "deadline_external": external_deadline,
            "deadline_user": user_deadline,
            "deadline_computed": deadline_computed,
            "ref": ref,
            "depends_on": depends_on,
            "created": created,
            "sessions": sessions,
            "actual_duration": actual_duration,
            "active_since": active_since,
        }
        if mode:
            kwargs["execution_mode"] = mode
        if track:
            kwargs["track"] = track
        return kwargs


class MeetingParser:
    """Parses ad-hoc meeting lines from Google Docs into AdHocMeeting objects."""

    METADATA_PATTERN = re.compile(r'^MEETING\s*:::\s*(.+)$', re.IGNORECASE)
    MEETING_IDENTIFIER = "MEETING ::: start_time ; end_time ; optional_label"

    def __init__(self, tz: timezone = timezone.utc):
        """`tz` is the timezone a naive start/end time is assumed to already be in."""
        self.tz = tz

    def parse_meeting_line(
        self,
        paragraph: dict,
        checkbox_list_id: str | None,
        source: str = "google_docs",
    ) -> Optional[AdHocMeeting]:
        """Parse a paragraph into an AdHocMeeting if it matches the meeting format."""
        bullet = paragraph.get('bullet')
        if bullet is None:
            return None
        if checkbox_list_id is None:
            return None
        if bullet.get('list_id') != checkbox_list_id:
            return None

        text = paragraph['text'].strip()
        if not text:
            return None

        match = self.METADATA_PATTERN.match(text)
        if not match:
            return None

        metadata_str = match.group(1).strip()
        parts = [p.strip() for p in metadata_str.split(';')]
        if len(parts) < 2 or len(parts) > 3:
            raise TaskParseError(
                message=f"Invalid meeting format: expected 2-3 fields, got {len(parts)}. "
                        f"Format: start_time ; end_time ; optional_label",
                raw_text=text,
                field="metadata",
                value=metadata_str,
            )

        start_str, end_str = parts[0], parts[1]
        label = parts[2] if len(parts) == 3 else None

        start = self._parse_datetime(start_str, field="start_time", raw_text=text)
        end = self._parse_datetime(end_str, field="end_time", raw_text=text)

        if start >= end:
            raise TaskParseError(
                message="Meeting start time must be before end time",
                raw_text=text,
                field="time_order",
                value=f"{start} >= {end}",
            )

        return AdHocMeeting(start=start, end=end, label=label, source=source)

    def _parse_datetime(
        self,
        datetime_str: str,
        field: Optional[str] = None,
        raw_text: Optional[str] = None,
    ) -> datetime:
        try:
            dt = datetime.fromisoformat(datetime_str)
        except ValueError as exc:
            raise TaskParseError(
                message=f"Invalid datetime format: '{datetime_str}'. "
                        f"Expected ISO-8601 format",
                raw_text=raw_text,
                field=field or "datetime",
                value=datetime_str,
            ) from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt


def _extract_description(paragraphs: list[dict], task_index: int, task_indent_level: int) -> Optional[str]:
    """Collect the description block immediately following a task paragraph.

    A description is every contiguous paragraph after the task line whose
    indent_level exceeds the task's own indent_level -- i.e. every paragraph
    indented (via Tab) relative to the task's checkbox. It ends at the first
    paragraph back at or above that indent level (the next task, a heading,
    or end of tab), so it always reflects the document's current structure
    rather than a cached range that could go stale if the user edits the doc.

    Reverses the write-side markdown-bullet translation (see
    google_docs.writer._format_description_text): a paragraph that is itself
    a real Docs bullet is rendered back as "- [x] "/"- [ ] " (struck-through or
    not) rather than plain "- ", since the Docs API can't tell a checkbox
    bullet glyph apart from a plain disc/circle/square glyph on read (glyph
    type comes back empty/unspecified for checkboxes either way) -- so every
    real bullet round-trips through the checkbox syntax uniformly, with
    strikethrough as the only signal available for checked state. A paragraph
    that is plain text but already starts with a literal "-"/"* " is escaped
    with a leading backslash on the way out, so writing this description back
    doesn't misread that literal dash as a bullet marker on the next round-trip.
    """
    lines: list[str] = []
    for paragraph in paragraphs[task_index + 1:]:
        if paragraph.get('indent_level', 0) <= task_indent_level:
            break
        text = paragraph.get('text', '').strip()
        if not text:
            continue
        bullet = paragraph.get('bullet')
        if bullet is not None:
            marker = "- [x] " if bullet.get('has_strikethrough', False) else "- [ ] "
            lines.append(f"{marker}{text}")
        elif text[:2] in ("- ", "* "):
            lines.append(f"\\{text}")
        else:
            lines.append(text)
    return "\n".join(lines) if lines else None


class TodoDeriver:
    """Derives canonical TODO list from document structures."""

    def __init__(self, parser: Optional[TaskParser] = None, tz: timezone = timezone.utc):
        """`tz` is used to construct the default TaskParser/MeetingParser when
        an explicit `parser` isn't supplied, and always for meeting parsing.
        """
        self.tz = tz
        self.parser = parser or TaskParser(tz=tz)

    def derive_todo_list(
        self,
        document_structure: dict,
        exclude_tab_titles: Optional[list[str]] = None,
    ) -> list[Task]:
        """Derive TODO list from tabs, excluding specified tab titles."""
        if exclude_tab_titles is None:
            exclude_tab_titles = EXCLUDED_TAB_TITLES

        exclude_normalized = {t.lower() for t in exclude_tab_titles}
        tasks = []
        document_title = document_structure.get('title', '').strip()

        for tab in document_structure.get('tabs', []):
            try:
                tab_title = tab.get('title', '').strip()
                if tab_title.lower() in exclude_normalized:
                    continue

                checkbox_list_id = tab.get('checkbox_list_id')
                if checkbox_list_id is None:
                    raise TaskParseError(
                        message=f"No checkbox list ID found in tab '{tab_title}'. "
                                f"Tab must contain a checkbox line with text: "
                                f"'{TaskParser.TASK_IDENTIFIER}'",
                        raw_text=None,
                        field="checkbox_list_id",
                        value=None,
                    )

                paragraphs = tab.get('paragraphs', [])
                for index, paragraph in enumerate(paragraphs):
                    style = paragraph.get('style', 'NORMAL_TEXT')
                    if style in ['HEADING_1', 'HEADING_2', 'HEADING_3']:
                        continue
                    try:
                        task = self.parser.parse_task_line(paragraph, checkbox_list_id)
                        if task:
                            if document_title:
                                task.document_title = document_title
                            if tab_title:
                                task.section = tab_title
                            task.description = _extract_description(
                                paragraphs, index, paragraph.get('indent_level', 0)
                            )
                            tasks.append(task)
                    except TaskParseError:
                        continue
            except TaskParseError:
                continue

        return tasks

    def derive_meetings_list(
        self,
        document_structure: dict,
        exclude_tab_titles: Optional[list[str]] = None,
    ) -> list[AdHocMeeting]:
        """Derive list of ad-hoc meetings from document structure."""
        if exclude_tab_titles is None:
            exclude_tab_titles = EXCLUDED_TAB_TITLES

        exclude_normalized = {t.lower() for t in exclude_tab_titles}
        meetings = []
        meeting_parser = MeetingParser(tz=self.tz)

        for tab in document_structure.get('tabs', []):
            try:
                tab_title = tab.get('title', '').strip()
                if tab_title.lower() in exclude_normalized:
                    continue

                checkbox_list_id = tab.get('checkbox_list_id')
                if checkbox_list_id is None:
                    continue

                for paragraph in tab.get('paragraphs', []):
                    try:
                        meeting = meeting_parser.parse_meeting_line(paragraph, checkbox_list_id)
                        if meeting:
                            meetings.append(meeting)
                    except TaskParseError:
                        continue
            except TaskParseError:
                continue

        return meetings


def parse_document_tasks(
    document_structure: dict,
    source: str = "google_docs",
    tz: timezone = timezone.utc,
) -> list[Task]:
    """Parse all tasks from a document structure with tabs."""
    parser = TaskParser(tz=tz)
    tasks = []
    document_title = document_structure.get('title', '').strip()

    for tab in document_structure.get('tabs', []):
        tab_title = tab.get('title', '').strip()
        checkbox_list_id = tab.get('checkbox_list_id')
        if checkbox_list_id is None:
            continue
        paragraphs = tab.get('paragraphs', [])
        for index, paragraph in enumerate(paragraphs):
            try:
                task = parser.parse_task_line(paragraph, checkbox_list_id, source=source)
                if task:
                    if document_title:
                        task.document_title = document_title
                    if tab_title:
                        task.section = tab_title
                    task.description = _extract_description(
                        paragraphs, index, paragraph.get('indent_level', 0)
                    )
                    tasks.append(task)
            except TaskParseError:
                continue

    return tasks


def derive_todo_list(
    document_structure: dict,
    exclude_tab_titles: Optional[list[str]] = None,
    tz: timezone = timezone.utc,
) -> list[Task]:
    """Derive and sort the canonical TODO list from a document with tabs."""
    return TodoDeriver(tz=tz).derive_todo_list(document_structure, exclude_tab_titles)


def parse_document_meetings(
    document_structure: dict,
    exclude_tab_titles: Optional[list[str]] = None,
    tz: timezone = timezone.utc,
) -> list[AdHocMeeting]:
    """Parse all ad-hoc meetings from a document structure with tabs."""
    return TodoDeriver(tz=tz).derive_meetings_list(document_structure, exclude_tab_titles)
