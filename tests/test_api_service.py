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
