"""来源候选池与来源状态管理的组合实现。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, Self

from ..schemas.base import utc_now
from ..schemas.source import (
    CandidateHandle,
    CandidatePoolQueryBatchResult,
    DownloadedAsset,
    SourceHealth,
    SourceStatus,
    SubtitleSource,
)
from ..schemas.target import SubtitleTarget
from .pool import CandidatePoolQueryService, SourceCandidatePoolAdapter, SourceQuery, SourceQueryPlan


class SourceAdapterPort(SourceCandidatePoolAdapter, Protocol):
    """来源组合服务所需的单个来源适配器端口。"""

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """下载一个来源候选。"""

    async def refresh(self, manual: bool = False) -> SourceStatus:
        """刷新来源状态。"""

    async def close(self) -> None:
        """释放来源运行资源。"""


class SourceStorePort(Protocol):
    """来源组合服务所需的状态与凭据持久化端口。"""

    async def list_source_statuses(self) -> list[SourceStatus]:
        """读取来源状态。"""

    async def save_source_status(self, status: SourceStatus) -> None:
        """保存来源状态。"""

    async def update_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """更新来源凭据。"""

    async def get_credentials(self, source: SubtitleSource) -> dict[str, str]:
        """读取来源凭据。"""

    async def clear_credentials(self, source: SubtitleSource) -> None:
        """清除来源凭据。"""


class SourceAdministration:
    """隐藏来源 adapter 字典并统一提供候选池、下载与来源管理。"""

    def __init__(
        self, adapters: Mapping[SubtitleSource, SourceAdapterPort], store: SourceStorePort | None = None
    ) -> None:
        """创建来源能力组合。"""

        self._adapters = dict(adapters)
        self._pool = CandidatePoolQueryService(self._adapters)
        self._store = store

    @classmethod
    def build(
        cls,
        *,
        moviepilot_enabled: bool,
        opensubtitles_enabled: bool,
        assrt_enabled: bool,
        opensubtitles_credentials: dict[str, str],
        assrt_credentials: dict[str, str],
        allowed_formats: set[str],
        store: SourceStorePort,
    ) -> Self:
        """由组合根创建来源 adapter 并返回统一管理 facade。"""

        from .assrt import AssrtSource
        from .moviepilot import MoviePilotSource
        from .opensubtitles import OpenSubtitlesSource

        return cls(
            {
                SubtitleSource.MOVIEPILOT: MoviePilotSource(
                    enabled=moviepilot_enabled,
                    allowed_formats=allowed_formats,
                ),
                SubtitleSource.OPENSUBTITLES: OpenSubtitlesSource(
                    enabled=opensubtitles_enabled,
                    credentials=opensubtitles_credentials,
                    allowed_formats=allowed_formats,
                ),
                SubtitleSource.ASSRT: AssrtSource(
                    enabled=assrt_enabled,
                    credentials=assrt_credentials,
                    allowed_formats=allowed_formats,
                ),
            },
            store=store,
        )

    async def query(
        self,
        context: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> CandidatePoolQueryBatchResult:
        """查询全部来源候选。"""

        return await self._pool.query(context, custom_queries)

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """通过候选来源句柄下载字幕资产。"""

        adapter = self._adapters[handle.candidate.source]
        return await adapter.download(handle, directory)

    def default_queries(self, source: SubtitleSource, context: SubtitleTarget) -> tuple[str, ...]:
        """返回前端展示使用的来源默认查询词。"""

        adapter = self._adapters.get(source)
        if adapter is None:
            return ()
        plan = adapter.query_plan(context, None)
        queries: Sequence[SourceQuery] = plan.queries if isinstance(plan, SourceQueryPlan) else plan
        return tuple(item.label for item in queries if item.label)

    async def statuses(self) -> list[SourceStatus]:
        """读取持久化来源状态。"""

        if self._store is None:
            return []
        return await self._store.list_source_statuses()

    def status_snapshot(self, source: SubtitleSource) -> SourceStatus:
        """返回来源当前配置与非敏感运行详情。"""

        adapter = self._adapters.get(source)
        details = getattr(adapter, "runtime_details", dict)()
        return SourceStatus(
            source=source,
            enabled=bool(getattr(adapter, "enabled", False)),
            configured=bool(getattr(adapter, "configured", adapter is not None)),
            details=details,
        )

    async def refresh(self, manual: bool = False) -> list[SourceStatus]:
        """刷新全部来源并持久化状态。"""

        previous = {item.source: item for item in await self._store.list_source_statuses()} if self._store else {}
        results = await asyncio.gather(
            *(adapter.refresh(manual=manual) for adapter in self._adapters.values()), return_exceptions=True
        )
        statuses: list[SourceStatus] = []
        for source, result in zip(self._adapters, results, strict=True):
            if isinstance(result, SourceStatus):
                status = result
            else:
                adapter = self._adapters[source]
                status = SourceStatus(
                    source=source,
                    enabled=bool(getattr(adapter, "enabled", False)),
                    configured=bool(getattr(adapter, "configured", True)),
                    health=SourceHealth.ERROR,
                    last_checked_at=utc_now(),
                    last_error_at=utc_now(),
                    last_error_summary="字幕源状态刷新失败",
                )
            old = previous.get(source)
            if old is not None:
                status.last_success_at = status.last_success_at or old.last_success_at
                status.last_error_at = status.last_error_at or old.last_error_at
                status.last_error_summary = status.last_error_summary or old.last_error_summary
                status.details = {**old.details, **status.details}
            statuses.append(status)
            if self._store is not None:
                await self._store.save_source_status(status)
        return statuses

    async def update_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """更新来源凭据并刷新 adapter。"""

        allowed_fields = {
            SubtitleSource.OPENSUBTITLES: {"api_key", "username", "password"},
            SubtitleSource.ASSRT: {"token"},
        }.get(source, set())
        if not values or set(values) - allowed_fields:
            raise ValueError("请求包含不属于该字幕源的凭据字段")
        if self._store is None:
            return False
        configured = await self._store.update_credentials(source, values)
        adapter = self._adapters.get(source)
        replace_credentials = getattr(adapter, "replace_credentials", None) if adapter is not None else None
        if callable(replace_credentials):
            await replace_credentials(await self._store.get_credentials(source))
        return configured

    async def clear_credentials(self, source: SubtitleSource) -> None:
        """清除来源凭据并立即停用 adapter。"""

        if self._store is not None:
            await self._store.clear_credentials(source)
        adapter = self._adapters.get(source)
        if adapter is not None:
            adapter.enabled = False
            replace_credentials = getattr(adapter, "replace_credentials", None)
            if callable(replace_credentials):
                await replace_credentials({})

    async def close(self) -> None:
        """释放来源 adapter 与候选池缓存。"""

        await self._pool.close()
        await asyncio.gather(*(adapter.close() for adapter in self._adapters.values()), return_exceptions=True)
