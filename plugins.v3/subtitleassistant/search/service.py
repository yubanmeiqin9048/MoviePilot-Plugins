"""手动与自动流程共用的字幕搜索入口。"""

from __future__ import annotations

from collections.abc import Mapping

from ..schemas.source import SourceSearchBatch, SubtitleSource
from ..schemas.target import SubtitleTarget
from ..source import SourceAdministration


class SubtitleSearch:
    """汇总来源候选与执行结果，不执行识别、准入或排序。"""

    def __init__(self, sources: SourceAdministration) -> None:
        """绑定共享的字幕来源能力。"""

        self._sources = sources

    async def search(
        self,
        target: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> SourceSearchBatch:
        """按目标与可选查询词返回完整的来源搜索批次。"""

        return await self._sources.query(target, custom_queries)
