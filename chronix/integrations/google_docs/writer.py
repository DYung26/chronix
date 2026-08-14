"""Google Docs implementation of the TaskWriter interface."""

import re
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

        placeholder_index, placeholder_existed, tab_id, task_indent = self._find_insertion_point(doc, task.tab)

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

        if task.description:
            description_text, bullets = _format_description_text(task.description)
            # The task line was inserted as "\n" + task_line with no trailing
            # newline of its own -- task_insert_index + 1 + len(task_line) is
            # the pre-existing character right after it (the placeholder's
            # old newline, or whatever followed). Inserting the description's
            # leading "\n" there makes that first new newline the task
            # paragraph's terminator, and the description's own paragraph(s)
            # start one character later, at description_start + 1 -- not at
            # description_start itself. Styling/list operations must target
            # that later range, or they land partly on the task's own
            # terminating newline and can bleed into the task paragraph.
            description_insert_at = task_insert_index + 1 + len(task_line)
            description_location: dict[str, Any] = {"index": description_insert_at}
            if tab_id is not None:
                description_location["tabId"] = tab_id
            requests.append({"insertText": {"location": description_location, "text": description_text}})

            description_range: dict[str, Any] = {
                "startIndex": description_insert_at + 1,
                "endIndex": description_insert_at + len(description_text),
            }
            if tab_id is not None:
                description_range["tabId"] = tab_id

            # insertText inherits whatever formatting sits immediately to the
            # left of the insertion point. Rather than track down and clear
            # each inherited attribute individually (bold, etc.), reset the
            # whole character range to default in one call -- a freshly
            # inserted description should never inherit any styling.
            requests.append({
                "updateTextStyle": {
                    "range": description_range,
                    "textStyle": {},
                    "fields": "*",
                }
            })

            # A paragraph created by splitting a bulleted line also inherits
            # checkbox-list membership, not just character formatting -- so
            # the description starts out as its own checkbox item(s). Remove
            # that list membership; a description is plain indented text, not
            # a checklist entry. Lines using markdown bullet syntax ("- "/"* ")
            # get real bullets applied back afterward, below.
            requests.append({
                "deleteParagraphBullets": {
                    "range": description_range,
                }
            })

            # Markdown-style bullet lines ("- foo", "* foo", "- [ ] foo") in
            # the description become real Docs bullets. Applied *before* the
            # indent step below, not after: createParagraphBullets
            # recalculates each bulleted paragraph's indent as part of
            # establishing its nesting level, which would otherwise silently
            # override an indent set earlier. Running indent last guarantees
            # it's always the final word. Checkbox bullets ([x]) also get
            # strikethrough applied, matching the app's done/undone convention.
            for bullet in bullets:
                rel_start, rel_end = bullet.range
                bullet_range: dict[str, Any] = {
                    "startIndex": description_insert_at + rel_start,
                    "endIndex": description_insert_at + rel_end,
                }
                if tab_id is not None:
                    bullet_range["tabId"] = tab_id
                requests.append({
                    "createParagraphBullets": {
                        "range": bullet_range,
                        "bulletPreset": bullet.preset,
                    }
                })
                if bullet.checked:
                    requests.append({
                        "updateTextStyle": {
                            "range": bullet_range,
                            "textStyle": {"strikethrough": True},
                            "fields": "strikethrough",
                        }
                    })

            # Indent one level deeper than the task's own paragraph, not an
            # absolute indent -- a task that's itself indented (nested under a
            # section, etc.) should still get its description one Tab further
            # in, relative to wherever the task actually sits. indentFirstLine
            # must be set explicitly alongside indentStart (to the same value,
            # so the first line aligns with the rest of the paragraph rather
            # than hanging): paragraphs split off a checkbox-list item inherit
            # that item's own indentFirstLine, measured from the page margin
            # independently of indentStart, which can otherwise mask/offset it.
            # This runs last (after deleteParagraphBullets and
            # createParagraphBullets above) since both of those requests
            # recalculate indent themselves as a side effect of changing list
            # membership -- applying our value afterward guarantees it wins.
            requests.append({
                "updateParagraphStyle": {
                    "range": description_range,
                    "paragraphStyle": {
                        "indentStart": {
                            "magnitude": (task_indent + 1) * _INDENT_POINTS_PER_LEVEL,
                            "unit": "PT",
                        },
                        "indentFirstLine": {
                            "magnitude": (task_indent + 1) * _INDENT_POINTS_PER_LEVEL,
                            "unit": "PT",
                        },
                    },
                    "fields": "indentStart,indentFirstLine",
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

        element, tab_id, content, element_index = _find_task_element_by_id(doc, task_id)
        if element is None:
            raise TaskNotFoundError(task_id)

        requests = _build_update_requests(element, tab_id, update, self._tz, content, element_index)
        if requests:
            self._docs_service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": requests},
            ).execute()

    def delete_task(self, document_id: str, task_id: str) -> None:
        """Remove a task paragraph from the document by its persistent ID.

        Also removes the task's description block (the indented paragraphs
        immediately following it), if any, since a description has no
        independent identity once its owning task is gone.
        """
        doc = self._docs_service.documents().get(
            documentId=document_id,
            includeTabsContent=True,
        ).execute()

        element, tab_id, content, element_index = _find_task_element_by_id(doc, task_id)
        if element is None:
            raise TaskNotFoundError(task_id)

        start_index: int = element.get("startIndex", 0)
        end_index: int = element.get("endIndex", 0)

        description_range = _find_description_range(content, element_index)
        if description_range is not None:
            end_index = description_range[1]

        range_: dict[str, Any] = {"startIndex": start_index, "endIndex": end_index}
        if tab_id is not None:
            range_["tabId"] = tab_id

        self._docs_service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"deleteContentRange": {"range": range_}}]},
        ).execute()

    def _find_insertion_point(
        self, doc: dict[str, Any], tab_token: str | None = None
    ) -> tuple[int, bool, str | None, int]:
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
                index, existed, indent = result
                return index, existed, None, indent

        raise ValueError("No TASKS section found in document")

    def _find_insertion_in_tab(
        self, tab_data: dict[str, Any], allow_excluded: bool = False
    ) -> tuple[int, bool, str, int] | None:
        tab_id = tab_data.get("tabProperties", {}).get("tabId", "")
        title = tab_data.get("tabProperties", {}).get("title", "")
        if not allow_excluded and title.lower() in EXCLUDED_TAB_TITLES:
            return None
        content = tab_data.get("documentTab", {}).get("body", {}).get("content", [])
        result = _find_placeholder_index(content)
        if result is None:
            return None
        index, existed, indent = result
        return index, existed, tab_id, indent

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
    content: list[dict[str, Any]] | None = None,
    element_index: int = -1,
) -> list[dict[str, Any]]:
    """Build batchUpdate requests to apply ``update`` to the given element.

    ``content``/``element_index`` locate the task within the raw content list
    so its description block (see _find_description_range) can also be
    updated when ``update.has_description_change()``. Both are optional so
    callers that never touch descriptions (or lack this context) keep working.

    Requests touching the description range are emitted before requests
    touching the task line itself, since the description sits later in the
    document: batchUpdate applies requests in order against the
    progressively-mutated document, so editing the earlier (task line) range
    first would shift the description range's indices out from under it.
    """
    requests: list[dict[str, Any]] = []

    start_index: int = element.get("startIndex", 0)
    end_index: int = element.get("endIndex", 0)
    text_end = end_index - 1

    if update.has_description_change() and content is not None and element_index >= 0:
        requests.extend(
            _build_description_update_requests(content, element_index, tab_id, update.description)
        )

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


