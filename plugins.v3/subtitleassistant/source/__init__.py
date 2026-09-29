"""字幕来源查询与来源管理能力的调用侧契约。"""

from .conclusion import (
    SOURCE_NAMES,
    SOURCE_SKIP_REASONS,
    describe_source_run,
    source_run_is_warning,
)
from .service import SourceAdministration

__all__ = [
    "SOURCE_NAMES",
    "SOURCE_SKIP_REASONS",
    "SourceAdministration",
    "describe_source_run",
    "source_run_is_warning",
]
