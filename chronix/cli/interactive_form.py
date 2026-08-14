"""Full-screen interactive form for task add/update, and simple line prompts
for single-value commands (delete/done/undone/pause/resume/task-id lookups).

The form is one shared component used by both `add` (fields start empty) and
`update` (fields start prefilled with the task's current values), navigated
with Tab/Shift+Tab and Up/Down between fields, arrow keys within a field's
text, and a dedicated key to submit. Cancelling (Escape or Ctrl+C) aborts
without invoking the caller's submit callback, so no fields are ever applied
on a cancelled form.
"""

from dataclasses import dataclass
from datetime import timezone
from typing import Callable, Optional

from prompt_toolkit import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.containers import ConditionalContainer, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from chronix.core.metadata import parse_deadline, parse_duration

_VALID_MODES = ("atomic", "flex", "contiguous_preferred")
_VALID_TRACKS = ("auto", "primary", "secondary")

_FORM_STYLE = Style.from_dict({
    "frame.label": "fg:cyan bold",
    "field-label": "fg:cyan",
    "field-label.focused": "fg:cyan bold",
    "field-error": "fg:red",
    "help-text": "fg:#888888",
    "text-area": "",
    "text-area.focused": "bg:#1c1c1c",
})


@dataclass
class FormField:
    key: str
    label: str
    initial: str = ""
    multiline: bool = False
    help_text: str = ""
    validate: Optional[Callable[[str], Optional[str]]] = None


def _validate_duration(text):
    if not text.strip():
        return "Duration is required (e.g. 2h, 30m)."
    if parse_duration(text) is None:
        return "Invalid duration. Use forms like 2h, 30m, 2hours, 30minutes."
    return None


def _validate_optional_deadline(text):
    if not text.strip():
        return None
    try:
        parse_deadline(text, timezone.utc)
    except ValueError:
        return "Invalid date. Use ISO-8601, e.g. 2026-07-15T09:00:00."
    return None


def _validate_mode(text):
    if not text.strip():
        return None
    if text.strip() not in _VALID_MODES:
        return "Must be one of: " + ", ".join(_VALID_MODES) + "."
    return None


def _validate_track(text):
    if not text.strip():
        return None
    if text.strip() not in _VALID_TRACKS:
        return "Must be one of: " + ", ".join(_VALID_TRACKS) + "."
    return None


def build_task_form_fields(
    title="",
    duration_str="",
    description="",
    external_deadline_str="",
    user_deadline_str="",
    mode="",
    track="",
    ref="",
    deps="",
    tab="",
    include_tab_field=True,
):
    fields = []
    fields.append(FormField("title", "Title", title, validate=lambda t: "Title is required." if not t.strip() else None))
    fields.append(FormField("duration", "Duration (e.g. 2h, 30m)", duration_str, validate=_validate_duration))
    fields.append(FormField("description", "Description", description, multiline=True, help_text="Multi-line supported. Leave empty for none."))
    fields.append(FormField("external_deadline", "External deadline (ISO-8601)", external_deadline_str, validate=_validate_optional_deadline, help_text="e.g. 2026-07-15T09:00:00. Leave empty for none."))
    fields.append(FormField("user_deadline", "User deadline (ISO-8601)", user_deadline_str, validate=_validate_optional_deadline, help_text="e.g. 2026-07-15T09:00:00. Leave empty for none."))
    fields.append(FormField("mode", "Mode (atomic/flex/contiguous_preferred)", mode, validate=_validate_mode, help_text="Leave empty for default."))
    fields.append(FormField("track", "Track (auto/primary/secondary)", track, validate=_validate_track, help_text="Leave empty for auto."))
    fields.append(FormField("ref", "Ref", ref, help_text="Short identifier other tasks can depend on."))
    fields.append(FormField("deps", "Deps (comma-separated refs)", deps))
    if include_tab_field:
        fields.append(FormField("tab", "Tab (title or ID)", tab, help_text="Leave empty for the default tab."))
    return fields


class TaskForm:
    def __init__(self, title, fields):
        self.title = title
        self.fields = fields
        self._errors = {}

        self._text_areas = {}
        for f in fields:
            self._text_areas[f.key] = TextArea(
                text=f.initial,
                multiline=f.multiline,
                height=4 if f.multiline else 1,
                style="class:text-area",
                focus_on_click=True,
            )

        self._error_control = FormattedTextControl(text=self._render_errors)
        self._app = self._build_app()

    def _render_errors(self):
        if not self._errors:
            return [("", "")]
        lines = []
        for f in self.fields:
            if f.key in self._errors:
                lines.append(("class:field-error", "  " + f.label + ": " + self._errors[f.key] + "\n"))
        return lines

    def _label_style(self, f):
        if self._app.layout.has_focus(self._text_areas[f.key]):
            return "class:field-label.focused"
        return "class:field-label"

    def _build_field_row(self, f):
        content = HSplit([
            Window(
                FormattedTextControl(lambda f=f: [(self._label_style(f), f.label + ":")]),
                dont_extend_height=True,
            ),
            self._text_areas[f.key],
        ])
        if f.help_text:
            help_window = Window(
                FormattedTextControl([("class:help-text", "  " + f.help_text)]),
                dont_extend_height=True,
            )
            return HSplit([content, help_window])
        return content

    def _build_app(self):
        field_rows = [self._build_field_row(f) for f in self.fields]

        error_window = ConditionalContainer(
            Window(self._error_control, dont_extend_height=True),
            filter=Condition(lambda: bool(self._errors)),
        )

        help_line = Window(
            FormattedTextControl([("class:help-text", "Tab/Down next field  |  Shift+Tab/Up previous  |  Ctrl+S submit  |  Esc cancel")]),
            dont_extend_height=True,
        )

        body = HSplit(field_rows + [error_window, help_line])
        root = Frame(body, title=self.title, style="class:frame.label")

        kb = KeyBindings()

        @kb.add("c-s")
        def _submit(event):
            if self._validate_all():
                event.app.exit(result="submit")

        @kb.add("escape")
        @kb.add("c-c")
        def _cancel(event):
            event.app.exit(result="cancel")

        @kb.add("tab")
        def _next(event):
            event.app.layout.focus_next()

        @kb.add("s-tab")
        def _prev(event):
            event.app.layout.focus_previous()

        @kb.add("down")
        def _down(event):
            buf = event.app.current_buffer
            doc = buf.document
            if doc.cursor_position_row >= doc.line_count - 1:
                event.app.layout.focus_next()
            else:
                buf.cursor_down()

        @kb.add("up")
        def _up(event):
            buf = event.app.current_buffer
            doc = buf.document
            if doc.cursor_position_row <= 0:
                event.app.layout.focus_previous()
            else:
                buf.cursor_up()

        app = Application(
            layout=Layout(root, focused_element=self._text_areas[self.fields[0].key]),
            key_bindings=kb,
            style=_FORM_STYLE,
            full_screen=True,
            mouse_support=True,
        )
        return app

    def _validate_all(self):
        self._errors = {}
        for f in self.fields:
            if f.validate is None:
                continue
            text = self._text_areas[f.key].text
            error = f.validate(text)
            if error:
                self._errors[f.key] = error
        return not self._errors

    def run(self):
        outcome = self._app.run()
        if outcome != "submit":
            return None
        return {f.key: self._text_areas[f.key].text for f in self.fields}
