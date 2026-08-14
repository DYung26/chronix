"""Simple line-prompt helpers for single-value interactive input.

Used by the lighter task commands (delete/done/undone/pause/resume, and any
command that needs a task_id filled in before it can proceed) that don't
warrant the full-screen form in interactive_form.py -- a single required
value doesn't benefit from a multi-field layout.
"""

from typing import Optional

from chronix.cli.formatting import console


def prompt_task_id(action: str) -> Optional[str]:
    """Prompt for a task_id. Returns None if the user submits blank (cancel).

    `action` describes what the id is for, e.g. "delete" -> "Task ID to delete: ".
    """
    try:
        value = console.input(f"[cyan]Task ID to {action}:[/cyan] ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return None
    return value or None


def prompt_value(label: str, current: Optional[str] = None) -> Optional[str]:
    """Prompt for a single field's value, showing the current value as a hint.

    Pressing Enter with no input keeps `current` (if any) -- returns `current`
    unchanged. Typing a value returns that value. Returns None only if both
    input is blank and there is no current value to fall back to (i.e. the
    field is still unset), leaving it to the caller to decide whether that's
    acceptable.
    """
    hint = f" [{current}]" if current else ""
    try:
        value = console.input(f"[cyan]{label}{hint}:[/cyan] ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return current
    return value if value else current


def confirm(prompt_text: str, default_no: bool = True) -> bool:
    """Ask a yes/no question. Defaults to No on blank input unless default_no=False."""
    suffix = "[y/N]" if default_no else "[Y/n]"
    try:
        value = console.input(f"[yellow]{prompt_text} {suffix}:[/yellow] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    if not value:
        return not default_no
    return value in ("y", "yes")
