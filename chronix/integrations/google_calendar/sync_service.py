"""Google Calendar sync service for syncing Chronix schedules to Google Calendar."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from chronix.core.models import ScheduledTask, DaySchedule, TimeBlock
from chronix.integrations.google_calendar.client import GoogleCalendarClient
from chronix.integrations.google_calendar.models import CalendarSyncResult, ConflictInfo


UTC = ZoneInfo("UTC")
class CalendarEventClassifier:
    """Classifies calendar events for sync purposes."""
    
    @staticmethod
    def is_chronix_managed(event: dict) -> bool:
        """Check if event was created by Chronix."""
        extended_props = event.get('extendedProperties', {}).get('private', {})
        return extended_props.get('chronix_managed') == 'true'
    
    @staticmethod
    def get_chronix_task_id(event: dict) -> Optional[str]:
        """Extract Chronix task ID from event."""
        extended_props = event.get('extendedProperties', {}).get('private', {})
        task_id = extended_props.get('chronix_task_id')
        if task_id:
            return task_id
        
        # Fallback: try to extract from description
        description = event.get('description', '')
        if 'Chronix Task ID:' in description:
            parts = description.split('Chronix Task ID:')
            if len(parts) > 1:
                task_id = parts[1].strip().split('\n')[0]
                return task_id
        
        return None
    
    @staticmethod
    def event_overlaps(event: dict, blocked_window: TimeBlock) -> bool:
        """Check if event overlaps with a blocked time window."""
        event_start = _parse_datetime(event.get('start'))
        event_end = _parse_datetime(event.get('end'))
        
        if not event_start or not event_end:
            return False
        
        # Check for overlap
        return not (event_end <= blocked_window.start or event_start >= blocked_window.end)
    
    @staticmethod
    def corresponds_to_blocked_time(event: dict, blocked_time: list[TimeBlock]) -> bool:
        """Check if event corresponds to a Chronix blocked-time window."""
        for block in blocked_time:
            if CalendarEventClassifier.event_overlaps(event, block):
                return True
        return False


class CalendarSyncService:
    """Orchestrates syncing Chronix schedules to Google Calendar."""
    
    def __init__(self, calendar_client: Optional[GoogleCalendarClient] = None):
        self.client = calendar_client or GoogleCalendarClient()
        self.classifier = CalendarEventClassifier()
    
    def sync(
        self,
        day_schedule: DaySchedule,
        sync_start: datetime,
        sync_end: datetime,
        force: bool = False
    ) -> CalendarSyncResult:
        """
        Sync a day's schedule to Google Calendar.
        
        Args:
            day_schedule: The computed day schedule with scheduled tasks
            sync_start: Start of sync window (effective start time)
            sync_end: End of sync window (typically end of day)
            force: If True, overwrite conflicting non-Chronix events
        
        Returns:
            CalendarSyncResult with success status and sync statistics
        """
        try:
            calendar_id = self.client.get_primary_calendar()
            
            # Fetch existing calendar events in and around the sync window
            # Include events that start before sync_start in case of boundary shortening
            fetch_start = sync_start - timedelta(hours=24)
            existing_events = self.client.list_events(calendar_id, fetch_start, sync_end)
            
            # Classify existing events
            chronix_events = {}
            blocked_time_events = []
            conflicting_events = []
            
            for event in existing_events:
                if self.classifier.is_chronix_managed(event):
                    task_id = self.classifier.get_chronix_task_id(event)
                    if task_id:
                        chronix_events[event['id']] = (event, task_id)
                elif self.classifier.corresponds_to_blocked_time(event, day_schedule.blocked_time):
                    blocked_time_events.append(event)
                else:
                    # Check if this event conflicts with scheduled tasks
                    conflicts = self._find_conflicts(event, day_schedule.scheduled_tasks, sync_start, sync_end)
                    if conflicts:
                        conflicting_events.append((event, conflicts))
            
            # If there are conflicts and no force, return error
            if conflicting_events and not force:
                conflict_infos = []
                for event, conflict_list in conflicting_events:
                    for task_conflict in conflict_list:
                        conflict_infos.append(task_conflict)
                
                return CalendarSyncResult(
                    success=False,
                    conflicts=conflict_infos,
                    error_message=f"Found {len(conflict_infos)} conflicts with existing calendar events. Use --force to overwrite."
                )
            
            # Proceed with sync
            result = CalendarSyncResult(success=True)
            
            # Handle existing Chronix events
            result = self._reconcile_chronix_events(
                calendar_id, chronix_events, day_schedule, sync_start, sync_end
            )
            
            # Handle conflicting non-Chronix events if force=True
            if force and conflicting_events:
                for event, _ in conflicting_events:
                    self.client.delete_event(calendar_id, event['id'])
                    result.deleted_count += 1
            
            # Create new events for scheduled tasks
            for task in day_schedule.scheduled_tasks:
                if task.start >= sync_start and task.start <= sync_end:
                    event_data = self._create_event_data(task)
                    self.client.create_event(calendar_id, event_data)
                    result.created_count += 1
            
            return result
        
        except Exception as e:
            return CalendarSyncResult(
                success=False,
                error_message=f"Sync failed: {str(e)}"
            )
    
    def _find_conflicts(self, event: dict, scheduled_tasks: list[ScheduledTask], sync_start: datetime, sync_end: datetime) -> list[ConflictInfo]:
        """Find conflicts between a calendar event and scheduled tasks."""
        event_start = _parse_datetime(event.get('start'))
        event_end = _parse_datetime(event.get('end'))
        
        if not event_start or not event_end:
            return []
        
        conflicts = []
        for task in scheduled_tasks:
            if task.start >= sync_start and task.start <= sync_end:
                # Check for overlap
                if not (event_end <= task.start or event_start >= task.end):
                    conflicts.append(ConflictInfo(
                        calendar_event_id=event['id'],
                        calendar_event_title=event.get('summary', 'Untitled'),
                        calendar_event_start=event_start,
                        calendar_event_end=event_end,
                        chronix_task_id=task.task.id,
                        chronix_task_title=task.task.title,
                        chronix_scheduled_start=task.start,
                        chronix_scheduled_end=task.end,
                        reason=f"Calendar event overlaps with scheduled task"
                    ))
        
        return conflicts
    
    def _reconcile_chronix_events(self, calendar_id: str, chronix_events: dict, day_schedule: DaySchedule, sync_start: datetime, sync_end: datetime) -> CalendarSyncResult:
        """Reconcile existing Chronix events with the new schedule."""
        result = CalendarSyncResult(success=True)
        
        for event_id, (event, task_id) in chronix_events.items():
            event_start = _parse_datetime(event.get('start'))
            event_end = _parse_datetime(event.get('end'))
            
            if not event_start or not event_end:
                continue
            
            # If event is entirely before sync start, leave it alone
            if event_end <= sync_start:
                continue
            
            # If event starts before sync but extends into it, shorten it
            if event_start < sync_start < event_end:
                event['end'] = _format_datetime(sync_start)
                self.client.update_event(calendar_id, event_id, event)
                result.shortened_count += 1
                continue
            
            # If event is after sync end, leave it alone
            if event_start >= sync_end:
                continue
            
            # If event is within sync window, delete it (will be recreated if still scheduled)
            self.client.delete_event(calendar_id, event_id)
            result.deleted_count += 1
        
        return result
    
    def _create_event_data(self, task: ScheduledTask) -> dict:
        """Create Google Calendar event data from a scheduled task."""
        return {
            'summary': task.task.title,
            'description': self._create_event_description(task),
            'start': {
                'dateTime': task.start.isoformat(),
                'timeZone': str(task.start.tzinfo) if task.start.tzinfo else 'UTC'
            },
            'end': {
                'dateTime': task.end.isoformat(),
                'timeZone': str(task.end.tzinfo) if task.end.tzinfo else 'UTC'
            },
            'extendedProperties': {
                'private': {
                    'chronix_managed': 'true',
                    'chronix_task_id': task.task.id,
                }
            }
        }
    
    def _create_event_description(self, task: ScheduledTask) -> str:
        """Create event description with task metadata."""
        lines = [
            f"Chronix Task ID: {task.task.id}",
            f"Duration: {(task.end - task.start).total_seconds() / 60:.0f} minutes",
        ]
        
        if task.task.ref:
            lines.append(f"Source: {task.task.ref}")
        
        if task.task.depends_on:
            lines.append(f"Depends on: {', '.join(task.task.depends_on)}")
        
        return "\n".join(lines)


def _parse_datetime(dt_dict: dict) -> Optional[datetime]:
    """Parse datetime from Google Calendar event datetime dict."""
    if not dt_dict:
        return None
    
    if 'dateTime' in dt_dict:
        # Parse ISO format datetime string
        iso_str = dt_dict['dateTime']
        # Handle timezone offset
        if '+' in iso_str:
            dt_part, tz_part = iso_str.rsplit('+', 1)
            tz_hours, tz_mins = tz_part.split(':') if ':' in tz_part else (tz_part, '0')
            tz_offset = timedelta(hours=int(tz_hours), minutes=int(tz_mins))
            return datetime.fromisoformat(dt_part).replace(tzinfo=timezone(tz_offset))
        elif iso_str.endswith('Z'):
            return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        else:
            return datetime.fromisoformat(iso_str)
    elif 'date' in dt_dict:
        from datetime import datetime as dt
        date_str = dt_dict['date']
        return dt.strptime(date_str, '%Y-%m-%d').replace(hour=0, minute=0, second=0, tzinfo=UTC)
    
    return None


def _format_datetime(dt: datetime) -> dict:
    """Format datetime for Google Calendar event."""
    return {
        'dateTime': dt.isoformat(),
        'timeZone': str(dt.tzinfo) if dt.tzinfo else 'UTC'
    }
