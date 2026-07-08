"""Metadata serialization and parsing for the key=value task format.

The canonical format is:
    Task title ::: key=value; key=value; ...

Keys and values are stripped of surrounding whitespace. Key lookup is
case-insensitive (keys are normalized to lowercase). Unknown keys are
preserved so future fields round-trip transparently.
"""

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from chronix.core.models import WorkSession


def parse_metadata(metadata_str: str) -> dict[str, str]:
    """Parse a metadata string into a key=value dictionary.

    Pairs that do not contain '=' are silently ignored, preserving
    forward compatibility with older positional-style lines that still
    contain dashes or bare words.

    Returns a dict with lowercase-normalized keys and stripped values.
    """
    result: dict[str, str] = {}
    for part in metadata_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key:
            result[key] = value
    return result


def serialize_metadata(fields: dict[str, str]) -> str:
    """Serialize a key=value dict to the canonical metadata string.

    Keys are emitted in insertion order. The caller is responsible for
    ordering well-known keys before unknown ones.
    """
    return "; ".join(f"{k}={v}" for k, v in fields.items())


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

_DURATION_UNITS: list[tuple[str, int]] = [
    ("hours", 3600),
    ("hour", 3600),
    ("minutes", 60),
    ("minute", 60),
    ("h", 3600),
    ("m", 60),
]


def parse_duration(value: str) -> Optional[timedelta]:
    """Parse a duration string such as '2h', '30m', '3hours', '45minutes'.

    Returns None if the string cannot be parsed.
    """
    v = value.strip().lower()
    for suffix, seconds_per_unit in _DURATION_UNITS:
        if v.endswith(suffix):
            numeric = v[: -len(suffix)].strip()
            if numeric.isdigit() and int(numeric) > 0:
                return timedelta(seconds=int(numeric) * seconds_per_unit)
    return None


def serialize_duration(duration: timedelta) -> str:
    """Serialize a timedelta to the shortest unambiguous duration string."""
    total_minutes = int(duration.total_seconds()) // 60
    hours, remainder = divmod(total_minutes, 60)
    if remainder == 0:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours}{unit}"
    unit = "minute" if total_minutes == 1 else "minutes"
    return f"{total_minutes}{unit}"


# ---------------------------------------------------------------------------
# Display datetime formatting
#
# All datetime-bearing metadata fields are stored internally as
# timezone-aware datetimes, but are displayed in Google Docs as naive,
# second-precision local-time strings (no offset, no microseconds) for
# readability. "Local" here means the caller-supplied `tz` -- in practice
# always the app's configured `scheduling.timezone` -- since Google Docs is
# treated as the source of truth and is assumed to already contain times in
# that same timezone; chronix must round-trip them as-is rather than
# re-interpreting or re-offsetting them. This is the single source of truth
# for that display format; every serialize_*/parse_* helper below routes
# through it. `tz` defaults to UTC only for historical/one-off callers (e.g.
# scripts/migrate_datetime_display_format.py) that predate configurable
# timezones; real application code must always pass the configured tz
# explicitly.
# ---------------------------------------------------------------------------

