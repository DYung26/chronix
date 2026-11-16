from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from chronix.api import service
from chronix.api.service import ActiveTaskProvider, widget_tasks_from_intervals


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


def test_active_task_provider_keeps_successfully_synced_projects_on_partial_failure(
    monkeypatch, caplog
):
    project = SimpleNamespace(project_context=SimpleNamespace(project_id="available"))
    monkeypatch.setattr(service._context, "projects", [])
    monkeypatch.setattr(service._context, "ad_hoc_meetings", [])
    monkeypatch.setattr(service._context, "last_sync", None)
    monkeypatch.setattr(service._context, "config", None)

    def sync_with_partial_failure():
        service._context.projects = [project]
        return {
            "ok": False,
            "failures": [{"project_name": "temporarily unavailable"}],
        }

    monkeypatch.setattr(service.tools_read, "sync", sync_with_partial_failure)

    provider = ActiveTaskProvider()
    provider._refresh_context()

    assert service._context.projects == [project]
    assert provider._last_context_sync is not None
    assert "serving 1 successfully synced projects" in caplog.text


def test_active_task_provider_restores_cached_context_when_all_sources_fail(
    monkeypatch, caplog
):
    project = SimpleNamespace(project_context=SimpleNamespace(project_id="cached"))
    cached_meetings = [object()]
    cached_sync = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    cached_config = object()
    monkeypatch.setattr(service._context, "projects", [project])
    monkeypatch.setattr(service._context, "ad_hoc_meetings", cached_meetings)
    monkeypatch.setattr(service._context, "last_sync", cached_sync)
    monkeypatch.setattr(service._context, "config", cached_config)

    def failed_sync():
        service._context.projects = []
        service._context.ad_hoc_meetings = []
        service._context.last_sync = None
        service._context.config = None
        return {"ok": False, "failures": [{"project_name": "offline"}]}

    monkeypatch.setattr(service.tools_read, "sync", failed_sync)

    ActiveTaskProvider()._refresh_context()

    assert service._context.projects == [project]
    assert service._context.ad_hoc_meetings is cached_meetings
    assert service._context.last_sync is cached_sync
    assert service._context.config is cached_config
    assert "serving cached task context" in caplog.text


def test_active_task_provider_fails_when_no_project_context_can_be_loaded(monkeypatch):
    monkeypatch.setattr(service._context, "projects", [])
    monkeypatch.setattr(service._context, "ad_hoc_meetings", [])
    monkeypatch.setattr(service._context, "last_sync", None)
    monkeypatch.setattr(service._context, "config", None)
    monkeypatch.setattr(
        service.tools_read,
        "sync",
        lambda: {"ok": False, "failures": [{"project_name": "offline"}]},
    )

    with pytest.raises(RuntimeError, match="task sources could not be loaded"):
        ActiveTaskProvider()._refresh_context()


class _EmptyAggregator:
    def aggregate(self, projects):
        return []

    def get_task_pool(self, aggregated):
        return []


def test_active_task_provider_caches_calendar_snapshot(monkeypatch):
    project = SimpleNamespace(project_context=SimpleNamespace(project_id="project"))
    monkeypatch.setattr(service._context, "projects", [project])
    monkeypatch.setattr(service, "TaskAggregator", _EmptyAggregator)

    class CalendarService:
        calls = 0

        def get_active_scheduled_tasks(self, now):
            self.calls += 1
            return []

    provider = ActiveTaskProvider(calendar_refresh_seconds=30)
    provider._refresh_context = lambda: None
    provider._calendar_service = CalendarService()

    first = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    provider.get_active_tasks(first)
    provider.get_active_tasks(first + timedelta(seconds=10))

    assert provider._calendar_service.calls == 1


def test_active_task_provider_serves_cached_calendar_snapshot_on_refresh_failure(monkeypatch, caplog):
    project = SimpleNamespace(project_context=SimpleNamespace(project_id="project"))
    monkeypatch.setattr(service._context, "projects", [project])
    monkeypatch.setattr(service, "TaskAggregator", _EmptyAggregator)

    class CalendarService:
        calls = 0

        def get_active_scheduled_tasks(self, now):
            self.calls += 1
            if self.calls > 1:
                raise TimeoutError("calendar timeout")
            return []

    provider = ActiveTaskProvider(calendar_refresh_seconds=0)
    provider._refresh_context = lambda: None
    provider._calendar_service = CalendarService()

    now = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    assert provider.get_active_tasks(now) == []
    assert provider.get_active_tasks(now + timedelta(seconds=1)) == []
    assert "serving the last successful interval snapshot" in caplog.text


def test_widget_data_includes_active_configured_blocks_without_task_projects(monkeypatch):
    from chronix.core.models import TimeBlock

    now = datetime(2026, 11, 16, 10, 15, tzinfo=timezone.utc)
    block = TimeBlock(
        start=datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc),
        kind="break",
        label="Sleep",
    )
    config = SimpleNamespace(scheduling=SimpleNamespace(timezone="UTC"))
    monkeypatch.setattr(service._context, "projects", [])
    monkeypatch.setattr(service._context, "config", config)
    monkeypatch.setattr(service._context, "ad_hoc_meetings", [])
    monkeypatch.setattr(service, "config_to_time_blocks", lambda config, target_date: [block] if target_date == now.date() else [])

    provider = ActiveTaskProvider()
    provider._refresh_context = lambda: None

    result = provider.get_widget_data(now)

    assert result["tasks"] == []
    assert result["blocks"] == [{
        "id": "break:2026-11-16T10:00:00+00:00:Sleep",
        "title": "Sleep",
        "kind": "break",
        "startAt": "2026-11-16T10:00:00+00:00",
        "endAt": "2026-11-16T11:00:00+00:00",
    }]


def test_widget_data_includes_upcoming_configured_blocks(monkeypatch):
    from chronix.core.models import TimeBlock

    now = datetime(2026, 11, 16, 10, 15, tzinfo=timezone.utc)
    block = TimeBlock(
        start=datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc),
        end=datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc),
        kind="meeting",
        label="Later",
    )
    monkeypatch.setattr(service._context, "projects", [])
    monkeypatch.setattr(service._context, "config", SimpleNamespace(scheduling=SimpleNamespace(timezone="UTC")))
    monkeypatch.setattr(service._context, "ad_hoc_meetings", [])
    monkeypatch.setattr(service, "config_to_time_blocks", lambda config, target_date: [block] if target_date == now.date() else [])

    provider = ActiveTaskProvider()
    provider._refresh_context = lambda: None

    result = provider.get_widget_data(now)
    assert result["tasks"] == []
    assert result["blocks"][0]["title"] == "Later"
    assert result["blocks"][0]["startAt"] == "2026-11-16T11:00:00+00:00"
