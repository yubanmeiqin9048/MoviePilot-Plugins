"""手动与自动流程共用的字幕搜索入口。"""

from __future__ import annotations

from collections.abc import Mapping

from ..schemas.source import SourceSearchBatch, SubtitleSource
from ..schemas.target import SubtitleTarget
from ..source import SourceAdministration


class SubtitleSearch:
    """编排字幕搜索流程：标准化输入、协调来源查询、为结果附加上下文。"""

    def __init__(self, sources: SourceAdministration) -> None:
        """绑定共享的字幕来源能力。"""

        self._sources = sources

    async def search(
        self,
        target: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource | str, str | None] | None = None,
    ) -> SourceSearchBatch:
        """按目标与可选查询词返回完整的来源搜索批次。

        Args:
            target: 字幕搜索目标上下文
            custom_queries: 可选的自定义查询词，键可以是 SubtitleSource 枚举或其字符串值

        Returns:
            包含所有来源搜索结果的批次对象
        """

        normalized = self._normalize_queries(custom_queries)
        return await self._sources.query(target, normalized)

    def _normalize_queries(
        self,
        queries: Mapping[SubtitleSource | str, str | None] | None,
    ) -> dict[SubtitleSource, str | None]:
        """标准化自定义查询词，支持字符串和枚举混合输入，为所有来源生成条目。"""

        # 先为所有来源生成 None 默认值
        normalized: dict[SubtitleSource, str | None] = {source: None for source in SubtitleSource}

        if not queries:
            return normalized

        # 填充用户提供的查询词
        for key, value in queries.items():
            if isinstance(key, SubtitleSource):
                normalized[key] = value
            elif isinstance(key, str):
                # 尝试将字符串解析为 SubtitleSource
                try:
                    source = SubtitleSource(key)
                    normalized[source] = value
                except ValueError:
                    # 忽略无效的来源标识
                    continue

        return normalized