def _build_description_update_requests(
    content: list[dict[str, Any]],
    task_element_index: int,
    tab_id: str | None,
    new_description: str | None,
) -> list[dict[str, Any]]:
    """Build requests to replace a task's description block with ``new_description``.

    Deletes the existing description range (if one is found) and, if
    ``new_description`` is non-None, inserts it as indented paragraphs
    immediately after the task line. ``new_description`` of None only
    deletes -- it does not insert a replacement.
    """
    requests: list[dict[str, Any]] = []
    task_element = content[task_element_index]
    insert_at = task_element.get("endIndex", 0) - 1

    existing_range = _find_description_range(content, task_element_index)
    if existing_range is not None:
        range_: dict[str, Any] = {"startIndex": existing_range[0], "endIndex": existing_range[1]}
        if tab_id is not None:
            range_["tabId"] = tab_id
        requests.append({"deleteContentRange": {"range": range_}})

    if new_description:
        description_text, bullets = _format_description_text(new_description)
        # Same boundary reasoning as create_task: the leading "\n" of
        # description_text becomes the task paragraph's own terminating
        # newline when inserted right after the task's text, so the
        # description's actual paragraph content starts one character later.
        location: dict[str, Any] = {"index": insert_at}
        if tab_id is not None:
            location["tabId"] = tab_id
        requests.append({
            "insertText": {
                "location": location,
                "text": description_text,
            }
        })

        description_range: dict[str, Any] = {
            "startIndex": insert_at + 1,
            "endIndex": insert_at + len(description_text),
        }
        if tab_id is not None:
            description_range["tabId"] = tab_id

        # insertText inherits whatever formatting sits immediately to the
        # left of the insertion point. Rather than track down and clear each
        # inherited attribute individually, reset the whole character range
        # to default in one call -- a freshly inserted description should
        # never inherit any styling from the task line or a prior description.
        requests.append({
            "updateTextStyle": {
                "range": description_range,
                "textStyle": {},
                "fields": "*",
            }
        })

        # A paragraph created by splitting a bulleted line also inherits
        # checkbox-list membership, not just character formatting -- remove
        # it, since a description is plain indented text, not a checklist
        # entry. Lines using markdown bullet syntax ("- "/"* ") get real
        # bullets applied back afterward, below.
        requests.append({
            "deleteParagraphBullets": {
                "range": description_range,
            }
        })

        # Markdown-style bullet lines ("- foo", "* foo", "- [ ] foo") in the
        # description become real Docs bullets. Applied *before* the indent
        # step below, not after: createParagraphBullets recalculates each
        # bulleted paragraph's indent as part of establishing its nesting
        # level, which would otherwise silently override an indent set
        # earlier. Running indent last guarantees it's always the final word.
        # Checkbox bullets ([x]) also get strikethrough applied, matching the
        # app's done/undone convention.
        for bullet in bullets:
            rel_start, rel_end = bullet.range
            bullet_range: dict[str, Any] = {
                "startIndex": insert_at + rel_start,
                "endIndex": insert_at + rel_end,
            }
            if tab_id is not None:
                bullet_range["tabId"] = tab_id
            requests.append({
                "createParagraphBullets": {
                    "range": bullet_range,
                    "bulletPreset": bullet.preset,
                }
            })
            if bullet.checked:
                requests.append({
                    "updateTextStyle": {
                        "range": bullet_range,
                        "textStyle": {"strikethrough": True},
                        "fields": "strikethrough",
                    }
                })

        # One level deeper than the task's own current indent, not an
        # absolute indent -- matches create_task's rule so a task edited
        # after being manually re-indented in the document still gets its
        # description placed correctly relative to it. indentFirstLine must
        # be set explicitly alongside indentStart (to the same value, so the
        # first line aligns with the rest of the paragraph rather than
        # hanging): paragraphs split off a checkbox-list item inherit that
        # item's own indentFirstLine, which is measured from the page margin
        # independently of indentStart and can otherwise mask/offset it. This
        # runs last (after deleteParagraphBullets and createParagraphBullets
        # above) since both recalculate indent themselves as a side effect of
        # changing list membership -- applying our value afterward guarantees
        # it wins.
        task_indent = _paragraph_indent_level(task_element.get("paragraph", {}))
        requests.append({
            "updateParagraphStyle": {
                "range": description_range,
                "paragraphStyle": {
                    "indentStart": {
                        "magnitude": (task_indent + 1) * _INDENT_POINTS_PER_LEVEL,
                        "unit": "PT",
                    },
                    "indentFirstLine": {
                        "magnitude": (task_indent + 1) * _INDENT_POINTS_PER_LEVEL,
                        "unit": "PT",
                    },
                },
                "fields": "indentStart,indentFirstLine",
            }
        })

    return requests


