"""Domain models for tasks and related entities."""

from datetime import datetime, date, timedelta, timezone
from typing import Optional, Literal
from pydantic import BaseModel, field_validator, model_validator
import secrets


ExecutionMode = Literal["atomic", "flex", "contiguous_preferred"]


def generate_task_id() -> str:
    """Generate a random, URL-safe persistent task identifier."""
    return secrets.token_urlsafe(6)


class WorkSession(BaseModel):
    """A completed interval of work on a task."""

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def validate_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        return v

    @model_validator(mode="after")
    def validate_start_before_end(self):
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class Task(BaseModel):
    """Represents a unit of work, independent of its source or scheduling."""

    id: Optional[str] = None
    title: str
    project: Optional[str] = None
    section: Optional[str] = None
    document_title: Optional[str] = None
    estimated_duration: timedelta
    deadline_user: Optional[datetime] = None
    deadline_external: Optional[datetime] = None
    deadline_computed: Optional[datetime] = None
    completed: bool = False
    source: str
    ref: Optional[str] = None
    depends_on: list[str] = []
    execution_mode: ExecutionMode = "atomic"
    # Priority rank inherited from the task's source document's config entry
    # (DocumentConfig.priority). Lower = higher priority; None = unranked.
    # Used only as a soft bias in scheduling urgency -- never overrides
    # deadline safety. Stamped on by TaskAggregator, not set by parsers.
    priority: Optional[int] = None
    created: Optional[datetime] = None
    sessions: list[WorkSession] = []
    actual_duration: Optional[timedelta] = None
    active_since: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def set_default_execution_mode(cls, values):
        if isinstance(values, dict) and "execution_mode" not in values:
            duration = values.get("estimated_duration")
            if duration is not None:
                if isinstance(duration, timedelta):
                    total_minutes = duration.total_seconds() / 60
                elif isinstance(duration, (int, float)):
                    total_minutes = duration / 60 if duration > 60 else duration
                else:
                    total_minutes = 0

                if total_minutes <= 90:
                    values["execution_mode"] = "atomic"
                else:
                    values["execution_mode"] = "flex"
        return values

    @field_validator("estimated_duration")
    @classmethod
    def validate_duration_positive(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("estimated_duration must be positive")
        return v

    @field_validator("deadline_user", "deadline_external", "deadline_computed")
    @classmethod
    def validate_deadline_timezone_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return v

    @field_validator("created", "active_since")
    @classmethod
    def validate_datetime_timezone_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return v

    @model_validator(mode="after")
    def validate_id_or_title_nonempty(self):
        if not self.id and not self.title:
            raise ValueError("at least one of id or title must be non-empty")
        return self

    @property
    def effective_deadline(self) -> Optional[datetime]:
        """Returns deadline_external if set, else deadline_user, else deadline_computed."""
        if self.deadline_external is not None:
            return self.deadline_external
        if self.deadline_user is not None:
            return self.deadline_user
        return self.deadline_computed

    @property
    def is_active(self) -> bool:
        """True if the task has an open work session (either implicit or explicit)."""
        return self.active_since is not None

    @property
    def is_paused(self) -> bool:
        """True if the task has completed sessions but no current active session."""
        return bool(self.sessions) and self.active_since is None

    def compute_actual_duration(self) -> timedelta:
        """Sum of all completed work session durations."""
        return sum((s.duration for s in self.sessions), timedelta())


class TimeBlock(BaseModel):
    """Represents a reserved interval of time."""

    start: datetime
    end: datetime
    kind: str
    label: Optional[str] = None

    @field_validator("start", "end")
    @classmethod
    def validate_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        return v

    @model_validator(mode="after")
    def validate_start_before_end(self):
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class AdHocMeeting(BaseModel):
     """Represents an ad-hoc, non-recurring meeting from Google Docs."""

     start: datetime
     end: datetime
     label: Optional[str] = None
     source: str = "google_docs"

     @field_validator("start", "end")
     @classmethod
     def validate_timezone_aware(cls, v: datetime) -> datetime:
         if v.tzinfo is None:
             raise ValueError("start and end must be timezone-aware")
         return v

     @model_validator(mode="after")
     def validate_start_before_end(self):
         if self.start >= self.end:
             raise ValueError("start must be before end")
         return self

     def to_time_block(self) -> TimeBlock:
         """Convert to a TimeBlock for scheduler integration."""
         return TimeBlock(
             start=self.start,
             end=self.end,
             kind="meeting",
             label=self.label
         )


class ScheduledTask(BaseModel):
    """Represents a Task placed into time."""

    task: Task
    start: datetime
    end: datetime
    violates_deadline_user: bool
    violates_deadline_external: bool
    is_segment: bool = False
    segment_index: Optional[int] = None
    total_segments: Optional[int] = None
    # True when the task's full estimated_duration wasn't placed within the
    # scheduling window (e.g. it ran out of room for today and will need
    # further chunks on a later day). Distinct from is_segment, which marks
    # a task split into multiple displayed parts within the same window.
    is_partial: bool = False

    @field_validator("start", "end")
    @classmethod
    def validate_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        return v

    @model_validator(mode="after")
    def validate_start_before_end(self):
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self

    @model_validator(mode="after")
    def validate_duration_matches(self):
        actual_duration = self.end - self.start
        if (
            not self.is_segment
            and not self.is_partial
            and actual_duration != self.task.estimated_duration
        ):
            raise ValueError("duration must equal task.estimated_duration")
        return self

    @model_validator(mode="after")
    def validate_segment_fields(self):
        if self.is_segment:
            if self.segment_index is None or self.total_segments is None:
                raise ValueError("segment_index and total_segments required when is_segment=True")
            if self.segment_index < 1 or self.segment_index > self.total_segments:
                raise ValueError("segment_index must be between 1 and total_segments")
        return self


class DaySchedule(BaseModel):
    """Represents the result of scheduling tasks for a single day."""

    date: date
    scheduled_tasks: list[ScheduledTask] = []
    blocked_time: list[TimeBlock] = []
    conflicts: list[str] = []
