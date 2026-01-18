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
        """Returns deadline_user if set, otherwise deadline_external."""
        return self.deadline_user if self.deadline_user is not None else self.deadline_external


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


class ScheduledTask(BaseModel):
    """Represents a Task placed into time."""

    task: Task
    start: datetime
    end: datetime
    violates_deadline_user: bool
    violates_deadline_external: bool

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
        if actual_duration != self.task.estimated_duration:
            raise ValueError("duration must equal task.estimated_duration")
        return self


class DaySchedule(BaseModel):
    """Represents the result of scheduling tasks for a single day."""

    date: date
    scheduled_tasks: list[ScheduledTask] = []
    blocked_time: list[TimeBlock] = []
    conflicts: list[str] = []
