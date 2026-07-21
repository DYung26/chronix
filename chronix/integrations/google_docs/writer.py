"""Google Docs implementation of the TaskWriter interface."""

from collections import defaultdict
from datetime import timezone
from typing import Any

from chronix.core.metadata import (
    KEY_ACTIVE_SINCE,
    KEY_ACTUAL_DURATION,
    KEY_CREATED,
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
    WELL_KNOWN_KEYS,
    parse_metadata,
    serialize_created,
    serialize_deadline,
    serialize_duration,
    serialize_metadata,
)
from chronix.core.models import Task, generate_task_id
from chronix.core.todo import EXCLUDED_TAB_TITLES, TaskParser
from chronix.core.writer import NewTask, TaskNotFoundError, TaskUpdate, TaskWriter
from chronix.integrations.google_docs.auth import AuthStrategy, get_default_auth_strategy


class GoogleDocsTaskWriter(TaskWriter):
    """Writes tasks to a Google Docs document using the batchUpdate API.

    Finds the empty placeholder checkbox immediately after the TASKS identifier
    and inserts the new task by splitting that line. The split leaves a new
    empty checkbox above the inserted task, preserving the placeholder convention.
    If no empty placeholder exists it is created before insertion.
    The new paragraph inherits checkbox list membership from the split point.
    """

    def __init__(self, auth_strategy: AuthStrategy | None = None, tz: timezone = timezone.utc):
        """`tz` is the timezone naive metadata datetimes are read/written in.

        Should be the app's configured `scheduling.timezone`, since Google Docs
        is treated as the source of truth for those naive values as-is.
        """
        self._auth = auth_strategy or get_default_auth_strategy()
        self._service = None
        self._tz = tz

    @property
    def _docs_service(self) -> Any:
        if self._service is None:
            self._service = self._auth.get_service()
        return self._service

    def create_task(self, document_id: str, task: NewTask) -> None:
        doc = self._docs_service.documents().get(
            documentId=document_id,
            includeTabsContent=True,
        ).execute()

        placeholder_index, placeholder_existed, tab_id = self._find_insertion_point(doc, task.tab)

        location: dict[str, Any] = {"index": placeholder_index}
        if tab_id is not None:
            location["tabId"] = tab_id

        task_line = format_task_line(task, self._tz)
        requests: list[dict[str, Any]] = []

        if not placeholder_existed:
            requests.append({"insertText": {"location": location, "text": "\n"}})
            task_insert_index = placeholder_index + 1
        else:
            task_insert_index = placeholder_index

        task_location: dict[str, Any] = {"index": task_insert_index}
        if tab_id is not None:
            task_location["tabId"] = tab_id

        requests.append({"insertText": {"location": task_location, "text": "\n" + task_line}})

        metadata_offset = task_line.index(" :::")
        bold_start = task_insert_index + 1 + metadata_offset
        bold_end = task_insert_index + 1 + len(task_line)
        bold_location: dict[str, Any] = {"startIndex": bold_start, "endIndex": bold_end}
        if tab_id is not None:
            bold_location["tabId"] = tab_id
        requests.append({
            "updateTextStyle": {
                "range": bold_location,
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })

        self._docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute()

    def update_task(self, document_id: str, task_id: str, update: TaskUpdate) -> None:
        """Apply a generic update to a task identified by its persistent ID.

        Fetches the current document, locates the task element by id=, rewrites
        the task line in a single batchUpdate call.  The revision_id from the
        fetch is available on the response and can be used for optimistic
        concurrency checks in future work.
        """
        doc = self._docs_service.documents().get(
            documentId=document_id,
            includeTabsContent=True,
        ).execute()

        element, tab_id = _find_task_element_by_id(doc, task_id)
        if element is None:
            raise TaskNotFoundError(task_id)

        requests = _build_update_requests(element, tab_id, update, self._tz)
        if requests:
            self._docs_service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": requests},
            ).execute()

    def delete_task(self, document_id: str, task_id: str) -> None:
        """Remove a task paragraph from the document by its persistent ID."""
        doc = self._docs_service.documents().get(
            documentId=document_id,
            includeTabsContent=True,
        ).execute()

        element, tab_id = _find_task_element_by_id(doc, task_id)
        if element is None:
            raise TaskNotFoundError(task_id)

        start_index: int = element.get("startIndex", 0)
        end_index: int = element.get("endIndex", 0)

        range_: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
        if tab_id is not None:
            range_["tabId"] = tab_id

        self._docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"deleteContentRange": {"range": range_}}]},
        ).execute()

    def _find_insertion_point(
        self, doc: dict[str, Any], tab_token: str | None = None
    ) -> tuple[int, bool, str | None]:
        tabs = doc.get("tabs", [])
        if tabs:
            if tab_token is not None:
                tab_data = _find_tab_by_token(tabs, tab_token)
                if tab_data is None:
                    raise ValueError(
                        f"Tab '{tab_token}' not found in document. "
                        f"Available tabs: {_format_tab_titles(tabs)}"
                    )
                result = self._find_insertion_in_tab(tab_data, allow_excluded=True)
                if result is None:
                    raise ValueError(f"No TASKS section found in tab '{tab_token}'")
                return result
            for tab_data in tabs:
                result = self._find_insertion_in_tab(tab_data)
                if result is not None:
                    return result
        else:
            content = doc.get("body", {}).get("content", [])
            result = _find_placeholder_index(content)
            if result is not None:
                index, existed = result
                return index, existed, None

        raise ValueError("No TASKS section found in document")

    def _find_insertion_in_tab(
        self, tab_data: dict[str, Any], allow_excluded: bool = False
    ) -> tuple[int, bool, str] | None:
        tab_id = tab_data.get("tabProperties", {}).get("tabId", "")
        title = tab_data.get("tabProperties", {}).get("title", "")
        if not allow_excluded and title.lower() in EXCLUDED_TAB_TITLES:
            return None
        content = tab_data.get("documentTab", {}).get("body", {}).get("content", [])
        result = _find_placeholder_index(content)
        if result is None:
            return None
        index, existed = result
        return index, existed, tab_id

    def backfill_missing_ids(
        self,
        document_id: str,
        tasks: list[Task],
        source_data: Any = None,
    ) -> list[Task]:
        tasks_needing_ids = [t for t in tasks if t.id is None]
        if not tasks_needing_ids:
            return tasks

        doc = source_data if source_data is not None else self._docs_service.documents().get(
            documentId=document_id, includeTabsContent=True
        ).execute()

        elements_without_id = _find_task_elements_without_id(doc)

        by_title: dict[str, list[tuple[dict[str, Any], str | None]]] = defaultdict(list)
        for element, tab_id in elements_without_id:
            title = _paragraph_text(element.get("paragraph", {})).split(" ::: ", 1)[0].strip()
            by_title[title].append((element, tab_id))

        title_cursor: dict[str, int] = defaultdict(int)
        assignments: list[tuple[Task, str, dict[str, Any], str | None]] = []
        for task in tasks_needing_ids:
            candidates = by_title.get(task.title, [])
            idx = title_cursor[task.title]
            if idx < len(candidates):
                element, tab_id = candidates[idx]
                title_cursor[task.title] += 1
                assignments.append((task, generate_task_id(), element, tab_id))

        assignments_desc = sorted(
            assignments,
            key=lambda a: _separator_insert_index(a[2]),
            reverse=True,
        )

        requests: list[dict[str, Any]] = []
        for _, task_id, element, tab_id in assignments_desc:
            insert_index = _separator_insert_index(element)
            if insert_index < 0:
                continue
            location: dict[str, Any] = {"index": insert_index}
            if tab_id is not None:
                location["tabId"] = tab_id
            requests.append({"insertText": {"location": location, "text": f"{KEY_ID}={task_id}; "}})

        if requests:
            self._docs_service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": requests},
            ).execute()

        id_map: dict[int, str] = {id(task): task_id for task, task_id, _, _ in assignments}
        return [
            task.model_copy(update={"id": id_map[id(task)]}) if id(task) in id_map else task
            for task in tasks
        ]


