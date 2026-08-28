"""Configuration management for chronix."""

from chronix.config.settings import (
    ChronixConfig,
    SchedulingConfig,
    GoogleDocsConfig,
    DocumentConfig,
    TimeBlockConfig,
    WorkWindowConfig,
    StartupConfig,
)
from chronix.config.converters import (
    config_to_time_blocks,
    get_work_window,
    get_work_windows,
)

__all__ = [
    "ChronixConfig",
    "SchedulingConfig",
    "GoogleDocsConfig",
    "DocumentConfig",
    "TimeBlockConfig",
    "WorkWindowConfig",
    "StartupConfig",
    "config_to_time_blocks",
    "get_work_window",
    "get_work_windows",
]
