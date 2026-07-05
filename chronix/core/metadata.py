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
# timezone-aware UTC datetimes, but are displayed in Google Docs as naive,
# second-precision UTC strings (no offset, no microseconds) for readability.
# This is the single source of truth for that display format; every
# serialize_* helper below routes through it.
# ---------------------------------------------------------------------------

def _format_display_datetime(dt: datetime) -> str:
    """Format a datetime for display: naive UTC, second precision."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------

def parse_deadline(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string.

    Returns None for the sentinel '-' or any blank value.
    Raises ValueError for malformed strings.
    """
    v = value.strip()
    if not v or v == "-":
        return None
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_deadline(dt: Optional[datetime]) -> str:
    """Serialize a deadline datetime.  Returns '-' for absent values."""
    if dt is None:
        return "-"
    return _format_display_datetime(dt)


# ---------------------------------------------------------------------------
# Created-timestamp helpers
# ---------------------------------------------------------------------------

def parse_created(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC creation timestamp.

    Returns None for blank or sentinel '-' values.
    Raises ValueError for malformed strings.
    """
    v = value.strip()
    if not v or v == "-":
        return None
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_created(dt: datetime) -> str:
    """Serialize a creation timestamp as a naive UTC datetime, second precision."""
    return _format_display_datetime(dt)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def parse_sessions(value: str) -> list[tuple[datetime, datetime]]:
    """Parse a sessions value like [start/end,start/end,...] into (start, end) tuples.

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
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            sessions.append((start, end))
        except ValueError:
            continue
    return sessions


def serialize_sessions(sessions: "list[WorkSession]") -> str:
    """Serialize a list of WorkSessions to the [start/end,...] metadata format."""
    if not sessions:
        return "[]"

    parts = [
        f"{_format_display_datetime(s.start)}/{_format_display_datetime(s.end)}"
        for s in sessions
    ]
    return "[" + ",".join(parts) + "]"


def parse_active_since(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 active_since timestamp."""
    v = value.strip()
    if not v or v == "-":
        return None
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_active_since(dt: datetime) -> str:
    """Serialize an active_since timestamp, second precision UTC."""
    return _format_display_datetime(dt)


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
