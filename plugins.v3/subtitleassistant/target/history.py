"""整理历史目标查询与宿主目标事实投影。"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.sdk import queries as sdk_queries
from app.sdk.queries import (
    MAX_QUERY_PAGE_SIZE,
    QueryPage,
    QueryPageRequest,
    TransferHistoryFilter,
    TransferHistorySnapshot,
)

from ..schemas.config import PluginConfig
from ..schemas.target import ResolvedTarget, SearchTarget, SubtitleTarget
from .mapping import resolve_path
from .projection import target_from_history


@dataclass(slots=True)
class TransferHistoryPage:
    """宿主整理历史稳定分页结果。"""

    items: Sequence[TransferHistorySnapshot]
    page: int
    page_size: int
    total: int


class TransferHistoryPort(Protocol):
    """稳定宿主历史查询所需的最小 SDK 端口。"""

    async def async_list_transfer_history(
        self,
        *,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryPage[TransferHistorySnapshot]:
        """分页读取整理历史快照。"""

    async def async_get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None:
        """按编号读取整理历史快照。"""


class _TransferHistoryAdapter:
    """通过稳定 SDK 查询整理历史，由宿主管理短事务。"""

    async def async_list_transfer_history(
        self,
        *,
        filters: TransferHistoryFilter,
        page: QueryPageRequest,
    ) -> QueryPage[TransferHistorySnapshot]:
        """通过 SDK 分页读取整理历史快照。"""

        return await sdk_queries.async_list_transfer_history(filters=filters, page=page)

    async def async_get_transfer_history(
        self,
        history_id: int,
    ) -> TransferHistorySnapshot | None:
        """通过 SDK 读取单条整理历史快照。"""

        return await sdk_queries.async_get_transfer_history(history_id)


def _history_filter(value: str | None) -> TransferHistoryFilter:
    """把目标搜索输入转换为稳定 SDK 的整理历史筛选。"""

    query = (value or "").strip()
    if query == "成功":
        return TransferHistoryFilter(status=True)
    if query == "失败":
        return TransferHistoryFilter(status=False)
    return TransferHistoryFilter(text=query or None)


class TargetCatalogService:
    """拥有整理历史分页、目标投影与实际字幕路径解析。"""

    def __init__(
        self,
        history_query: TransferHistoryPort | None = None,
        batch_size: int = 100,
        config_provider: Callable[[], PluginConfig] | None = None,
    ) -> None:
        """创建目标目录服务。"""

        self._history_query = history_query or _TransferHistoryAdapter()
        self._batch_size = batch_size
        self._config_provider = config_provider or PluginConfig

    async def _histories(self) -> list[TransferHistorySnapshot]:
        """分页读取全部成功整理历史。"""
        result: list[TransferHistorySnapshot] = []
        page_number = 1
        filters = TransferHistoryFilter(status=True)
        count = min(self._batch_size, MAX_QUERY_PAGE_SIZE)
        while True:
            page = await self._history_query.async_list_transfer_history(
                filters=filters,
                page=QueryPageRequest(page=page_number, count=count),
            )
            if not page.items:
                break
            result.extend(page.items)
            if not page.has_next:
                break
            page_number += 1
        return result

    def _to_target(self, history: TransferHistorySnapshot | None) -> SearchTarget | None:
        """把一条成功的本地文件整理历史投影为插件目标。"""

        return target_from_history(history)

    async def list_targets(
        self,
        page: int = 1,
        page_size: int = 25,
        search: str | None = None,
    ) -> TransferHistoryPage:
        """按稳定宿主历史查询语义返回当前页整理历史快照。"""

        result = await self._history_query.async_list_transfer_history(
            filters=_history_filter(search),
            page=QueryPageRequest(page=page, count=page_size),
        )
        return TransferHistoryPage(
            items=result.items,
            page=result.page,
            page_size=result.count,
            total=result.total,
        )

    async def list_all_targets(self) -> Sequence[SearchTarget]:
        """返回按最新整理时间去重的有效历史目标。"""

        converted = [self._to_target(row) for row in await self._histories()]
        valid = sorted(
            (item for item in converted if item is not None), key=lambda item: item.transferred_at, reverse=True
        )
        unique: dict[str, SearchTarget] = {}
        for item in valid:
            key = os.path.normcase(os.path.abspath(item.context.target_path))
            unique.setdefault(key, item)
        return list(unique.values())

    async def get_target(self, history_id: int) -> SearchTarget | None:
        """按历史编号返回成功的本地文件目标快照。"""

        history = await self._history_query.async_get_transfer_history(history_id)
        return self._to_target(history)

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> ResolvedTarget:
        """仅在执行文件操作时按当前配置解析并冻结实际字幕目标。"""

        config = self._config_provider()
        resolution = resolve_path(target.target_path, getattr(config, "path_mappings", ()))
        return ResolvedTarget(
            original_path=resolution.original_path,
            resolved_path=resolution.resolved_path,
            mapping=resolution.mapping,
            title=target.title,
            original_title=target.original_title,
            english_title=target.english_title,
            year=target.year,
            media_type=target.media_type,
            season=target.season,
            episode=target.episode,
            tmdb_id=target.tmdb_id,
            imdb_id=target.imdb_id,
            target_file_name=target.target_file_name,
            target_storage=target.target_storage,
            target_type=target.target_type,
            target_extension=target.target_extension,
            target_container=target.target_container,
        )
