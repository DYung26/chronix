from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chronix.api.service import widget_tasks_from_intervals


def test_widget_tasks_use_current_titles_and_skip_completed_or_missing_tasks():
    start = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=45)
    intervals = [
        {"id": "open", "start": start, "end": end, "track": "primary"},
        {"id": "done", "start": start, "end": end, "track": "secondary"},
        {"id": "removed", "start": start, "end": end, "track": "primary"},
    ]
    tasks = {
        "open": SimpleNamespace(title="Current task title", completed=False),
        "done": SimpleNamespace(title="Completed task", completed=True),
    }

    result = widget_tasks_from_intervals(intervals, tasks)

    assert result == [
        {
            "id": "open",
            "title": "Current task title",
            "startAt": start.isoformat(),
            "endAt": end.isoformat(),
            "track": "primary",
        }
    ]
