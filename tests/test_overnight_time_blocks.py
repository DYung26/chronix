from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from chronix.config import ChronixConfig, config_to_time_blocks
from chronix.config.settings import TimeBlockConfig


def test_overnight_block_ends_on_following_local_date():
    config = ChronixConfig()
    config.scheduling.timezone = "Africa/Lagos"
    config.scheduling.sleep_windows = [
        TimeBlockConfig(
            start_time=time(22, 0),
            end_time=time(6, 0),
            kind="sleep",
            label="Sleep",
            days=["monday"],
        )
    ]

    block, = config_to_time_blocks(config, date(2026, 11, 16))
    tz = ZoneInfo("Africa/Lagos")
    assert block.start == datetime(2026, 11, 16, 22, 0, tzinfo=tz)
    assert block.end == datetime(2026, 11, 17, 6, 0, tzinfo=tz)
    assert block.kind == "sleep"


def test_overnight_block_is_assigned_to_its_start_day():
    config = ChronixConfig()
    config.scheduling.sleep_windows = [
        TimeBlockConfig(
            start_time=time(22, 0),
            end_time=time(6, 0),
            kind="sleep",
            days=["monday"],
        )
    ]
    assert config_to_time_blocks(config, date(2026, 11, 17)) == []


def test_equal_start_and_end_is_invalid():
    with pytest.raises(ValidationError):
        TimeBlockConfig(
            start_time=time(22, 0),
            end_time=time(22, 0),
            kind="sleep",
        )


def test_normal_same_day_block_is_unchanged():
    config = ChronixConfig()
    config.scheduling.breaks = [
        TimeBlockConfig(
            start_time=time(12, 0),
            end_time=time(12, 30),
            kind="break",
            days=["monday"],
        )
    ]
    block, = config_to_time_blocks(config, date(2026, 11, 16))
    assert block.start.date() == block.end.date()
    assert block.end.hour == 12
    assert block.end.minute == 30
