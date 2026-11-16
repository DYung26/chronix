"""Application-facing read service for the Chronix task widget."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

from chronix.cli.commands import _context
from chronix.core.aggregation import TaskAggregator
from chronix.config import ChronixConfig, config_to_time_blocks
from chronix.integrations.google_calendar import CalendarSyncService
from chronix.mcp import tools_read

logger = logging.getLogger(__name__)


def widget_tasks_from_intervals(
    intervals: list[dict[str, Any]],
    tasks_by_id: dict[str, Any],
) -> list[dict[str, str]]:
    """Join persisted schedule intervals to current, incomplete Chronix tasks."""
    result = []
    for interval in intervals:
        task = tasks_by_id.get(interval["id"])
        if task is None or task.completed:
            continue
        result.append(
            {
                "id": interval["id"],
                "title": task.title,
                "startAt": interval["start"].isoformat(),
                "endAt": interval["end"].isoformat(),
                "track": interval["track"],
            }
        )
    return result


class ActiveTaskProvider:
    """Resolve active scheduled intervals from Chronix and its calendar snapshot."""

    def __init__(
        self,
        context_refresh_seconds: float = 30.0,
        calendar_refresh_seconds: float = 30.0,
    ) -> None:
        self.context_refresh_seconds = context_refresh_seconds
        self.calendar_refresh_seconds = calendar_refresh_seconds
        self._last_context_sync: float | None = None
        self._last_calendar_sync: float | None = None
        self._cached_intervals: list[dict[str, Any]] | None = None
        self._calendar_service: CalendarSyncService | None = None
        self._lock = threading.RLock()

    def get_widget_data(self, now: datetime | None = None) -> dict[str, list[dict[str, str]]]:
        """Resolve current tasks and configured time blocks for the widget."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        with self._lock:
            try:
                self._refresh_context()
            except RuntimeError:
                # The personal schedule is still useful when task sources are
                # temporarily unavailable. Keep configured blocks visible.
                logger.exception("Task context unavailable; serving configured time blocks")

            config = _context.config or ChronixConfig.load_or_default()
            local_now = now.astimezone(ZoneInfo(config.scheduling.timezone))
            blocks = []
            for block in config_to_time_blocks(config, local_now.date()):
                blocks.append({
                        "id": f"{block.kind}:{block.start.isoformat()}:{block.label or ''}",
                        "title": block.label or block.kind.replace("_", " ").title(),
                        "kind": block.kind,
                        "startAt": block.start.isoformat(),
                        "endAt": block.end.isoformat(),
                    })

            for meeting in _context.ad_hoc_meetings:
                if meeting.start.date() == local_now.date():
                    blocks.append({
                        "id": f"meeting:{meeting.start.isoformat()}:{meeting.label or ''}",
                        "title": meeting.label or "Meeting",
                        "kind": "meeting",
                        "startAt": meeting.start.isoformat(),
                        "endAt": meeting.end.isoformat(),
                    })

            blocks.sort(key=lambda block: (block["startAt"], block["id"]))
            if not _context.projects:
                return {"tasks": [], "blocks": blocks}

            aggregator = TaskAggregator()
            aggregated = aggregator.aggregate(_context.projects)
            tasks = aggregator.get_task_pool(aggregated)
            tasks_by_id = {task.id: task for task in tasks if task.id}

            if self._calendar_service is None:
                google_client = _context._ensure_google_client()
                google_client.authenticate(interactive=False)
                self._calendar_service = CalendarSyncService(
                    auth_strategy=google_client.auth_strategy
                )
            monotonic_now = time.monotonic()
            cache_is_fresh = (
                self._cached_intervals is not None
                and self._last_calendar_sync is not None
                and monotonic_now - self._last_calendar_sync < self.calendar_refresh_seconds
            )
            if not cache_is_fresh:
                try:
                    intervals = self._calendar_service.get_active_scheduled_tasks(now)
                except Exception:
                    if self._cached_intervals is None:
                        raise
                    logger.exception(
                        "Calendar refresh failed; serving the last successful interval snapshot"
                    )
                else:
                    self._cached_intervals = intervals
                    self._last_calendar_sync = monotonic_now

            return {
                "tasks": widget_tasks_from_intervals(self._cached_intervals or [], tasks_by_id),
                "blocks": blocks,
            }

    def get_active_tasks(self, now: datetime | None = None) -> list[dict[str, str]]:
        """Backward-compatible task-only accessor."""
        return self.get_widget_data(now)["tasks"]

    def _refresh_context(self) -> None:
        now = time.monotonic()
        if (
            self._last_context_sync is not None
            and now - self._last_context_sync < self.context_refresh_seconds
        ):
            return

        previous = (
            _context.projects,
            _context.ad_hoc_meetings,
            _context.last_sync,
            _context.config,
        )
        result = tools_read.sync()
        self._last_context_sync = now

        if result.get("ok"):
            return

        if _context.projects and _context.projects is not previous[0]:
            logger.warning(
                "Chronix source refresh was partial; serving %d successfully synced projects: %s",
                len(_context.projects),
                result.get("failures", []),
            )
            return

        (
            _context.projects,
            _context.ad_hoc_meetings,
            _context.last_sync,
            _context.config,
        ) = previous
        if _context.projects:
            logger.warning(
                "Chronix source refresh failed; serving cached task context: %s", result
            )
            return
        raise RuntimeError("Chronix task sources could not be loaded")


def create_payload(
    provider: ActiveTaskProvider, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if hasattr(provider, "get_widget_data"):
        widget_data = provider.get_widget_data(now)
    else:
        widget_data = {"tasks": provider.get_active_tasks(now), "blocks": []}
    return {
        "ok": True,
        "serverTime": now.isoformat(),
        "tasks": widget_data["tasks"],
        "blocks": widget_data["blocks"],
        "pollAfterMs": 10000,
    }
