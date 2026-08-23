"""可选目标查询与人工字幕搜索会话服务。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.chain.media import MediaChain
from app.core.cache import AsyncCache
from app.log import logger
from app.schemas.types import MediaType as HostMediaType

from ..attribution import CandidateRecognizer
from ..schemas.attribution import CandidateMatchContext
from ..schemas.base import new_id
from ..schemas.candidate import CandidateRecognition, CandidateRecognitionStatus, SubtitleCandidate
from ..schemas.http.search import ManualCandidateItem, ManualSourceResult, SearchPlanItem
from ..schemas.http.target import TargetListItem
from ..schemas.search import (
    ManualSearchResult,
    ManualSearchStatus,
    ManualSourceView,
    ManualSubmitResult,
    ManualSubmitStatus,
)
from ..schemas.source import (
    CandidateHandle,
    CandidatePoolQueryBatchResult,
    CandidatePoolStatus,
    SourceCandidatePoolResult,
    SubtitleSource,
)
from ..schemas.target import MediaType, SearchTarget, SubtitleTarget
from ..schemas.task import ManualEnqueueResult, TaskWorkItem
from ..source import CandidatePool
from ..target import TargetCatalog

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
class SessionCandidate:
    """搜索会话内可提交到下载队列的完整候选。"""

    session_id: str
    target: SearchTarget
    handle: CandidateHandle
    recognition_status: CandidateRecognitionStatus
    actual_query: str | None = None


@dataclass(slots=True)
class ManualSearchSession:
    """由宿主短期缓存保存的人工搜索会话。"""

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


class ManualTaskCoordinatorPort(Protocol):
    """人工搜索提交依赖的最小任务协调端口。"""

    async def enqueue_manual(self, item: TaskWorkItem) -> ManualEnqueueResult | None:
        """在协调器锁与持久化流程内入队人工候选。"""


@dataclass(frozen=True, slots=True)
class _MediaResolution:
    """宿主媒体补充在目标 adapter 内投影出的插件事实。"""

    context: SubtitleTarget
    match_context: CandidateMatchContext


def _build_match_context(context: SubtitleTarget, mediainfo: Any) -> CandidateMatchContext | None:
    """在搜索宿主 adapter 边界投影插件自有媒体归属事实。"""

    if mediainfo is None:
        return None
    aliases: list[str] = []
    for value in (
        getattr(mediainfo, "en_title", None),
        getattr(mediainfo, "original_title", None),
        *(getattr(mediainfo, "names", None) or []),
    ):
        if isinstance(value, str) and value.strip() and value.strip() not in aliases:
            aliases.append(value.strip())
    season_years = tuple(
        (str(season), str(year))
        for season, year in (getattr(mediainfo, "season_years", None) or {}).items()
        if year not in (None, "")
    )

    def _integer(value: object) -> int | None:
        try:
            return int(str(value)) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    raw_douban = getattr(mediainfo, "douban_id", None)
    return CandidateMatchContext(
        title=str(getattr(mediainfo, "title", None) or context.title),
        aliases=tuple(aliases),
        original_title=getattr(mediainfo, "original_title", None) or context.original_title,
        year=context.year,
        media_type=context.media_type,
        tmdb_id=context.tmdb_id,
        imdb_id=context.imdb_id,
        douban_id=str(raw_douban).strip() if raw_douban not in (None, "") else None,
        bangumi_id=_integer(getattr(mediainfo, "bangumi_id", None)),
        anilist_id=_integer(getattr(mediainfo, "anilist_id", None)),
        season_years=season_years,
    )


async def _default_media_resolver(context: SubtitleTarget) -> _MediaResolution | None:
    """使用 MoviePilot 公共媒体能力按 TMDB ID 补充媒体信息。"""

    if context.tmdb_id is None:
        return None
    host_type = HostMediaType.TV if context.media_type is MediaType.TV else HostMediaType.MOVIE
    mediainfo = await MediaChain().async_recognize_media(
        mtype=host_type,
        tmdbid=context.tmdb_id,
        cache=True,
    )
    if mediainfo is None:
        return None
    enriched = context.model_copy(
        update={
            "english_title": getattr(mediainfo, "en_title", None) or context.english_title,
            "original_title": getattr(mediainfo, "original_title", None) or context.original_title,
        }
    )
    match_context = _build_match_context(enriched, mediainfo)
    return _MediaResolution(context=enriched, match_context=match_context) if match_context is not None else None


class ManualSearchService:
    """读取共享来源候选池并管理人工搜索短期会话。"""

    def __init__(
        self,
        targets: TargetCatalog,
        candidate_pool: CandidatePool,
        matcher: CandidateRecognizer,
        cache: ManualSearchCachePort | None = None,
        media_resolver: Callable[[SubtitleTarget], Awaitable[_MediaResolution | None]] | None = None,
        coordinator: ManualTaskCoordinatorPort | None = None,
    ) -> None:
        """创建人工搜索服务。"""

        self._targets = targets
        self._candidate_pool = candidate_pool
        self._matcher = matcher
        self._cache = cache or AsyncCache(
            cache_type="ttl",
            maxsize=256,
            ttl=SEARCH_SESSION_TTL_SECONDS,
        )
        self._media_resolver = media_resolver or _default_media_resolver
        self._coordinator = coordinator

    @staticmethod
    def target_item(target: SearchTarget) -> TargetListItem:
        """按人工搜索规则投影整理历史目标与来源查询计划。"""

        context = target.context
        assrt_first = context.title.strip() if len(context.title.strip()) >= 3 else None
        assrt_second = next(
            (
                value.strip()
                for value in (context.english_title, context.original_title)
                if isinstance(value, str)
                and len(value.strip()) >= 3
                and value.strip().casefold() != context.title.casefold()
            ),
            None,
        )
        media_id = context.imdb_id or (str(context.tmdb_id) if context.tmdb_id else None)
        return TargetListItem(
            history_id=target.history_id,
            media_title=context.title,
            year=context.year,
            media_type=context.media_type,
            season=context.season,
            episode=context.episode,
            tmdb_id=context.tmdb_id,
            imdb_id=context.imdb_id,
            target_file_name=context.target_file_name,
            target_path=str(context.target_path),
            organized_at=target.transferred_at,
            search_plans={
                SubtitleSource.MOVIEPILOT: [
                    SearchPlanItem(kind="title", label="英文标题关键词（搜索时生成）", query=None, editable=False)
                ],
                SubtitleSource.OPENSUBTITLES: [
                    SearchPlanItem(kind="id", label="媒体 ID", query=media_id, editable=False),
                    SearchPlanItem(
                        kind="title",
                        label="英文标题" if context.english_title else "英文标题（搜索时补充）",
                        query=context.english_title,
                        editable=bool(context.english_title),
                    ),
                ],
                SubtitleSource.ASSRT: [
                    SearchPlanItem(kind="title", label="主标题", query=assrt_first, editable=True),
                    SearchPlanItem(kind="fallback", label="英文名/原名", query=assrt_second, editable=True),
                ],
            },
        )

    @staticmethod
    def source_item(run: ManualSourceView) -> ManualSourceResult:
        """按来源规则投影人工候选的安全显示字段。"""

        def candidate_item(recognition: CandidateRecognition) -> ManualCandidateItem:
            candidate = recognition.candidate
            allowed = {
                SubtitleSource.MOVIEPILOT: {"site_name", "description"},
                SubtitleSource.OPENSUBTITLES: {"release", "media_id"},
                SubtitleSource.ASSRT: {"videoname", "native_name"},
            }[candidate.source]
            details = {key: value for key, value in candidate.metadata.items() if key in allowed}
            if candidate.source is SubtitleSource.MOVIEPILOT:
                details["site_priority"] = candidate.site_priority
            elif candidate.source is SubtitleSource.OPENSUBTITLES:
                details["trusted"] = candidate.trusted
            else:
                details["revision"] = candidate.revision
            return ManualCandidateItem(
                candidate_key=candidate.stable_key,
                recognition_status=recognition.status,
                source=candidate.source,
                name=candidate.name,
                file_name=candidate.file_name,
                language=candidate.language or None,
                format=candidate.format or None,
                package_scope=candidate.package_scope,
                season=candidate.season,
                episode=candidate.episode,
                seasons=list(candidate.seasons),
                episodes=list(candidate.episodes),
                translation_type=candidate.translation_type,
                hearing_impaired=candidate.hearing_impaired,
                rating=candidate.score,
                votes=candidate.votes,
                downloads=candidate.download_count,
                uploaded_at=candidate.uploaded_at,
                query=run.matched_query,
                source_details=details,
            )

        plans: list[SearchPlanItem] = []
        for index, query in enumerate(run.default_queries):
            if run.source is SubtitleSource.MOVIEPILOT:
                kind, label, editable = "title", f"英文关键词 {index + 1}", True
            elif run.source is SubtitleSource.OPENSUBTITLES and query.startswith(("IMDb ID:", "TMDB ID:")):
                kind, label, editable = "id", "媒体 ID", False
            elif run.source is SubtitleSource.OPENSUBTITLES:
                kind, label, editable = "title", "英文标题", True
            elif index == 0:
                kind, label, editable = "title", "中文标题", True
            else:
                kind, label, editable = "fallback", "英文标题/原名", True
            plans.append(SearchPlanItem(kind=kind, label=label, query=query, editable=editable))
        return ManualSourceResult(
            source=run.source,
            status=run.status.value,
            default_plans=plans,
            executed_queries=run.executed_queries,
            matched_query=run.matched_query,
            candidate_count=len(run.candidates),
            duration_ms=run.duration_ms,
            error_summary=run.error_summary,
            details=dict(run.details),
            candidates=[candidate_item(item) for item in run.candidates],
        )

    @staticmethod
    def _session_key(session_id: str) -> str:
        """构造独立区域内的结构化搜索会话键。"""

        return json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":"))

    async def _enrich_target(self, target: SearchTarget) -> SearchTarget:
        """在英文标题缺失时尽力通过宿主媒体能力补充。"""

        if target.context.english_title or not (target.context.tmdb_id or target.context.imdb_id):
            return target
        try:
            resolution = await self._media_resolver(target.context)
        except Exception:  # noqa: BLE001 - 宿主媒体补充失败时必须降级查询
            logger.warning(f"人工字幕搜索无法为整理历史 {target.history_id} 补充英文标题，将跳过依赖英文标题的查询")
            return target
        if resolution is None:
            return target
        target.context = resolution.context
        target.match_context = resolution.match_context
        return target

    @staticmethod
    def _source_context(run: SourceCandidatePoolResult) -> str:
        """把人工来源的缓存、分页和查询信息转换为中文。"""

        details = dict(getattr(run, "details", {}) or {})
        parts: list[str] = []
        cache_items = [item for item in details.get("cache", []) if isinstance(item, Mapping)]
        cache_hit = details.get("cache_hit") is True or any(item.get("hit") is True for item in cache_items)
        cache_requested = details.get("cache_hit") is False or any(
            item.get("hit") is False or item.get("state") in {"miss", "invalid"} for item in cache_items
        )
        cached_at = details.get("cache_stored_at")
        if cache_hit:
            matched_query = run.matched_query or details.get("matched_query")
            selected = next(
                (item for item in cache_items if item.get("query") == matched_query and item.get("hit") is True),
                None,
            ) or next((item for item in cache_items if item.get("hit") is True), None)
            cached_at = (selected or {}).get("stored_at") or cached_at
            parts.append(f"复用了缓存{f'（写入时间 {cached_at}）' if cached_at else ''}")
        elif cache_requested:
            parts.append("本次实际请求了字幕站")
        page_items = [item for item in details.get("pagination", []) if isinstance(item, Mapping)]
        page_count = details.get("page_count")
        if page_items:
            page_count = sum(
                item["pages_fetched"]
                for item in page_items
                if isinstance(item.get("pages_fetched"), int) and item["pages_fetched"] > 0
            )
        pagination_incomplete = details.get("pagination_complete") is False or any(
            item.get("complete") is False for item in page_items
        )
        if isinstance(page_count, int) and page_count > 0:
            parts.append(f"读取 {page_count} 页")
            if pagination_incomplete:
                parts.append("分页未完整读取")
        query = str(details.get("query") or run.matched_query or details.get("matched_query") or "").strip()
        query_type = str(details.get("query_type") or "").strip()
        if query:
            parts.append(f"使用{QUERY_TYPE_NAMES.get(query_type, '查询词')}“{query}”")
        return "，".join(parts)

    @classmethod
    def _log_source_result(cls, history_id: int, run: SourceCandidatePoolResult) -> None:
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
        if run.status == "partial":
            logger.warning(
                f"人工字幕搜索查询 {name} 部分完成：{run.error_summary or '分页读取未完成'}，"
                f"已保留 {len(run.candidates)} 个可下载候选{suffix}"
            )
            return
        if run.status == "error":
            logger.warning(f"人工字幕搜索查询 {name} 失败：{run.error_summary or '字幕源请求异常'}{suffix}")
            return
        conclusion = "字幕站没有返回候选" if raw_count == 0 else f"字幕站返回 {raw_count} 个候选，全部保留供用户选择"
        duration = f"；耗时 {run.duration_ms} 毫秒" if run.duration_ms is not None else ""
        if run.details.get("pagination_complete") is False:
            logger.warning(
                f"整理历史 {history_id} 的人工字幕搜索 {name} 查询分页未完整读取："
                f"已保留 {len(run.candidates)} 个可下载候选{suffix}"
            )
            return
        logger.info(f"整理历史 {history_id} 的人工字幕搜索已完成 {name} 查询：{conclusion}{suffix}{duration}")

    @staticmethod
    def _public_status(status: CandidatePoolStatus) -> ManualSearchStatus:
        """把部分完成映射为现有人工 API 的受限语义。"""

        return ManualSearchStatus.LIMITED if status is CandidatePoolStatus.PARTIAL else ManualSearchStatus(status)

    async def search(
        self,
        history_id: int,
        custom_queries: dict[SubtitleSource | str, str | None] | None = None,
    ) -> ManualSearchResult:
        """批量查询三个来源，有候选时创建三十分钟短期会话。"""

        target = await self._targets.get_target(history_id)
        if target is None:
            raise LookupError("目标整理历史不存在或已不可用")
        target = await self._enrich_target(target)
        values = custom_queries or {}
        ordered_sources = list(SubtitleSource)
        normalized_queries = {source: values.get(source, values.get(source.value)) for source in ordered_sources}
        try:
            batch = await self._candidate_pool.query(target.context, normalized_queries)
        except Exception:  # noqa: BLE001 - 批量查询异常必须收敛为逐来源安全结果
            logger.warning(f"整理历史 {history_id} 的人工字幕搜索失败：共享来源候选池查询异常")
            batch = CandidatePoolQueryBatchResult(
                sources={
                    source: SourceCandidatePoolResult(
                        source=source,
                        status=CandidatePoolStatus.ERROR,
                        error_summary=f"{SOURCE_NAMES[source]} 人工搜索失败",
                    )
                    for source in ordered_sources
                }
            )
        runs = [
            batch.sources.get(
                source,
                SourceCandidatePoolResult(
                    source=source,
                    status=CandidatePoolStatus.ERROR,
                    error_summary=f"{SOURCE_NAMES[source]}候选池运行结果缺失",
                ),
            )
            for source in ordered_sources
        ]
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
                        target.match_context,
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
                status=self._public_status(run.status),
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

    async def submit(self, session_id: str, candidate_key: str) -> ManualSubmitResult:
        """校验人工搜索会话并在协调器内提交用户选定候选。"""

        session = await self._cache.get(
            self._session_key(session_id),
            region=SEARCH_SESSION_REGION,
        )
        if not isinstance(session, ManualSearchSession) or session.session_id != session_id:
            return ManualSubmitResult(status=ManualSubmitStatus.SESSION_NOT_FOUND)
        if not isinstance(session.candidates, dict) or not isinstance(candidate_key, str) or not candidate_key:
            return ManualSubmitResult(status=ManualSubmitStatus.CANDIDATE_NOT_FOUND)
        candidate = session.candidates.get(candidate_key)
        if (
            not isinstance(candidate, SessionCandidate)
            or candidate.session_id != session_id
            or not isinstance(getattr(candidate.target, "context", None), SubtitleTarget)
            or not isinstance(candidate.handle, CandidateHandle)
            or not isinstance(candidate.handle.candidate, SubtitleCandidate)
            or candidate.handle.candidate.stable_key != candidate_key
            or candidate.handle.opaque is None
        ):
            return ManualSubmitResult(status=ManualSubmitStatus.CANDIDATE_NOT_FOUND)
        if self._coordinator is None:
            logger.warning("人工字幕候选提交失败：任务协调器当前不可用")
            return ManualSubmitResult(status=ManualSubmitStatus.REJECTED)
        result = await self._coordinator.enqueue_manual(
            TaskWorkItem(
                context=candidate.target.context,
                match_context=candidate.target.match_context,
                target_history_id=candidate.target.history_id,
                manual_handle=candidate.handle,
                manual_session_id=session_id,
                actual_search_query=candidate.actual_query,
            )
        )
        if result is None:
            return ManualSubmitResult(status=ManualSubmitStatus.REJECTED)
        return ManualSubmitResult(status=ManualSubmitStatus.SUCCESS, task=result.task, reused=result.reused)

    async def clear_sessions(self) -> None:
        """按显式管理请求清除人工搜索会话缓存区域。"""

        await self._cache.clear(region=SEARCH_SESSION_REGION)
