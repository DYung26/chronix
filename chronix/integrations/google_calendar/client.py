"""Google Calendar API client."""

from typing import Any, Optional
from datetime import datetime
from chronix.integrations.google_docs.auth import get_default_auth_strategy, AuthStrategy, SCOPES
from googleapiclient.discovery import build


class GoogleCalendarClient:
    """Client for interacting with Google Calendar API."""
    
    def __init__(self, auth_strategy: Optional[AuthStrategy] = None):
        self.auth_strategy = auth_strategy or get_default_auth_strategy()
        self._service = None
    
    @property
    def service(self) -> Any:
        """Lazily build Calendar service from the shared auth strategy."""
        if self._service is None:
            creds = self.auth_strategy.get_credentials(interactive=False)
            self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def authenticate(self, interactive: bool = False) -> bool:
        """Validate shared credentials and initialize the Calendar service."""
        creds = self.auth_strategy.get_credentials(interactive=interactive)
        self._service = build("calendar", "v3", credentials=creds)
        return True

    def list_events(self, calendar_id: str, start_time: datetime, end_time: datetime) -> list[dict]:
        """List calendar events in the given time range."""
        events_result = self.service.events().list(
            calendarId=calendar_id,
            timeMin=start_time.isoformat(),
            timeMax=end_time.isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        
        return events_result.get('items', [])
    
    def create_event(self, calendar_id: str, event_data: dict) -> dict:
        """Create a new calendar event."""
        return self.service.events().insert(
            calendarId=calendar_id,
            body=event_data
        ).execute()
    
    def update_event(self, calendar_id: str, event_id: str, event_data: dict) -> dict:
        """Update an existing calendar event."""
        return self.service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=event_data
        ).execute()
    
    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete a calendar event."""
        self.service.events().delete(
            calendarId=calendar_id,
            eventId=event_id
        ).execute()
    
    def get_primary_calendar(self) -> str:
        """Get the primary calendar ID for the authenticated user."""
        calendar = self.service.calendars().get(calendarId='primary').execute()
        return calendar['id']