_CHECKBOX_PATTERN = re.compile(r"^-\s*\[\s*([xX]?)\s*\]\s*")

_BULLET_PRESET_PLAIN = "BULLET_DISC_CIRCLE_SQUARE"
_BULLET_PRESET_CHECKBOX = "BULLET_CHECKBOX"


class _DescriptionBullet:
    """A description line that should become a real Docs bullet.

    ``range`` is the (start, end) character offset within the text returned
    by _format_description_text. ``checked`` only applies to checkbox-preset
    bullets and drives whether strikethrough is also applied, matching the
    app's existing done/undone convention.
    """

    def __init__(self, range_: tuple[int, int], preset: str, checked: bool = False):
        self.range = range_
        self.preset = preset
        self.checked = checked


def _format_description_text(
    description: str,
) -> tuple[str, list[_DescriptionBullet]]:
    """Serialize a description string to insertable document text.

    Each line becomes its own paragraph, inserted right after the task line.

    A line whose first non-whitespace characters are "- " or "* " is treated
    as a markdown-style bullet: the marker is stripped and the line becomes a
    real Google Docs bullet point. "- []", "- [ ]", "- [x]", "- [X]" (with or
    without the inner/trailing spaces) is a checkbox-style bullet instead of a
    plain one, with "x"/"X" additionally struck through -- matching the app's
    existing done/undone convention of representing completion as
    strikethrough, since a checkbox glyph can't be read back from the Docs
    API to distinguish it from a plain bullet glyph (glyph type comes back
    empty/unspecified for checkboxes). Escaping the marker with a leading
    backslash ("\\- " or "\\* ") keeps it as literal text instead -- the
    backslash is stripped but the dash/asterisk is kept, matching how the
    person would expect a Docs paragraph starting with a literal "-" to render.

    Returns (text, bullets) where bullets describes every line that should
    become a real Docs bullet. Callers translate each bullet's range into
    document character offsets by adding their own insertion base index, then
    issue a createParagraphBullets request (with the bullet's preset) and,
    for checked bullets, an updateTextStyle strikethrough request.
    """
    lines = description.split("\n")
    text_parts: list[str] = []
    bullets: list[_DescriptionBullet] = []
    offset = 0

    for line in lines:
        stripped = line.lstrip()
        leading_ws = line[: len(line) - len(stripped)]
        checkbox_match = _CHECKBOX_PATTERN.match(stripped)
        is_checkbox = checkbox_match is not None
        is_checked_box = is_checkbox and checkbox_match.group(1) != ""
        is_bullet = not is_checkbox and stripped[:2] in ("- ", "* ")
        is_escaped = stripped[:3] in ("\\- ", "\\* ")

        if is_checkbox:
            content = stripped[checkbox_match.end():]
        elif is_bullet:
            content = stripped[2:]
        elif is_escaped:
            content = stripped[1:]
        else:
            content = line

        piece = "\n" + (
            leading_ws + content if (is_checkbox or is_bullet or is_escaped) else content
        )
        line_start = offset + 1  # +1 to skip the leading "\n" itself
        line_end = offset + len(piece)

        if is_checkbox:
            preset = _BULLET_PRESET_CHECKBOX
            bullets.append(_DescriptionBullet((line_start, line_end), preset, is_checked_box))
        elif is_bullet:
            bullets.append(_DescriptionBullet((line_start, line_end), _BULLET_PRESET_PLAIN))

        text_parts.append(piece)
        offset = line_end

    return "".join(text_parts), bullets


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