# ---------------------------------------------------------------------------
# Line serialization
# ---------------------------------------------------------------------------

def format_task_line(task: NewTask, tz: timezone = timezone.utc) -> str:
    """Serialize a NewTask to the canonical key=value task line.

    ``id`` is included only when set on the task; callers that want a persistent
    id assigned immediately (rather than later via sync's backfill) must pass one.
    A ``created`` timestamp is always included.  If ``task.created`` is set it
    is used as-is; otherwise the current UTC time is stamped automatically.
    `tz` is the timezone naive deadline/created values are written in.
    """
    from datetime import datetime

    fields: dict[str, str] = {}
    if task.id is not None:
        fields[KEY_ID] = task.id
    fields[KEY_ESTIMATE] = serialize_duration(task.duration)

    if task.external_deadline is not None:
        fields[KEY_EXTERNAL_DEADLINE] = serialize_deadline(task.external_deadline, tz)
    if task.user_deadline is not None:
        fields[KEY_USER_DEADLINE] = serialize_deadline(task.user_deadline, tz)
    if task.mode is not None:
        fields[KEY_MODE] = task.mode
    if task.track is not None:
        fields[KEY_TRACK] = task.track
    if task.ref is not None:
        fields[KEY_REF] = task.ref
    if task.depends is not None:
        fields[KEY_DEPENDS] = task.depends
    created = task.created if task.created is not None else datetime.now(timezone.utc)
    fields[KEY_CREATED] = serialize_created(created, tz)
    for k, v in task.extra.items():
        fields[k] = v

    return f"{task.title} ::: {serialize_metadata(fields)}"


