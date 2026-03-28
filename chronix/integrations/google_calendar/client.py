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
        """Lazily initialize and return the Google Calendar API service."""
        if self._service is None:
            # Get credentials from auth strategy
            creds = None
            if hasattr(self.auth_strategy, 'token_path'):
                from google.oauth2.credentials import Credentials
                token_path = self.auth_strategy.token_path
                if token_path.exists():
                    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            
            if not creds:
                # Fall back to getting service from auth strategy (it uses docs service with expanded scopes)
                # We need to build calendar service directly
                from google.oauth2.credentials import Credentials
                from pathlib import Path
                config_dir = Path.home() / ".config" / "chronix" / "google"
                token_path = config_dir / "token.json"
                
                if token_path.exists():
                    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            
            if creds:
                self._service = build("calendar", "v3", credentials=creds)
            else:
                raise ValueError("Could not initialize Calendar service credentials")
        
        return self._service
    
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
