"""Data models for Google Calendar integration."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class CalendarEventMetadata(BaseModel):
    """Metadata for identifying and tracking Chronix-managed calendar events."""
    
    chronix_managed: bool
    chronix_task_id: str
    source_type: Optional[str] = None
    document_title: Optional[str] = None
    tab_name: Optional[str] = None


class ConflictInfo(BaseModel):
    """Information about a calendar event conflict."""
    
    calendar_event_id: str
    calendar_event_title: str
    calendar_event_start: datetime
    calendar_event_end: datetime
    chronix_task_id: str
    chronix_task_title: str
    chronix_scheduled_start: datetime
    chronix_scheduled_end: datetime
    reason: str


class CalendarSyncResult(BaseModel):
    """Result of a calendar sync operation."""
    
    success: bool
    created_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    shortened_count: int = 0
    conflicts: list[ConflictInfo] = []
    error_message: Optional[str] = None