def _apply_update_to_task_line(current_line: str, update: TaskUpdate, tz: timezone = timezone.utc) -> str:
    """Produce the updated task line string from the current line and an update.

    Preserves the existing id= and all unknown metadata keys.
    Only fields explicitly set on ``update`` are changed.
    Migrates legacy ``duration=`` key to ``estimate=`` on any update.
    `tz` is the timezone naive deadline values are written in.
    """
    sep = " ::: "
    if sep not in current_line:
        return current_line

    title, meta_str = current_line.split(sep, 1)
    kv = parse_metadata(meta_str)

    # Migrate legacy duration key to estimate on any update
    if KEY_DURATION in kv:
        if KEY_ESTIMATE not in kv:
            kv[KEY_ESTIMATE] = kv.pop(KEY_DURATION)
        else:
            del kv[KEY_DURATION]

    if update.title is not None:
        title = update.title

    if update.duration is not None:
        kv[KEY_ESTIMATE] = serialize_duration(update.duration)

    if update.has_external_deadline_change():
        kv[KEY_EXTERNAL_DEADLINE] = serialize_deadline(update.external_deadline, tz)  # type: ignore[arg-type]

    if update.has_user_deadline_change():
        kv[KEY_USER_DEADLINE] = serialize_deadline(update.user_deadline, tz)  # type: ignore[arg-type]

    if update.mode is not None:
        kv[KEY_MODE] = update.mode

    if update.track is not None:
        kv[KEY_TRACK] = update.track

    for k, v in update.metadata.items():
        kv[k.lower()] = v

    for k in update.metadata_remove:
        kv.pop(k.lower(), None)

    ordered: dict[str, str] = {}
    for wk in WELL_KNOWN_KEYS:
        if wk in kv:
            ordered[wk] = kv[wk]
    for k, v in kv.items():
        if k not in ordered:
            ordered[k] = v

    return f"{title.strip()}{sep}{serialize_metadata(ordered)}"


