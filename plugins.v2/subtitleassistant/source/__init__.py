"""字幕来源候选池与来源管理能力的调用侧契约。"""

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ..schemas.source import (
    CandidateHandle,
    CandidatePoolQueryBatchResult,
    DownloadedAsset,
    SubtitleSource,
)
from ..schemas.target import SubtitleTarget
from .service import SourceAdministration


class CandidatePool(Protocol):
    """按归一化字幕目标查询全部来源候选。"""

    async def query(
        self,
        context: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> CandidatePoolQueryBatchResult:
        """查询候选池并返回按来源分组的安全结果。"""

    async def close(self) -> None:
        """释放来源查询运行资源。"""

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """安全下载一个来源候选。"""

    def default_queries(self, source: SubtitleSource, context: SubtitleTarget) -> tuple[str, ...]:
        """返回来源默认查询词的安全预览。"""


__all__ = ["CandidatePool", "SourceAdministration"]
