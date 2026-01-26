"""Domain models for tasks and related entities."""

from datetime import datetime, date, timedelta, timezone
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator
import secrets


class Task(BaseModel):
    """Represents a unit of work, independent of its source or scheduling."""

    id: Optional[str] = None
    title: str
    project: Optional[str] = None
    section: Optional[str] = None
    estimated_duration: timedelta
    deadline_user: Optional[datetime] = None
    deadline_external: Optional[datetime] = None
    completed: bool = False
    source: str

    @model_validator(mode="before")
    @classmethod
    def generate_id_if_empty(cls, values):
        if isinstance(values, dict):
            if not values.get("id"):
                values["id"] = secrets.token_urlsafe(6)
        return values

    @field_validator("estimated_duration")
    @classmethod
    def validate_duration_positive(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("estimated_duration must be positive")
        return v

    @field_validator("deadline_user", "deadline_external")
    @classmethod
    def validate_deadline_timezone_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return v

    @model_validator(mode="after")
    def validate_id_or_title_nonempty(self):
        if not self.id and not self.title:
            raise ValueError("at least one of id or title must be non-empty")
        return self

    @property
    def effective_deadline(self) -> Optional[datetime]:
        """Returns deadline_external if set, otherwise deadline_user."""
        return self.deadline_external if self.deadline_external is not None else self.deadline_user


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
        if not self.is_segment and actual_duration != self.task.estimated_duration:
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
