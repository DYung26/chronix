"""Utilities for converting configuration to domain models."""

from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from chronix.config.settings import ChronixConfig, TimeBlockConfig
from chronix.core.models import TimeBlock


def config_to_time_blocks(
    config: ChronixConfig,
    target_date: date,
    timezone: Optional[str] = None
) -> list[TimeBlock]:
    """
    Convert configuration time blocks to domain TimeBlock objects for a specific date.
    """
    tz_str = timezone or config.scheduling.timezone
    tz = ZoneInfo(tz_str)

    blocks = []
    day_name = target_date.strftime("%A").lower()

    all_blocks = (
        config.scheduling.sleep_windows +
        config.scheduling.breaks +
        config.scheduling.meetings
    )

    for block_config in all_blocks:
        if day_name not in block_config.days:
            continue

        start_dt = datetime.combine(target_date, block_config.start_time, tzinfo=tz)
        end_dt = datetime.combine(target_date, block_config.end_time, tzinfo=tz)

        blocks.append(TimeBlock(
            start=start_dt,
            end=end_dt,
            kind=block_config.kind,
            label=block_config.label,
        ))

    return blocks


def get_work_windows(
    config: ChronixConfig,
    target_date: date,
    timezone: Optional[str] = None
) -> list[tuple[datetime, datetime]]:
    """
    Return all work windows for a day as (start, end) datetime tuples.

    Gaps between windows are automatically added as blocked time by the
    caller (commands.py) so the scheduler skips them.
    """
    tz_str = timezone or config.scheduling.timezone
    tz = ZoneInfo(tz_str)

    windows = config.scheduling.effective_work_windows()
    return [
        (
            datetime.combine(target_date, w.start_time, tzinfo=tz),
            datetime.combine(target_date, w.end_time, tzinfo=tz),
        )
        for w in windows
    ]


def get_work_window(
    config: ChronixConfig,
    target_date: date,
    timezone: Optional[str] = None
) -> tuple[datetime, datetime]:
    """
    Return the overall work span for a day: earliest window start to latest window end.

    For display purposes and the schedule header. For scheduling, use get_work_windows.
    """
    windows = get_work_windows(config, target_date, timezone)
    return windows[0][0], windows[-1][1]
