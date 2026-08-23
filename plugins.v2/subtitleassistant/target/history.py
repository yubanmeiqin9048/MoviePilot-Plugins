"""整理历史目标查询与宿主目标事实投影。"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.schemas.types import MediaType as HostMediaType

from ..schemas.attribution import CandidateMatchContext
from ..schemas.config import PluginConfig
from ..schemas.target import MediaType, PathMappingResolution, SearchTarget, SubtitleTarget
from .mapping import resolve_path


@dataclass(slots=True)
class TransferHistoryPage:
    """宿主整理历史原始分页结果。"""

    items: list[TransferHistory]
    page: int
    page_size: int


class TransferHistoryPort(Protocol):
    """整理历史查询所需的最小宿主端口。"""

    async def async_list_by_page(self, page: int, count: int, status: bool | None = None) -> list[TransferHistory]:
        """分页读取整理历史。"""

    async def async_get(self, historyid: int) -> TransferHistory | None:
        """按编号读取整理历史。"""


def _parse_number(value: object) -> int | None:
    """从宿主季集字段中读取第一个整数。"""

    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _parse_history_time(value: object) -> datetime:
    """把宿主整理时间归一化为 UTC。"""

    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(UTC)


class TargetCatalogService:
    """拥有整理历史分页、目标投影与实际字幕路径解析。"""

    def __init__(
        self,
        history_oper: TransferHistoryPort | None = None,
        batch_size: int = 100,
        config_provider: Callable[[], PluginConfig] | None = None,
    ) -> None:
        """创建目标目录服务。"""

        self._history_oper = history_oper or TransferHistoryOper()
        self._batch_size = batch_size
        self._config_provider = config_provider or PluginConfig

    async def _histories(self) -> list[TransferHistory]:
        """分页读取全部成功整理历史。"""

        result: list[TransferHistory] = []
        page = 1
        while True:
            batch = await self._history_oper.async_list_by_page(page=page, count=self._batch_size, status=True)
            if not batch:
                break
            result.extend(batch)
            if len(batch) < self._batch_size:
                break
            page += 1
        return result

    def _to_target(self, history: TransferHistory | None) -> SearchTarget | None:
        """把一条成功的本地文件整理历史投影为插件目标。"""

        if history is None:
            return None
        path_value = getattr(history, "dest", None)
        file_data = getattr(history, "dest_fileitem", None)
        if (
            getattr(history, "status", False) is not True
            or getattr(history, "dest_storage", None) != "local"
            or not isinstance(path_value, str)
            or not path_value.strip()
            or not isinstance(file_data, dict)
            or file_data.get("type") != "file"
        ):
            return None
        raw_type = str(getattr(history, "type", "") or "")
        media_type = (
            MediaType.TV
            if raw_type in {HostMediaType.TV.value, "tv", "TV"}
            else MediaType.MOVIE
            if raw_type in {HostMediaType.MOVIE.value, "movie", "MOVIE"}
            else MediaType.UNKNOWN
        )
        try:
            year_value = getattr(history, "year", None)
            year = int(str(year_value)) if year_value is not None else None
        except (TypeError, ValueError):
            year = None
        try:
            tmdb_value = getattr(history, "tmdbid", None)
            tmdb_id = int(str(tmdb_value)) if tmdb_value is not None else None
        except (TypeError, ValueError):
            tmdb_id = None
        context = SubtitleTarget(
            title=str(getattr(history, "title", "") or Path(path_value).stem),
            original_title=getattr(history, "original_title", None),
            english_title=getattr(history, "en_title", None),
            year=year,
            media_type=media_type,
            season=_parse_number(getattr(history, "seasons", None)),
            episode=_parse_number(getattr(history, "episodes", None)),
            tmdb_id=tmdb_id,
            imdb_id=getattr(history, "imdbid", None),
            target_path=Path(path_value),
            target_file_name=str(file_data.get("name") or Path(path_value).name),
            target_storage="local",
            target_type="file",
            target_extension=str(file_data.get("extension") or Path(path_value).suffix).lstrip("."),
            target_container=file_data.get("container") if isinstance(file_data.get("container"), str) else None,
        )
        aliases = tuple(
            value.strip()
            for value in (context.english_title, context.original_title)
            if isinstance(value, str) and value.strip() and value.strip() != context.title
        )
        raw_douban = getattr(history, "doubanid", None)
        return SearchTarget(
            history_id=int(history.id),
            context=context,
            transferred_at=_parse_history_time(getattr(history, "date", None)),
            match_context=CandidateMatchContext(
                title=context.title,
                aliases=aliases,
                original_title=context.original_title,
                year=context.year,
                media_type=context.media_type,
                tmdb_id=context.tmdb_id,
                imdb_id=context.imdb_id,
                douban_id=str(raw_douban).strip() if raw_douban not in (None, "") else None,
                bangumi_id=_parse_number(getattr(history, "bangumiid", None)),
                anilist_id=_parse_number(getattr(history, "anilistid", None)),
            ),
        )

    async def list_targets(self, page: int = 1, page_size: int = 25) -> TransferHistoryPage:
        """原样返回宿主整理历史当前页，不附加状态过滤。"""

        items = await self._history_oper.async_list_by_page(page=page, count=page_size)
        return TransferHistoryPage(items=items, page=page, page_size=page_size)

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

        history = await self._history_oper.async_get(history_id)
        return self._to_target(history)

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> PathMappingResolution:
        """仅在执行文件操作时按当前配置解析实际字幕目标路径。"""

        config = self._config_provider()
        return resolve_path(target.target_path, getattr(config, "path_mappings", ()))
