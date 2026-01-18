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
    
    Args:
        config: ChronixConfig instance
        target_date: Date to generate blocks for
        timezone: Optional timezone override
    
    Returns:
        List of TimeBlock objects
    """
    tz_str = timezone or config.scheduling.timezone
    tz = ZoneInfo(tz_str)
    
    blocks = []
    day_name = target_date.strftime("%A").lower()
    
    # Process all configured time blocks
    all_blocks = (
        config.scheduling.sleep_windows +
        config.scheduling.breaks +
        config.scheduling.meetings
    )
    
    for block_config in all_blocks:
        # Check if this block applies to the target day
        if day_name not in block_config.days:
            continue
        
        # Create datetime objects for start and end
        start_dt = datetime.combine(target_date, block_config.start_time, tzinfo=tz)
        end_dt = datetime.combine(target_date, block_config.end_time, tzinfo=tz)
        
        # Create TimeBlock
        block = TimeBlock(
            start=start_dt,
            end=end_dt,
            kind=block_config.kind,
            label=block_config.label
        )
        blocks.append(block)
    
    return blocks


def get_work_window(
    config: ChronixConfig,
    target_date: date,
    timezone: Optional[str] = None
) -> tuple[datetime, datetime]:
    """
    Get work start and end times for a specific date.
    
    Args:
        config: ChronixConfig instance
        target_date: Date to get work window for
        timezone: Optional timezone override
    
    Returns:
        Tuple of (work_start, work_end) as datetime objects
    """
    tz_str = timezone or config.scheduling.timezone
    tz = ZoneInfo(tz_str)
    
    work_start = datetime.combine(
        target_date,
        config.scheduling.work_start_time,
        tzinfo=tz
    )
    
    work_end = datetime.combine(
        target_date,
        config.scheduling.work_end_time,
        tzinfo=tz
    )
    
    return work_start, work_end
