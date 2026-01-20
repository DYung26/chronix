"""Task parsing and TODO list derivation from structured document content."""

import re
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from chronix.core.models import Task


class TaskParseError(Exception):
    """Raised when task metadata cannot be parsed.
    
    Attributes:
        message: Human-readable error description
        raw_text: The original text being parsed (if available)
        field: The specific field that failed to parse (if applicable)
        value: The value that caused the error (if applicable)
    """
    
    def __init__(
        self,
        message: str,
        raw_text: Optional[str] = None,
        field: Optional[str] = None,
        value: Optional[str] = None
    ):
        self.message = message
        self.raw_text = raw_text
        self.field = field
        self.value = value
        super().__init__(message)
    
    def __str__(self) -> str:
        """Format error with context for debugging."""
        parts = [self.message]
        
        if self.field:
            parts.append(f"Field: {self.field}")
        
        if self.value is not None:
            parts.append(f"Value: {repr(self.value)}")
        
        if self.raw_text:
            # Truncate if too long
            text = self.raw_text if len(self.raw_text) <= 100 else self.raw_text[:97] + "..."
            parts.append(f"Raw text: {repr(text)}")
        
        return " | ".join(parts)
    
    def __repr__(self) -> str:
        """Representation for debugging."""
        return (
            f"TaskParseError(message={self.message!r}, "
            f"raw_text={self.raw_text!r}, "
            f"field={self.field!r}, "
            f"value={self.value!r})"
        )


class TaskParser:
    """Parses task lines with metadata into Task domain objects."""
    
    METADATA_PATTERN = re.compile(r'^(.*?)\s*:::\s*(.+)$')
    DURATION_PATTERN = re.compile(r'^(\d+)(hours?|minutes?)$', re.IGNORECASE)
    TASK_IDENTIFIER = "TASKS ::: duration; external_deadline; user_deadline"
    
    def parse_task_line(
        self, 
        paragraph: dict, 
        checkbox_list_id: str | None,
        source: str = "google_docs"
    ) -> Optional[Task]:
        """Parse a paragraph into a Task if it contains valid task metadata.
        
        A paragraph is considered a task only if:
        1. It has a bullet field
        2. The bullet.list_id matches the document's checkbox_list_id
        3. It is NOT the identifier line itself
        4. It matches the task metadata pattern (title ::: duration ; deadline ; deadline)
        
        Args:
            paragraph: The paragraph dictionary to parse
            checkbox_list_id: The discovered checkbox list ID for this document
            source: The source system (default: "google_docs")
        
        Returns:
            Task object if valid, None otherwise
        """
        # Check if it's a checkbox bullet (only checkbox bullets are tasks)
        bullet = paragraph.get('bullet')
        if bullet is None:
            return None

        # Require a valid checkbox list ID to be discovered
        if checkbox_list_id is None:
            return None

        # Only checkbox list items are tasks (match discovered list ID)
        if bullet.get('list_id') != checkbox_list_id:
            return None

        text = paragraph['text'].strip()
        if not text:
            return None

        # Exclude the identifier line itself (it's not a real task)
        if text == self.TASK_IDENTIFIER:
            return None

        match = self.METADATA_PATTERN.match(text)
        if not match:
            return None

        title = match.group(1).strip()
        metadata_str = match.group(2).strip()

        parts = [p.strip() for p in metadata_str.split(';')]
        if len(parts) != 3:
            raise TaskParseError(
                message=f"Invalid metadata format: expected 3 fields, got {len(parts)}. "
                        f"Format: duration ; external_deadline ; user_deadline",
                raw_text=text,
                field="metadata",
                value=metadata_str
            )

        duration_str, external_deadline_str, user_deadline_str = parts

        try:
            duration = self._parse_duration(duration_str, raw_text=text)
        except TaskParseError:
            raise
        
        try:
            external_deadline = self._parse_deadline(external_deadline_str, field="external_deadline", raw_text=text)
        except TaskParseError:
            raise

        try:
            user_deadline = self._parse_deadline(user_deadline_str, field="user_deadline", raw_text=text)
        except TaskParseError:
            raise

        completed = bullet.get('has_strikethrough', False)

        return Task(
            title=title,
            estimated_duration=duration,
            deadline_external=external_deadline,
            deadline_user=user_deadline,
            completed=completed,
            source=source
        )

    def _parse_duration(self, duration_str: str, raw_text: Optional[str] = None) -> timedelta:
        """Parse duration string into timedelta."""
        if duration_str == '-':
            raise TaskParseError(
                message="Duration cannot be unspecified (use a value, not '-')",
                raw_text=raw_text,
                field="duration",
                value=duration_str
            )

        match = self.DURATION_PATTERN.match(duration_str)
        if not match:
            raise TaskParseError(
                message=f"Invalid duration format: '{duration_str}'. "
                        f"Expected format: <number>hours or <number>minutes",
                raw_text=raw_text,
                field="duration",
                value=duration_str
            )

        value = int(match.group(1))
        unit = match.group(2).lower()

        if value <= 0:
            raise TaskParseError(
                message=f"Duration must be positive, got {value}",
                raw_text=raw_text,
                field="duration",
                value=duration_str
            )

        if unit.startswith('hour'):
            return timedelta(hours=value)
        elif unit.startswith('minute'):
            return timedelta(minutes=value)
        else:
            raise TaskParseError(
                message=f"Unknown duration unit: {unit}",
                raw_text=raw_text,
                field="duration",
                value=duration_str
            )

    def _parse_deadline(
        self, 
        deadline_str: str, 
        field: Optional[str] = None,
        raw_text: Optional[str] = None
    ) -> Optional[datetime]:
        """Parse deadline string into timezone-aware datetime."""
        if deadline_str == '-':
            return None

        try:
            dt = datetime.fromisoformat(deadline_str)
        except ValueError as e:
            raise TaskParseError(
                message=f"Invalid deadline format: '{deadline_str}'. "
                        f"Expected ISO-8601 format (e.g., 2026-01-09T12:00 or 2026-01-09T12:00+00:00)",
                raw_text=raw_text,
                field=field or "deadline",
                value=deadline_str
            ) from e

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt


