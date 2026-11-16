from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from chronix.integrations.google_calendar.sync_service import CalendarSyncService


def _event(task_id, start, end, *, title=None, track="primary", managed=True):
    private = {}
    if managed:
        private = {
            "chronix_managed": "true",
            "chronix_task_id": task_id,
            "chronix_track": track,
        }
    event = {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "summary": title or task_id,
        "extendedProperties": {"private": private},
    }
    return event


def test_get_active_scheduled_tasks_returns_original_overlapping_intervals_and_deduplicates():
    now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    client = MagicMock()
    client.get_primary_calendar.return_value = "primary"
    client.list_events.return_value = [
        _event("task-a", now - timedelta(hours=1), now + timedelta(hours=1), title="Older", track="primary"),
        _event("task-a", now - timedelta(minutes=30), now + timedelta(hours=2), title="Current", track="secondary"),
        _event("task-expired", now - timedelta(hours=2), now, title="Expired"),
        _event("task-future", now + timedelta(seconds=1), now + timedelta(hours=1), title="Future"),
        _event("unmanaged", now - timedelta(minutes=1), now + timedelta(minutes=1), managed=False),
        {
            "summary": "No task id",
            "start": {"dateTime": now.isoformat()},
            "end": {"dateTime": (now + timedelta(minutes=1)).isoformat()},
            "extendedProperties": {"private": {"chronix_managed": "true"}},
        },
    ]
    service = CalendarSyncService(calendar_client=client)

    result = service.get_active_scheduled_tasks(now)

    assert len(result) == 1
    assert result[0]["id"] == "task-a"
    assert result[0]["title"] == "Current"
    assert result[0]["start"] == now - timedelta(minutes=30)
    assert result[0]["end"] == now + timedelta(hours=2)
    assert result[0]["track"] == "secondary"
    client.list_events.assert_called_once_with(
        "primary",
        now - timedelta(seconds=1),
        now + timedelta(seconds=1),
    )


def test_get_active_scheduled_tasks_requires_timezone_aware_now():
    service = CalendarSyncService(calendar_client=MagicMock())

    with pytest.raises(ValueError, match="timezone-aware"):
        service.get_active_scheduled_tasks(datetime(2026, 11, 16, 10, 0))
