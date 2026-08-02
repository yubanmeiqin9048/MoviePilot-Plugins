"""可选目标查询与人工字幕搜索会话服务。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.chain.media import MediaChain
from app.core.cache import AsyncMemoryBackend
from app.core.context import MediaInfo as HostMediaInfo
from app.db.models.transferhistory import TransferHistory
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.schemas import FileItem
from app.schemas.types import MediaType as HostMediaType

from ..domain.enums import CandidateRecognitionStatus, MediaType, SubtitleSource
from ..domain.models import CandidateRecognition, MediaContext, SourceDetails, new_id
from .ports import (
    CandidateHandle,
    ManualSourceSearchResult,
    ManualSourceStatus,
    MediaMatcherPort,
    SubtitleSourcePort,
)

SEARCH_SESSION_REGION = "subtitleassistant_manual_search"
SEARCH_SESSION_TTL_SECONDS = 30 * 60

SOURCE_NAMES = {
    SubtitleSource.MOVIEPILOT: "MoviePilot 站点字幕源",
    SubtitleSource.OPENSUBTITLES: "OpenSubtitles",
    SubtitleSource.ASSRT: "ASSRT",
}

QUERY_TYPE_NAMES = {
    "media_id": "媒体 ID",
    "english_title": "英文标题",
    "custom": "自定义关键词",
    "keyword": "英文标题关键词",
}


@dataclass(slots=True)
class SearchTarget:
    """从 MoviePilot 成功整理历史还原的可选目标视频。"""

    history_id: int
    context: MediaContext
    transferred_at: datetime
    target_item: FileItem
    host_mediainfo: HostMediaInfo | None = None


@dataclass(slots=True)
class SearchTargetPage:
    """可选目标视频分页结果。"""

    items: list[SearchTarget]
    total: int
    page: int
    page_size: int


@dataclass(slots=True)
class ManualSourceView:
    """不包含下载句柄的人工来源搜索响应。"""

    source: SubtitleSource
    status: ManualSourceStatus
    candidates: list[CandidateRecognition] = field(default_factory=list)
    default_queries: list[str] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list)
    matched_query: str | None = None
    duration_ms: int | None = None
    error_summary: str | None = None
    details: SourceDetails = field(default_factory=dict)


@dataclass(slots=True)
class ManualSearchResult:
    """一次人工字幕搜索的安全汇总。"""

    session_id: str | None
    target: SearchTarget
    sources: list[ManualSourceView]


@dataclass(slots=True)
class SessionCandidate:
    """搜索会话内可提交到下载队列的完整候选。"""

    session_id: str
    target: SearchTarget
    handle: CandidateHandle
    recognition_status: CandidateRecognitionStatus
    actual_query: str | None = None


@dataclass(slots=True)
class ManualSearchSession:
    """只存在于当前进程内存缓存的人工搜索会话。"""

    session_id: str
    target: SearchTarget
    candidates: dict[str, SessionCandidate]


class ManualSearchCachePort(Protocol):
    """人工搜索会话使用的最小异步缓存端口。"""

    async def get(self, key: str, region: str | None = None) -> object:
        """读取一个未经信任的缓存值。"""

    async def set(
        self,
        key: str,
        value: object,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """保存一个人工搜索会话。"""

    async def clear(self, region: str | None = None) -> None:
        """清除指定缓存区域。"""


class TransferHistoryPort(Protocol):
    """整理历史查询使用的最小宿主端口。"""

    async def async_list_by_page(
        self,
        page: int,
        count: int,
        status: bool,
    ) -> list[TransferHistory]:
        """分页读取整理历史。"""

    async def async_get(self, historyid: int) -> TransferHistory | None:
        """按 ID 读取一条整理历史。"""


def _parse_number(value: object) -> int | None:
    """从整理历史季集字段中提取首个整数。"""

    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else None


def _parse_history_time(value: object) -> datetime:
    """把 MoviePilot 本地时间字符串转换为 UTC 时间。"""

    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(UTC)


class TargetQueryService:
    """通过 MoviePilot 整理历史查询可用的本地单文件目标。"""

    def __init__(self, history_oper: TransferHistoryPort | None = None, batch_size: int = 100) -> None:
        """创建目标查询服务并允许测试替换整理历史操作器。"""

        self._history_oper = history_oper or TransferHistoryOper()
        self._batch_size = batch_size

    async def _histories(self) -> list[TransferHistory]:
        """分页读取全部成功整理历史。"""

        result: list[TransferHistory] = []
        page = 1
        while True:
            batch = await self._history_oper.async_list_by_page(
                page=page,
                count=self._batch_size,
                status=True,
            )
            if not batch:
                break
            result.extend(batch)
            if len(batch) < self._batch_size:
                break
            page += 1
        return result

    async def _to_target(self, history: TransferHistory) -> SearchTarget | None:
        """校验整理历史并还原安全目标上下文。

        整理历史是人工搜索的元数据来源，目标视频即使已经被移走也不影响
        搜索。因此这里刻意不调用文件系统存在性检查；实际字幕路径由下载
        worker 或改配服务在执行时再解析。
        """

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
        if raw_type in {HostMediaType.TV.value, "tv", "TV"}:
            media_type = MediaType.TV
            host_type = HostMediaType.TV
        elif raw_type in {HostMediaType.MOVIE.value, "movie", "MOVIE"}:
            media_type = MediaType.MOVIE
            host_type = HostMediaType.MOVIE
        else:
            media_type = MediaType.UNKNOWN
            host_type = HostMediaType.UNKNOWN
        raw_year = getattr(history, "year", None)
        if raw_year is not None:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                year = None
        else:
            year = None
        raw_tmdb_id = getattr(history, "tmdbid", None)
        if raw_tmdb_id is not None:
            try:
                tmdb_id = int(raw_tmdb_id)
            except (TypeError, ValueError):
                tmdb_id = None
        else:
            tmdb_id = None
        item_data = dict(file_data)
        item_data.update({"path": path_value, "storage": "local"})
        target_item = FileItem.model_validate(item_data)
        context = MediaContext(
            title=str(getattr(history, "title", "") or Path(path_value).stem),
            year=year,
            media_type=media_type,
            season=_parse_number(getattr(history, "seasons", None)),
            episode=_parse_number(getattr(history, "episodes", None)),
            tmdb_id=tmdb_id,
            imdb_id=getattr(history, "imdbid", None),
            target_path=path_value,
            target_file_name=str(file_data.get("name") or Path(path_value).name),
            target_storage="local",
        )
        host_fields: dict[str, Any] = {
            "type": host_type,
            "title": context.title,
        }
        if year is not None:
            host_fields["year"] = str(year)
        if context.season is not None:
            host_fields["season"] = context.season
        if context.tmdb_id is not None:
            host_fields["tmdb_id"] = context.tmdb_id
        if context.imdb_id is not None:
            host_fields["imdb_id"] = context.imdb_id
        host_mediainfo = HostMediaInfo(**host_fields)
        return SearchTarget(
            history_id=int(history.id),
            context=context,
            transferred_at=_parse_history_time(getattr(history, "date", None)),
            target_item=target_item,
            host_mediainfo=host_mediainfo,
        )

    async def _valid_targets(self) -> list[SearchTarget]:
        """过滤并按规范目标路径保留最新整理历史。"""

        targets = [await self._to_target(item) for item in await self._histories()]
        valid = sorted(
            (item for item in targets if item is not None),
            key=lambda item: item.transferred_at,
            reverse=True,
        )
        unique: dict[str, SearchTarget] = {}
        for item in valid:
            key = os.path.normcase(os.path.abspath(item.context.target_path))
            unique.setdefault(key, item)
        return list(unique.values())

    async def list_targets(
        self,
        page: int = 1,
        page_size: int = 25,
        search: str | None = None,
    ) -> SearchTargetPage:
        """分页返回按标题、文件名或完整路径筛选的目标。"""

        targets = await self._valid_targets()
        term = (search or "").strip().casefold()
        if term:
            targets = [
                item
                for item in targets
                if term in item.context.title.casefold()
                or term in item.context.target_file_name.casefold()
                or term in item.context.target_path.casefold()
            ]
        start = (page - 1) * page_size
        return SearchTargetPage(
            items=targets[start : start + page_size],
            total=len(targets),
            page=page,
            page_size=page_size,
        )

    async def list_all_targets(self) -> list[SearchTarget]:
        """返回用于批量改配精确建议的全部有效整理历史目标。"""

        return await self._valid_targets()

    async def get_target(self, history_id: int) -> SearchTarget | None:
        """按整理历史 ID 返回成功的本地单文件历史目标。

        这里返回的是历史快照，不保证历史目标文件当前仍存在；调用方在
        真正执行文件操作前负责检查解析后的目标目录。
        """

        if hasattr(self._history_oper, "async_get"):
            history = await self._history_oper.async_get(history_id)
        else:
            history = next(
                (item for item in await self._histories() if int(getattr(item, "id", -1)) == history_id),
                None,
            )
        return await self._to_target(history) if history is not None else None


async def _default_media_resolver(context: MediaContext) -> HostMediaInfo | None:
    """使用 MoviePilot 公共媒体能力按 TMDB ID 补充媒体信息。"""

    if context.tmdb_id is None:
        return None
    host_type = HostMediaType.TV if context.media_type is MediaType.TV else HostMediaType.MOVIE
    return await MediaChain().async_recognize_media(
        mtype=host_type,
        tmdbid=context.tmdb_id,
        cache=True,
    )


class ManualSearchService:
    """并发执行三源人工搜索并管理进程内短期会话。"""

    def __init__(
        self,
        targets: TargetQueryService,
        sources: dict[SubtitleSource, SubtitleSourcePort],
        matcher: MediaMatcherPort,
        cache: ManualSearchCachePort | None = None,
        media_resolver: Callable[[MediaContext], Awaitable[HostMediaInfo | None]] | None = None,
    ) -> None:
        """创建人工搜索服务。"""

        self._targets = targets
        self._sources = sources
        self._matcher = matcher
        self._cache = cache or AsyncMemoryBackend(
            cache_type="ttl",
            maxsize=256,
            ttl=SEARCH_SESSION_TTL_SECONDS,
        )
        self._media_resolver = media_resolver or _default_media_resolver

    @staticmethod
    def _session_key(session_id: str) -> str:
        """构造独立区域内的结构化搜索会话键。"""

        return json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":"))

    async def _enrich_target(self, target: SearchTarget) -> SearchTarget:
        """在英文标题缺失时尽力通过宿主媒体能力补充。"""

        if target.context.english_title or not (target.context.tmdb_id or target.context.imdb_id):
            return target
        try:
            mediainfo = await self._media_resolver(target.context)
        except Exception:  # noqa: BLE001 - 宿主媒体补充失败时必须降级查询
            logger.warning(f"人工字幕搜索无法为整理历史 {target.history_id} 补充英文标题，将跳过依赖英文标题的查询")
            return target
        english_title = str(getattr(mediainfo, "en_title", "") or "").strip()
        if not english_title:
            return target
        target.context = target.context.model_copy(
            update={
                "english_title": english_title,
                "original_title": getattr(mediainfo, "original_title", None),
            }
        )
        target.host_mediainfo = mediainfo
        return target

    @staticmethod
    def _source_context(run: ManualSourceSearchResult) -> str:
        """把人工来源的缓存、分页和查询信息转换为中文。"""

        details = dict(getattr(run, "details", {}) or {})
        parts: list[str] = []
        if details.get("cache_hit") is True:
            cached_at = details.get("cache_stored_at")
            parts.append(f"复用了缓存{f'（写入时间 {cached_at}）' if cached_at else ''}")
        elif details.get("cache_hit") is False:
            parts.append("本次实际请求了字幕站")
        page_count = details.get("page_count")
        if isinstance(page_count, int) and page_count > 0:
            parts.append(f"读取 {page_count} 页")
            if details.get("pagination_complete") is False:
                parts.append("分页未完整读取")
        query = str(details.get("query") or run.matched_query or "").strip()
        query_type = str(details.get("query_type") or "").strip()
        if query:
            parts.append(f"使用{QUERY_TYPE_NAMES.get(query_type, '查询词')}“{query}”")
        return "，".join(parts)

    @classmethod
    def _log_source_result(cls, history_id: int, run: ManualSourceSearchResult) -> None:
        """为一次人工来源搜索记录唯一的中文业务结论。"""

        name = SOURCE_NAMES[run.source]
        context = cls._source_context(run)
        suffix = f"；{context}" if context else ""
        raw_count = max(len(run.candidates), int(getattr(run, "raw_count", 0)))
        if run.status == "disabled":
            logger.info(f"人工字幕搜索未查询 {name}：该来源未启用")
            return
        if run.status == "unconfigured":
            reason = "没有启用且支持字幕搜索的站点" if run.skip_reason == "no_subtitle_sites" else "该来源配置不完整"
            logger.info(f"人工字幕搜索未查询 {name}：{reason}")
            return
        if run.status == "limited":
            logger.warning(f"人工字幕搜索查询 {name} 受限：{run.error_summary or '字幕源暂时限制请求'}{suffix}")
            return
        if run.status == "error":
            logger.warning(f"人工字幕搜索查询 {name} 失败：{run.error_summary or '字幕源请求异常'}{suffix}")
            return
        conclusion = "字幕站没有返回候选" if raw_count == 0 else f"字幕站返回 {raw_count} 个候选，全部保留供用户选择"
        duration = f"；耗时 {run.duration_ms} 毫秒" if run.duration_ms is not None else ""
        log = logger.warning if run.details.get("pagination_complete") is False else logger.info
        log(f"整理历史 {history_id} 的人工字幕搜索已完成 {name} 查询：{conclusion}{suffix}{duration}")

    async def search(
        self,
        history_id: int,
        custom_queries: dict[SubtitleSource | str, str | None] | None = None,
    ) -> ManualSearchResult:
        """并发搜索三个来源，有候选时创建三十分钟内存会话。"""

        target = await self._targets.get_target(history_id)
        if target is None:
            raise LookupError("目标整理历史不存在或已不可用")
        target = await self._enrich_target(target)
        values = custom_queries or {}
        coroutines = []
        ordered_sources = list(SubtitleSource)
        for source in ordered_sources:
            adapter = self._sources[source]
            custom = values.get(source, values.get(source.value))
            coroutines.append(adapter.manual_search(target.context, custom))
        raw_results = await asyncio.gather(*coroutines, return_exceptions=True)
        runs: list[ManualSourceSearchResult] = []
        for source, result in zip(ordered_sources, raw_results, strict=True):
            if isinstance(result, BaseException):
                runs.append(
                    ManualSourceSearchResult(
                        source=source,
                        status="error",
                        error_summary=f"{SOURCE_NAMES[source]} 人工搜索失败",
                    )
                )
            else:
                runs.append(result)
        for run in runs:
            self._log_source_result(history_id, run)
        recognitions_by_run: list[list[CandidateRecognition]] = []
        handles_by_run: list[list[CandidateHandle]] = []
        for run in runs:
            recognitions: list[CandidateRecognition] = []
            handles: list[CandidateHandle] = []
            for handle in run.candidates:
                try:
                    recognition = self._matcher.recognize_candidate(
                        handle.candidate,
                        target.context,
                        getattr(target, "host_mediainfo", None),
                    )
                except Exception:  # noqa: BLE001 - 单候选异常必须降级且保留会话
                    recognition = CandidateRecognition(
                        candidate=handle.candidate.model_copy(deep=True),
                        status=CandidateRecognitionStatus.UNRECOGNIZED,
                    )
                    logger.warning(f"人工字幕搜索候选 {handle.candidate.stable_key} 识别异常，已按未识别候选保留")
                recognitions.append(recognition)
                handles.append(CandidateHandle(candidate=recognition.candidate, opaque=handle.opaque))
            recognitions_by_run.append(recognitions)
            handles_by_run.append(handles)
        session_id = new_id() if any(run.candidates for run in runs) else None
        if session_id:
            candidates: dict[str, SessionCandidate] = {}
            for run, recognitions, handles in zip(
                runs,
                recognitions_by_run,
                handles_by_run,
                strict=True,
            ):
                for recognition, handle in zip(recognitions, handles, strict=True):
                    candidates.setdefault(
                        handle.candidate.stable_key,
                        SessionCandidate(
                            session_id=session_id,
                            target=target,
                            handle=handle,
                            recognition_status=recognition.status,
                            actual_query=run.matched_query,
                        ),
                    )
            session = ManualSearchSession(
                session_id=session_id,
                target=target,
                candidates=candidates,
            )
            await self._cache.set(
                self._session_key(session_id),
                session,
                ttl=SEARCH_SESSION_TTL_SECONDS,
                region=SEARCH_SESSION_REGION,
            )
        views = [
            ManualSourceView(
                source=run.source,
                status=run.status,
                candidates=recognitions,
                default_queries=run.default_queries,
                executed_queries=run.executed_queries,
                matched_query=run.matched_query,
                duration_ms=run.duration_ms,
                error_summary=run.error_summary,
                details=dict(run.details),
            )
            for run, recognitions in zip(runs, recognitions_by_run, strict=True)
        ]
        candidate_count = sum(len(item.candidates) for item in runs)
        if session_id:
            logger.info(
                f"整理历史 {history_id} 的人工字幕搜索完成，共返回 {candidate_count} 个候选，搜索会话为 {session_id}"
            )
        else:
            logger.warning(f"整理历史 {history_id} 的人工字幕搜索完成，但三个来源都没有返回可下载候选")
        return ManualSearchResult(session_id=session_id, target=target, sources=views)

    async def get_candidate(
        self,
        session_id: str,
        candidate_key: str,
    ) -> SessionCandidate | None:
        """从有效会话读取完整不透明候选句柄。"""

        session = await self._cache.get(
            self._session_key(session_id),
            region=SEARCH_SESSION_REGION,
        )
        if not isinstance(session, ManualSearchSession):
            return None
        return session.candidates.get(candidate_key)

    async def clear_sessions(self) -> None:
        """在插件重载或停止时清除全部人工搜索会话。"""

        await self._cache.clear(region=SEARCH_SESSION_REGION)