class TodoDeriver:
    """Derives canonical TODO list from document structures."""

    def __init__(self, parser: Optional[TaskParser] = None):
        self.parser = parser or TaskParser()

    def derive_todo_list(
        self,
        document_structure: dict,
        exclude_tab_titles: Optional[list[str]] = None
    ) -> list[Task]:
        """Derive TODO list from tabs, excluding specified tab titles.

        Args:
            document_structure: Parsed document with tabs
            exclude_tab_titles: Tab titles to exclude (e.g., ["todo"]). 
                                Case-insensitive comparison.

        Returns:
            Sorted list of tasks from non-excluded tabs
        """
        if exclude_tab_titles is None:
            exclude_tab_titles = ['todo']

        # Normalize exclusion list for case-insensitive comparison
        exclude_normalized = [t.lower() for t in exclude_tab_titles]

        tasks = []

        # Process each tab
        tabs = document_structure.get('tabs', [])
        for tab in tabs:
            try:
                tab_title = tab.get('title', '').strip()

                # Skip excluded tabs
                if tab_title.lower() in exclude_normalized:
                    continue

                # Get the checkbox list ID for this tab
                checkbox_list_id = tab.get('checkbox_list_id')

                # If no checkbox list ID found for this tab, raise error
                if checkbox_list_id is None:
                    raise TaskParseError(
                        message=f"No checkbox list ID found in tab '{tab_title}'. "
                                f"Tab must contain a checkbox line with text: "
                                f"'TASKS ::: duration; external_deadline; user_deadline'",
                        raw_text=None,
                        field="checkbox_list_id",
                        value=None
                    )

                # Process paragraphs in this tab
                paragraphs = tab.get('paragraphs', [])
                for paragraph in paragraphs:
                    # Track section context using heading styles
                    style = paragraph.get('style', 'NORMAL_TEXT')

                    # Update section context if this is a heading
                    if style in ['HEADING_1', 'HEADING_2', 'HEADING_3']:
                        current_section = paragraph.get('text', '').strip()
                        continue

                    # Try to parse as task
                    try:
                        task = self.parser.parse_task_line(paragraph, checkbox_list_id)
                        if task:
                            # Optionally add tab context
                            if tab_title:
                                task.section = tab_title
                            tasks.append(task)
                    except TaskParseError:
                        # Ignore lines that can't be parsed as tasks
                        continue
            except TaskParseError:
                continue

        # Do NOT sort here - tasks will be sorted globally after aggregation
        return tasks

    def _extract_section_name(self, text: str) -> str:
        """Extract section name from heading text."""
        return text.strip()


def parse_document_tasks(
    document_structure: dict,
    source: str = "google_docs"
) -> list[Task]:
    """Parse all tasks from a document structure with tabs."""
    parser = TaskParser()
    tasks = []
    
    tabs = document_structure.get('tabs', [])
    for tab in tabs:
        # Get the checkbox list ID for this tab
        checkbox_list_id = tab.get('checkbox_list_id')
        
        # If no checkbox list ID found for this tab, skip it
        if checkbox_list_id is None:
            continue
        
        paragraphs = tab.get('paragraphs', [])
        for paragraph in paragraphs:
            try:
                task = parser.parse_task_line(paragraph, checkbox_list_id, source=source)
                if task:
                    tasks.append(task)
            except TaskParseError:
                continue
    
    return tasks


def derive_todo_list(
    document_structure: dict,
    exclude_tab_titles: Optional[list[str]] = None
) -> list[Task]:
    """Derive and sort the canonical TODO list from a document with tabs."""
    deriver = TodoDeriver()
    return deriver.derive_todo_list(document_structure, exclude_tab_titles)