def _format_display_datetime(dt: datetime, tz: timezone = timezone.utc) -> str:
    """Format a datetime for display: naive local time (per `tz`), second precision."""
    return dt.astimezone(tz).replace(tzinfo=None, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------

def parse_deadline(value: str, tz: timezone = timezone.utc) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string.

    A naive string (no UTC offset) is assumed to already be in `tz` -- the
    configured local timezone -- matching how Google Docs stores it. A string
    with an explicit offset is always respected as-is.

    Returns None for the sentinel '-' or any blank value.
    Raises ValueError for malformed strings.
    """
    v = value.strip()
    if not v or v == "-":
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise ValueError(
            f"Invalid deadline '{value}'. Expected ISO-8601, e.g. "
            f"2026-07-15T09:00:00 or 2026-07-15, or '-' to clear."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def serialize_deadline(dt: Optional[datetime], tz: timezone = timezone.utc) -> str:
    """Serialize a deadline datetime.  Returns '-' for absent values."""
    if dt is None:
        return "-"
    return _format_display_datetime(dt, tz)


# ---------------------------------------------------------------------------
# Created-timestamp helpers
# ---------------------------------------------------------------------------

def parse_created(value: str, tz: timezone = timezone.utc) -> Optional[datetime]:
    """Parse an ISO-8601 creation timestamp.

    A naive string (no UTC offset) is assumed to already be in `tz`.

    Returns None for blank or sentinel '-' values.
    Raises ValueError for malformed strings.
    """
    v = value.strip()
    if not v or v == "-":
        return None
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def serialize_created(dt: datetime, tz: timezone = timezone.utc) -> str:
    """Serialize a creation timestamp as a naive local-time datetime (per `tz`), second precision."""
    return _format_display_datetime(dt, tz)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def parse_sessions(value: str, tz: timezone = timezone.utc) -> list[tuple[datetime, datetime]]:
    """Parse a sessions value like [start/end,start/end,...] into (start, end) tuples.

    A naive start/end (no UTC offset) is assumed to already be in `tz`.

    Returns an empty list for blank, '-', or '[]' values.
    """
    v = value.strip()
    if not v or v == "-" or v == "[]":
        return []
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    sessions: list[tuple[datetime, datetime]] = []
    for interval in v.split(","):
        interval = interval.strip()
        if not interval:
            continue
        parts = interval.split("/", 1)
        if len(parts) != 2:
            continue
        start_str = parts[0].strip().replace("Z", "+00:00")
        end_str = parts[1].strip().replace("Z", "+00:00")
        try:
            start = datetime.fromisoformat(start_str)
            end = datetime.fromisoformat(end_str)
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            sessions.append((start, end))
        except ValueError:
            continue
    return sessions


def serialize_sessions(sessions: "list[WorkSession]", tz: timezone = timezone.utc) -> str:
    """Serialize a list of WorkSessions to the [start/end,...] metadata format."""
    if not sessions:
        return "[]"

    parts = [
        f"{_format_display_datetime(s.start, tz)}/{_format_display_datetime(s.end, tz)}"
        for s in sessions
    ]
    return "[" + ",".join(parts) + "]"


def parse_active_since(value: str, tz: timezone = timezone.utc) -> Optional[datetime]:
    """Parse an ISO-8601 active_since timestamp.

    A naive string (no UTC offset) is assumed to already be in `tz`.
    """
    v = value.strip()
    if not v or v == "-":
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def serialize_active_since(dt: datetime, tz: timezone = timezone.utc) -> str:
    """Serialize an active_since timestamp, second precision local time (per `tz`)."""
    return _format_display_datetime(dt, tz)


# ---------------------------------------------------------------------------
# Well-known key constants
# ---------------------------------------------------------------------------

KEY_ID = "id"
KEY_ESTIMATE = "estimate"
KEY_DURATION = "duration"  # backward-compat alias; parsed but never written
KEY_ACTUAL_DURATION = "actual_duration"
KEY_SESSIONS = "sessions"
KEY_ACTIVE_SINCE = "active_since"
KEY_EXTERNAL_DEADLINE = "external_deadline"
KEY_USER_DEADLINE = "user_deadline"
KEY_DEADLINE_COMPUTED = "deadline_computed"
KEY_MODE = "mode"
KEY_REF = "ref"
KEY_DEPENDS = "deps"
KEY_CREATED = "created"

WELL_KNOWN_KEYS = (
    KEY_ID,
    KEY_ESTIMATE,
    KEY_ACTUAL_DURATION,
    KEY_SESSIONS,
    KEY_ACTIVE_SINCE,
    KEY_EXTERNAL_DEADLINE,
    KEY_USER_DEADLINE,
    KEY_DEADLINE_COMPUTED,
    KEY_MODE,
    KEY_REF,
    KEY_DEPENDS,
    KEY_CREATED,
)