_INDENT_POINTS_PER_LEVEL = 36


def _paragraph_indent_level(paragraph: dict[str, Any]) -> int:
    """Same indent-level derivation as GoogleDocsParser, for raw API content.

    Kept independent of chronix.integrations.google_docs.parser so the writer
    only depends on raw content elements (with real character offsets), never
    on the already-parsed ParsedParagraph/indent_level used for reading.
    """
    bullet = paragraph.get("bullet")
    if bullet is not None:
        return bullet.get("nestingLevel", 0)
    indent_start = paragraph.get("paragraphStyle", {}).get("indentStart")
    if not indent_start:
        return 0
    return int(indent_start.get("magnitude", 0) // _INDENT_POINTS_PER_LEVEL)


def _find_description_range(
    content: list[dict[str, Any]], task_element_index: int
) -> tuple[int, int] | None:
    """Find the (startIndex, endIndex) span of the description block after a task.

    Mirrors chronix.core.todo._extract_description's indent-level rule, but
    operates on raw content elements with real character offsets so the
    writer can build a deleteContentRange/insertText pair. Always re-derived
    from the document's current indentation on every call -- never cached --
    since the user may have manually edited the document since it was last read.
    Returns None if there is no following paragraph more indented than the task.
    """
    task_element = content[task_element_index]
    task_indent = _paragraph_indent_level(task_element.get("paragraph", {}))

    start_index: int | None = None
    end_index: int | None = None
    for element in content[task_element_index + 1:]:
        if "paragraph" not in element:
            break
        paragraph = element["paragraph"]
        if _paragraph_indent_level(paragraph) <= task_indent:
            break
        if start_index is None:
            start_index = element.get("startIndex", 0)
        end_index = element.get("endIndex", 0)

    if start_index is None or end_index is None:
        return None
    return start_index, end_index


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


def _find_placeholder_index(content: list[dict[str, Any]]) -> tuple[int, bool, int] | None:
    """Find the empty placeholder checkbox immediately after the TASKS identifier.

    Returns (index, existed, indent) where index is the character position at
    which to split, existed indicates whether the placeholder was already
    present, and indent is the placeholder paragraph's indent level -- the
    level a newly-split task line inherits, and so the level its description
    (see create_task) must indent one further than.
    """
    checkbox_list_id = _find_checkbox_list_id(content)
    if checkbox_list_id is None:
        return None

    identifier_end: int | None = None
    identifier_indent = 0
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
            identifier_indent = _paragraph_indent_level(paragraph)
            continue

        if not after_identifier:
            continue

        if not text:
            return end_index - 1, True, _paragraph_indent_level(paragraph)
        return identifier_end - 1, False, identifier_indent

    if identifier_end is None:
        return None
    return identifier_end - 1, False, identifier_indent


def _find_task_element_by_id(
    doc: dict[str, Any],
    task_id: str,
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]], int]:
    """Locate the document element for the task with the given id= value.

    Returns (element, tab_id, content, element_index). `content` is the raw
    content list the element was found in and `element_index` is its position
    within it -- both needed by callers that must also locate the task's
    description block (see _find_description_range), which is always
    re-derived from this same content list rather than cached.
    Returns (None, None, [], -1) if not found.
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
        for index, element in enumerate(content):
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
                return element, tab_id, content, index

    return None, None, [], -1


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