# ---------------------------------------------------------------------------
# Update request builder
# ---------------------------------------------------------------------------

def _build_update_requests(
    element: dict[str, Any],
    tab_id: str | None,
    update: TaskUpdate,
    tz: timezone = timezone.utc,
) -> list[dict[str, Any]]:
    """Build batchUpdate requests to apply ``update`` to the given element."""
    requests: list[dict[str, Any]] = []

    start_index: int = element.get("startIndex", 0)
    end_index: int = element.get("endIndex", 0)
    text_end = end_index - 1

    current_text = _paragraph_text(element.get("paragraph", {}))
    new_text = _apply_update_to_task_line(current_text, update, tz)

    text_changed = new_text != current_text

    if text_changed:
        range_: dict[str, Any] = {"startIndex": start_index, "endIndex": text_end}
        if tab_id is not None:
            range_["tabId"] = tab_id

        location: dict[str, Any] = {"index": start_index}
        if tab_id is not None:
            location["tabId"] = tab_id

        requests.append({"deleteContentRange": {"range": range_}})
        requests.append({"insertText": {"location": location, "text": new_text}})

        metadata_offset = new_text.find(" :::")
        if metadata_offset != -1:
            bold_range: dict[str, Any] = {
                "startIndex": start_index + metadata_offset,
                "endIndex": start_index + len(new_text),
            }
            if tab_id is not None:
                bold_range["tabId"] = tab_id
            requests.append({
                "updateTextStyle": {
                    "range": bold_range,
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })

    if update.completed is not None:
        style_range: dict[str, Any] = {
            "startIndex": start_index,
            "endIndex": text_end,
        }
        if tab_id is not None:
            style_range["tabId"] = tab_id
        requests.append({
            "updateTextStyle": {
                "range": style_range,
                "textStyle": {"strikethrough": update.completed},
                "fields": "strikethrough",
            }
        })

    return requests


# ---------------------------------------------------------------------------
# Document traversal helpers
# ---------------------------------------------------------------------------

def _paragraph_text(paragraph: dict[str, Any]) -> str:
    parts = []
    for elem in paragraph.get("elements", []):
        tr = elem.get("textRun", {})
        if "suggestedInsertionIds" in tr or "suggestedDeletionIds" in tr:
            continue
        parts.append(tr.get("content", ""))
    return "".join(parts).strip()


def _find_tab_by_token(tabs: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    """Resolve a tab token (tabId, exact match, or title, case-insensitive) to its tab data."""
    for tab_data in tabs:
        if tab_data.get("tabProperties", {}).get("tabId") == token:
            return tab_data
    token_lower = token.strip().lower()
    for tab_data in tabs:
        title = tab_data.get("tabProperties", {}).get("title", "")
        if title.strip().lower() == token_lower:
            return tab_data
    return None


def _format_tab_titles(tabs: list[dict[str, Any]]) -> str:
    titles = [tab_data.get("tabProperties", {}).get("title") or "(untitled)" for tab_data in tabs]
    return ", ".join(titles) if titles else "(none)"


def _find_checkbox_list_id(content: list[dict[str, Any]]) -> str | None:
    for element in content:
        if "paragraph" not in element:
            continue
        paragraph = element["paragraph"]
        if "bullet" not in paragraph:
            continue
        if _paragraph_text(paragraph) == TaskParser.TASK_IDENTIFIER:
            return paragraph["bullet"].get("listId")
    return None


def _find_placeholder_index(content: list[dict[str, Any]]) -> tuple[int, bool] | None:
    """Find the empty placeholder checkbox immediately after the TASKS identifier.

    Returns (index, existed) where index is the character position at which to
    split and existed indicates whether the placeholder was already present.
    """
    checkbox_list_id = _find_checkbox_list_id(content)
    if checkbox_list_id is None:
        return None

    identifier_end: int | None = None
    after_identifier = False

    for element in content:
        if "paragraph" not in element:
            continue
        paragraph = element["paragraph"]
        bullet = paragraph.get("bullet")
        if bullet is None:
            continue
        if bullet.get("listId") != checkbox_list_id:
            continue

        end_index: int = element.get("endIndex", 0)
        text = _paragraph_text(paragraph)

        if text == TaskParser.TASK_IDENTIFIER:
            after_identifier = True
            identifier_end = end_index
            continue

        if not after_identifier:
            continue

        if not text:
            return end_index - 1, True
        return identifier_end - 1, False

    if identifier_end is None:
        return None
    return identifier_end - 1, False


def _find_task_element_by_id(
    doc: dict[str, Any],
    task_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Locate the document element for the task with the given id= value.

    Returns (element, tab_id) or (None, None) if not found.
    """
    tabs = doc.get("tabs", [])
    sources: list[tuple[list[dict[str, Any]], str | None]] = []
    if tabs:
        for tab_data in tabs:
            tab_title = tab_data.get("tabProperties", {}).get("title", "")
            if tab_title.lower() in EXCLUDED_TAB_TITLES:
                continue
            tid: str | None = tab_data.get("tabProperties", {}).get("tabId")
            content = tab_data.get("documentTab", {}).get("body", {}).get("content", [])
            sources.append((content, tid))
    else:
        sources.append((doc.get("body", {}).get("content", []), None))

    for content, tab_id in sources:
        checkbox_list_id = _find_checkbox_list_id(content)
        if checkbox_list_id is None:
            continue
        for element in content:
            if "paragraph" not in element:
                continue
            paragraph = element["paragraph"]
            if paragraph.get("bullet", {}).get("listId") != checkbox_list_id:
                continue
            text = _paragraph_text(paragraph)
            if not text or " ::: " not in text:
                continue
            if text == TaskParser.TASK_IDENTIFIER:
                continue
            meta_part = text.split(" ::: ", 1)[1]
            if parse_metadata(meta_part).get(KEY_ID) == task_id:
                return element, tab_id

    return None, None


def _separator_insert_index(element: dict[str, Any]) -> int:
    """Return the document character index immediately after ` ::: ` in a task element."""
    text = _paragraph_text(element.get("paragraph", {}))
    sep_pos = text.find(" ::: ")
    if sep_pos == -1:
        return -1
    return element.get("startIndex", 0) + sep_pos + len(" ::: ")


def _find_task_elements_without_id(
    doc: dict[str, Any],
) -> list[tuple[dict[str, Any], str | None]]:
    """Collect document elements for task lines that lack an id= field."""
    result: list[tuple[dict[str, Any], str | None]] = []

    tabs = doc.get("tabs", [])
    sources: list[tuple[list[dict[str, Any]], str | None]] = []
    if tabs:
        for tab_data in tabs:
            tab_title = tab_data.get("tabProperties", {}).get("title", "")
            if tab_title.lower() in EXCLUDED_TAB_TITLES:
                continue
            tab_id: str | None = tab_data.get("tabProperties", {}).get("tabId")
            content = tab_data.get("documentTab", {}).get("body", {}).get("content", [])
            sources.append((content, tab_id))
    else:
        sources.append((doc.get("body", {}).get("content", []), None))

    for content, tab_id in sources:
        checkbox_list_id = _find_checkbox_list_id(content)
        if checkbox_list_id is None:
            continue
        for element in content:
            if "paragraph" not in element:
                continue
            paragraph = element["paragraph"]
            if paragraph.get("bullet", {}).get("listId") != checkbox_list_id:
                continue
            text = _paragraph_text(paragraph)
            if not text or " ::: " not in text:
                continue
            if text == TaskParser.TASK_IDENTIFIER:
                continue
            if text.upper().startswith("MEETING"):
                continue
            meta_part = text.split(" ::: ", 1)[1]
            if parse_metadata(meta_part).get(KEY_ID):
                continue
            result.append((element, tab_id))

    return result
