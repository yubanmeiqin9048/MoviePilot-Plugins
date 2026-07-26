"""字幕助手串行任务编排服务。"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from anyio import Path as AsyncPath
from pydantic import ValidationError

from app.core.config import settings
from app.log import logger

from ..domain.enums import (
    AI_ATTRIBUTION_EVIDENCE_CODES,
    AiAttributionConfidence,
    AiAttributionOutcome,
    AttemptResult,
    AttributionEvidence,
    FileAttributionMethod,
    FileLocation,
    MediaType,
    PackageAttributionStrategy,
    RecordStatus,
    SourceHealth,
    SourceRunStatus,
    SubtitleSource,
    TaskStage,
    TaskStatus,
    TaskTrigger,
    UnmatchedReason,
)
from ..domain.models import (
    AiAttributionAudit,
    CandidateAttempt,
    CandidateAttributionSnapshot,
    FileAttributionEvidence,
    MatchRecord,
    MediaContext,
    PathMappingSnapshot,
    SourceRun,
    SourceStatus,
    StageTrace,
    SubtitleCandidate,
    SubtitleTask,
    elapsed_ms,
    utc_now,
)
from ..domain.ranking import candidate_rank
from ..infrastructure.ai_attribution import (
    AiAttributionAdapter,
    AiAttributionInput,
)
from ..sources.common import SourceLimitedError, SourceRequestError
from .config import PluginConfig
from .inventory import InventoryConsumeResult, SubtitleInventory
from .path_mapping import resolve_path
from .ports import (
    CandidateHandle,
    ExtractedSubtitle,
    MediaMatcherPort,
    SourceSearchResult,
    SubtitleSourcePort,
)

SOURCE_NAMES = {
    SubtitleSource.MOVIEPILOT: "MoviePilot 站点字幕源",
    SubtitleSource.OPENSUBTITLES: "OpenSubtitles",
    SubtitleSource.ASSRT: "ASSRT",
}

TRIGGER_NAMES = {
    TaskTrigger.TRANSFER_EVENT: "媒体整理事件",
    TaskTrigger.MANUAL_CANDIDATE: "人工选择字幕",
}

STAGE_NAMES = {
    TaskStage.PREFLIGHT: "前置检查",
    TaskStage.INVENTORY: "字幕库存查询",
    TaskStage.SEARCH: "字幕源搜索",
    TaskStage.DOWNLOAD: "候选下载",
    TaskStage.EXTRACT: "下载结果解包",
    TaskStage.MATCH: "字幕匹配",
    TaskStage.AI_ATTRIBUTION: "AI 智能接管",
    TaskStage.WRITE: "字幕落盘",
}

SOURCE_STATUS_REASONS = {
    SourceRunStatus.DISABLED: "该来源未启用",
    SourceRunStatus.UNCONFIGURED: "该来源配置不完整",
}

SOURCE_SKIP_REASONS = {
    "english_title_missing": "缺少英文标题",
    "yake_unavailable": "英文关键词提取组件不可用",
    "keyword_extraction_failed": "英文关键词提取失败",
    "keyword_extraction_empty": "没有提取到可用的英文关键词",
    "no_subtitle_sites": "没有启用且支持字幕搜索的站点",
}

REJECTION_REASON_NAMES = {
    "language": "语言不符合自动规则",
    "translation": "翻译类型不符合自动规则",
    "machine_translation": "机器翻译不符合自动规则",
    "foreign_parts_only": "仅外语对白字幕不符合自动规则",
    "download_locator": "缺少下载定位",
    "admission": "未通过来源基础规则",
    "duplicate": "重复候选",
    "query_unavailable": "缺少可用查询词",
    "media_or_episode_mismatch": "与当前媒体或季集不匹配",
}

QUERY_TYPE_NAMES = {
    "media_id": "媒体 ID",
    "english_title": "英文标题",
    "custom": "自定义关键词",
    "keyword": "英文标题关键词",
}

ATTEMPT_RESULT_NAMES = {
    AttemptResult.SUCCESS: "处理成功",
    AttemptResult.DOWNLOAD_FAILED: "下载失败",
    AttemptResult.EXTRACT_FAILED: "解包失败",
    AttemptResult.NO_MATCH: "没有匹配到当前目标字幕",
    AttemptResult.WRITE_FAILED: "落盘失败",
    AttemptResult.INTERRUPTED: "处理已中断",
}

AI_TAKEOVER_TRIGGER_REASONS = frozenset(
    {
        "media_unrecognized",
        "identity_missing",
        "season_ambiguous",
        "episode_ambiguous",
    }
)
"""应用层允许适配器请求 AI 接管的稳定触发原因。"""


@dataclass(slots=True)
class TaskWorkItem:
    """运行期任务项及不持久化的宿主对象。"""

    context: MediaContext
    target: Any
    host_mediainfo: Any
    target_history_id: int | None = None
    manual_handle: CandidateHandle | None = None
    manual_session_id: str | None = None
    actual_search_query: str | None = None
    task_id: str | None = None


@dataclass(slots=True)
class AttributedSubtitle:
    """运行期物理字幕文件及其持久化归属证据。"""

    extracted: ExtractedSubtitle
    evidence: FileAttributionEvidence


class _AiTakeoverMetrics(TypedDict, total=False):
    """AI 接管返回的可选聚合指标。"""

    attempt_count: int
    accepted_count: int
    rejected_count: int
    error_count: int
    over_limit_count: int
    reason_summary: dict[str, int]


class _CandidateFileMetrics(TypedDict):
    """一次候选文件归属处理的完整计数指标。"""

    extracted_count: int
    current_target_count: int
    same_media_other_episode_count: int
    ambiguous_count: int
    other_media_count: int
    ai_attempt_count: int
    ai_accepted_count: int
    ai_rejected_count: int
    ai_error_count: int
    ai_over_limit_count: int
    ai_reason_summary: dict[str, int]


def build_media_context(target: Any, meta: Any, mediainfo: Any) -> MediaContext | None:
    """从整理事件对象构建安全媒体上下文，不重新解析事件字典。"""

    path_value = getattr(target, "path", None)
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    target_name = str(getattr(target, "name", None) or Path(path_value).name)
    media_title = str(
        getattr(mediainfo, "title", None)
        or getattr(meta, "name", None)
        or getattr(meta, "cn_name", None)
        or getattr(meta, "en_name", None)
        or Path(path_value).stem
    ).strip()
    media_type_value = getattr(getattr(mediainfo, "type", None), "name", None) or str(
        getattr(getattr(mediainfo, "type", None), "value", "")
    )
    media_type = MediaType.TV if media_type_value.upper() in {"TV", "电视剧"} else MediaType.MOVIE
    year_value = getattr(mediainfo, "year", None) or getattr(meta, "year", None)
    try:
        year = int(year_value) if year_value not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    season = getattr(mediainfo, "season", None) or getattr(meta, "begin_season", None)
    episode = getattr(meta, "begin_episode", None)
    try:
        season = int(season) if season is not None else None
    except (TypeError, ValueError):
        season = None
    try:
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        episode = None
    tmdb_id = getattr(mediainfo, "tmdb_id", None) or getattr(meta, "tmdbid", None)
    try:
        tmdb_id = int(tmdb_id) if tmdb_id not in (None, "") else None
    except (TypeError, ValueError):
        tmdb_id = None
    return MediaContext(
        title=media_title,
        original_title=getattr(mediainfo, "original_title", None),
        english_title=getattr(mediainfo, "en_title", None),
        year=year,
        media_type=media_type,
        season=season,
        episode=episode,
        tmdb_id=tmdb_id,
        imdb_id=getattr(mediainfo, "imdb_id", None),
        target_path=path_value,
        target_file_name=target_name,
        target_storage=getattr(target, "storage", None),
    )


class TaskCoordinator:
    """维护单 worker 队列并执行字幕搜索、匹配与落盘。"""

    def __init__(
        self,
        store: Any,
        filesystem: Any,
        archive: Any,
        matcher: MediaMatcherPort,
        sources: Mapping[SubtitleSource, SubtitleSourcePort],
        config: PluginConfig,
        inventory: SubtitleInventory,
        ai_adapter: Any | None = None,
    ) -> None:
        """创建可注入依赖的任务协调器。"""

        self.store = store
        self.filesystem = filesystem
        self.archive = archive
        self.matcher = matcher
        self.sources = sources
        self.config = config
        self.inventory = inventory
        # 适配器不保存任务结果；由插件入口注入并在每个批次实时检查授权。
        if ai_adapter is not None:
            self.ai_adapter = ai_adapter
        else:
            self.ai_adapter = AiAttributionAdapter(lambda: self.config)
        self._queue: asyncio.Queue[TaskWorkItem] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._active_paths: dict[str, str] = {}
        self._active_items: dict[str, TaskWorkItem] = {}
        self._active_manual: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._accepting = True
        self._generation = 0

    def _path_key(self, path: str) -> str:
        """生成同一路径任务合并键。"""

        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _task_label(task: SubtitleTask) -> str:
        """返回适合人读日志的任务关联说明。"""

        return f"任务 {task.id}（{TRIGGER_NAMES[task.trigger]}）"

    @staticmethod
    def _candidate_label(candidate: SubtitleCandidate) -> str:
        """返回不暴露下载定位的候选说明。"""

        return f"{SOURCE_NAMES[candidate.source]} 候选“{candidate.name}”"

    @staticmethod
    def _rejection_summary(summary: dict[str, int]) -> str:
        """把自动规则排除汇总转换为中文说明。"""

        parts = [
            f"{REJECTION_REASON_NAMES.get(reason, reason)} {count} 个" for reason, count in summary.items() if count > 0
        ]
        return "、".join(parts) or "无"

    @staticmethod
    def _source_query_summary(details: dict[str, Any]) -> str:
        """返回来源缓存、分页和命中查询的中文说明。"""

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
        query = str(details.get("query") or "").strip()
        query_type = str(details.get("query_type") or "").strip()
        if query:
            query_name = QUERY_TYPE_NAMES.get(query_type, "查询词")
            parts.append(f"命中{query_name}“{query}”")
        return "，".join(parts)

    def _ensure_worker(self) -> None:
        """在当前事件循环中懒启动唯一 worker。"""

        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def enqueue(self, item: TaskWorkItem) -> str | None:
        """创建或合并一个运行期字幕任务。"""

        if not self._accepting:
            return None
        key = self._path_key(item.context.target_path)
        async with self._lock:
            if key in self._active_paths:
                return self._active_paths[key]
            task = SubtitleTask(
                media_title=item.context.title,
                year=item.context.year,
                media_type=item.context.media_type,
                season=item.context.season,
                episode=item.context.episode,
                tmdb_id=item.context.tmdb_id,
                imdb_id=item.context.imdb_id,
                target_file_name=item.context.target_file_name,
                target_path=item.context.target_path,
                target_history_id=item.target_history_id,
                history_target_path=item.context.target_path,
                target_storage=item.context.target_storage,
                package_attribution_strategy=self.config.package_attribution_strategy,
            )
            await self.store.save_task(task)
            logger.info(f"{self._task_label(task)}已创建，目标文件为“{task.target_path}”")
            item.task_id = task.id
            self._active_paths[key] = task.id
            self._active_items[task.id] = item
            await self._queue.put(item)
            self._ensure_worker()
            return task.id

    async def enqueue_manual(self, item: TaskWorkItem) -> tuple[str | None, bool]:
        """把用户选定候选提交到单 worker，并合并同会话同候选的非终态任务。"""

        if not self._accepting or item.manual_handle is None or not item.manual_session_id:
            return None, False
        candidate = item.manual_handle.candidate
        manual_key = f"{item.manual_session_id}:{candidate.source.value}:{candidate.stable_key}"
        async with self._lock:
            existing_id = self._active_manual.get(manual_key)
            if existing_id:
                return existing_id, True
            task = SubtitleTask(
                trigger=TaskTrigger.MANUAL_CANDIDATE,
                media_title=item.context.title,
                year=item.context.year,
                media_type=item.context.media_type,
                season=item.context.season,
                episode=item.context.episode,
                tmdb_id=item.context.tmdb_id,
                imdb_id=item.context.imdb_id,
                target_file_name=item.context.target_file_name,
                target_path=item.context.target_path,
                target_history_id=item.target_history_id,
                history_target_path=item.context.target_path,
                target_storage=item.context.target_storage,
                package_attribution_strategy=self.config.package_attribution_strategy,
                manual_source=candidate.source,
                manual_candidate_key=candidate.stable_key,
                manual_candidate_summary=candidate.model_dump(mode="json"),
                actual_search_query=item.actual_search_query,
            )
            await self.store.save_task(task)
            logger.info(
                f"{self._task_label(task)}已创建，将下载{self._candidate_label(candidate)}，"
                f"目标文件为“{task.target_path}”"
            )
            item.task_id = task.id
            self._active_manual[manual_key] = task.id
            self._active_items[task.id] = item
            await self._queue.put(item)
            self._ensure_worker()
            return task.id, False

    async def _worker_loop(self) -> None:
        """串行消费运行期队列。"""

        while self._accepting:
            item = await self._queue.get()
            try:
                task = await self.store.get_task(item.task_id) if item.task_id else None
                if task is not None:
                    await self._process(task, item)
                else:
                    logger.error(
                        f"字幕任务 {item.task_id or '未知'} 无法开始处理：持久化记录不存在，"
                        f"触发方式为“{'人工选择字幕' if item.manual_handle else '媒体整理事件'}”"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 必须隔离单项任务异常
                logger.error(
                    f"字幕任务 {item.task_id or '未知'} 的 worker 发生未处理异常："
                    f"{type(exc).__name__}；插件调用栈：{self._safe_traceback()}"
                )
            finally:
                self._queue.task_done()
                key = self._path_key(item.context.target_path)
                async with self._lock:
                    task_id = self._active_paths.pop(key, None)
                    if task_id:
                        self._active_items.pop(task_id, None)
                    if item.manual_handle is not None and item.manual_session_id:
                        manual_key = (
                            f"{item.manual_session_id}:{item.manual_handle.candidate.source.value}:"
                            f"{item.manual_handle.candidate.stable_key}"
                        )
                        manual_task_id = self._active_manual.pop(manual_key, None)
                        if manual_task_id:
                            self._active_items.pop(manual_task_id, None)

    async def _task_for_path(self, path_key: str) -> SubtitleTask | None:
        """按运行期路径键读取当前任务。"""

        async with self._lock:
            task_id = self._active_paths.get(path_key)
        if task_id is None:
            return None
        return await self.store.get_task(task_id)

    async def _save(self, task: SubtitleTask) -> None:
        """持久化任务快照。"""

        await self.store.save_task(task)

    @staticmethod
    def _safe_traceback(exc: BaseException | None = None) -> str:
        """返回适合单行日志的精简插件调用栈。"""

        frames = traceback.extract_tb(exc.__traceback__ if exc is not None else sys.exc_info()[2], limit=8)
        return " | ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)

    async def _set_stage(self, task: SubtitleTask, stage: TaskStage) -> None:
        """结束上一阶段并开始新的阶段轨迹。"""

        now = utc_now()
        if task.stage_traces and task.stage_traces[-1].finished_at is None:
            previous = task.stage_traces[-1]
            previous.finished_at = now
            previous.duration_ms = elapsed_ms(previous.started_at, now)
        task.stage = stage
        task.stage_traces.append(StageTrace(stage=stage, started_at=now))
        await self._save(task)
        logger.debug(f"{self._task_label(task)}进入“{STAGE_NAMES[stage]}”阶段")

    async def _finish_stage(self, task: SubtitleTask, summary: str | None = None) -> None:
        """结束当前阶段轨迹并记录摘要。"""

        if task.stage_traces and task.stage_traces[-1].finished_at is None:
            trace = task.stage_traces[-1]
            trace.finished_at = utc_now()
            trace.duration_ms = elapsed_ms(trace.started_at, trace.finished_at)
            trace.summary = summary

    async def _finish_task(
        self,
        task: SubtitleTask,
        status: TaskStatus,
        reason_code: str,
        reason_message: str,
    ) -> None:
        """写入任务终态、原因和耗时。"""

        await self._finish_stage(task, reason_message)
        now = utc_now()
        task.status = status
        task.stage = None
        task.reason_code = reason_code
        task.reason_message = reason_message
        task.finished_at = now
        task.duration_ms = elapsed_ms(task.started_at or task.created_at, now)
        await self._save(task)
        log = logger.warning if status is TaskStatus.FAILED else logger.info
        status_name = {
            TaskStatus.SUCCESS: "成功",
            TaskStatus.SKIPPED: "已跳过",
            TaskStatus.FAILED: "失败",
            TaskStatus.INTERRUPTED: "已中断",
        }.get(status, status.value)
        log(f"{self._task_label(task)}处理{status_name}：{reason_message}")

    async def _preflight(self, task: SubtitleTask, item: TaskWorkItem) -> bool:
        """完成本地文件、扩展名和已有字幕前置检查。"""

        target = item.target
        if getattr(target, "storage", None) != "local":
            await self._finish_task(task, TaskStatus.SKIPPED, "non_local_storage", "目标不是本地存储")
            return False
        if getattr(target, "type", None) != "file":
            await self._finish_task(task, TaskStatus.SKIPPED, "unsupported_media_container", "目标不是文件型媒体")
            return False
        extension = str(getattr(target, "extension", "") or "").lower()
        if f".{extension}" not in {str(item).lower() for item in settings.RMT_MEDIAEXT}:
            await self._finish_task(
                task, TaskStatus.SKIPPED, "unsupported_media_format", "目标格式不在宿主媒体格式集合中"
            )
            return False
        task.target_file_exists = await AsyncPath(task.target_path).is_file()
        await self._save(task)
        if not task.target_file_exists:
            await self._finish_task(task, TaskStatus.FAILED, "target_missing", "整理目标文件不存在")
            return False
        subtitle = await self.filesystem.has_standard_subtitle(Path(task.target_path))
        task.existing_subtitle_check = {"found": bool(subtitle), "path": str(subtitle) if subtitle else None}
        await self._save(task)
        if subtitle:
            await self._finish_task(task, TaskStatus.SKIPPED, "existing_standard_subtitle", "目标已有标准简中外挂字幕")
            return False
        if task.media_type is MediaType.TV and task.episode is not None and task.season is None:
            await self._finish_task(
                task,
                TaskStatus.FAILED,
                "season_missing",
                "电视剧目标已有集号但缺少季号，无法安全查询字幕库存或匹配字幕，未搜索字幕源",
            )
            return False
        return True

    async def _prepare_manual_target(self, task: SubtitleTask, item: TaskWorkItem) -> None:
        """在人工下载执行时按当前配置解析并固化实际字幕目标。"""

        history_path = task.history_target_path or item.context.target_path
        resolution = resolve_path(history_path, self.config.path_mappings)
        task.history_target_path = resolution.original_path
        task.target_path = resolution.resolved_path
        task.matched_path_mapping = (
            PathMappingSnapshot(**resolution.mapping.as_dict()) if resolution.mapping is not None else None
        )
        task.target_file_exists = await AsyncPath(task.target_path).is_file()
        item.context = item.context.model_copy(
            update={
                "target_path": task.target_path,
                "target_file_name": Path(task.target_path).name,
            }
        )
        await self._save(task)
        if resolution.mapping is not None:
            logger.info(
                f"{self._task_label(task)}执行人工下载时应用整理历史路径映射："
                f"历史路径为“{resolution.original_path}”，实际路径为“{resolution.resolved_path}”"
            )

    async def _search_sources(self, task: SubtitleTask, item: TaskWorkItem) -> list[CandidateHandle]:
        """并发搜索三个来源并汇总宿主规则确认后的候选。"""

        await self._set_stage(task, TaskStage.SEARCH)
        enabled: list[SubtitleSource] = []
        enabled_config = self.config.enabled_sources()
        for source in SubtitleSource:
            adapter = self.sources.get(source)
            if not enabled_config[source]:
                status = SourceRunStatus.DISABLED
            elif adapter is None or not getattr(adapter, "configured", True):
                status = SourceRunStatus.UNCONFIGURED
            else:
                enabled.append(source)
                continue
            task.source_runs.append(SourceRun(source=source, status=status))
            logger.info(f"{self._task_label(task)}未查询 {SOURCE_NAMES[source]}：{SOURCE_STATUS_REASONS[status]}")
        if not enabled:
            await self._save(task)
            logger.warning(f"{self._task_label(task)}没有可调用的字幕源，因此未获得当前目标的候选")
            return []
        results = await asyncio.gather(
            *(self.sources[source].search(item.context, self.config.allow_machine_translation) for source in enabled),
            return_exceptions=True,
        )
        handles: list[CandidateHandle] = []
        for source, result in zip(enabled, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                run = SourceRun(
                    source=source,
                    status=SourceRunStatus.ERROR,
                    error_summary=f"{SOURCE_NAMES[source]} 搜索发生内部异常",
                )
                await self._save_source_failure(source, run.error_summary)
                task.source_runs.append(run)
                logger.error(
                    f"{self._task_label(task)}查询{SOURCE_NAMES[source]}时发生非预期异常："
                    f"{type(result).__name__}；插件调用栈：{self._safe_traceback(result)}"
                )
                continue
            assert isinstance(result, SourceSearchResult)
            raw_count = max(len(result.candidates), int(getattr(result, "raw_count", 0)))
            admitted_count = max(len(result.candidates), int(getattr(result, "admitted_count", 0)))
            rejection_summary = dict(getattr(result, "rejection_summary", {}) or {})
            if result.skip_reason == "no_subtitle_sites":
                run_status = SourceRunStatus.UNCONFIGURED
            elif result.limited:
                run_status = SourceRunStatus.LIMITED
            elif result.error_summary:
                run_status = SourceRunStatus.ERROR
            elif result.candidates:
                run_status = SourceRunStatus.SUCCESS
            else:
                run_status = SourceRunStatus.EMPTY
            if result.skip_reason == "no_subtitle_sites":
                await self._save_source_unavailable(
                    source,
                    SOURCE_SKIP_REASONS["no_subtitle_sites"],
                    result.duration_ms,
                )
            elif result.error_summary:
                await self._save_source_failure(
                    source,
                    result.error_summary,
                    result.limited,
                    result.duration_ms,
                )
            elif result.skip_reason is None:
                await self._save_source_success(source, result.details, result.duration_ms)
            media_matched_count = 0
            for handle in result.candidates:
                normalized = self.matcher.normalize_candidate(handle.candidate, item.context, item.host_mediainfo)
                if normalized is not None:
                    handles.append(CandidateHandle(candidate=normalized, opaque=handle.opaque))
                    media_matched_count += 1
            media_rejected = admitted_count - media_matched_count
            if media_rejected:
                rejection_summary["media_or_episode_mismatch"] = (
                    rejection_summary.get("media_or_episode_mismatch", 0) + media_rejected
                )
            rejected_count = max(0, raw_count - media_matched_count)
            if run_status in {SourceRunStatus.SUCCESS, SourceRunStatus.EMPTY}:
                if raw_count > 0 and media_matched_count == 0:
                    run_status = SourceRunStatus.FILTERED
                elif media_matched_count > 0:
                    run_status = SourceRunStatus.SUCCESS
                else:
                    run_status = SourceRunStatus.EMPTY
            task.source_runs.append(
                SourceRun(
                    source=source,
                    status=run_status,
                    candidate_count=media_matched_count,
                    raw_count=raw_count,
                    admitted_count=admitted_count,
                    media_matched_count=media_matched_count,
                    rejected_count=rejected_count,
                    rejection_summary=rejection_summary,
                    duration_ms=result.duration_ms,
                    error_summary=result.error_summary,
                    details=result.details,
                )
            )
            log = (
                logger.warning
                if run_status in {SourceRunStatus.ERROR, SourceRunStatus.LIMITED}
                or result.details.get("pagination_complete") is False
                else logger.info
            )
            query_summary = self._source_query_summary(result.details)
            prefix = f"{self._task_label(task)}的 {SOURCE_NAMES[source]} 搜索"
            if run_status is SourceRunStatus.UNCONFIGURED:
                conclusion = f"未执行：{SOURCE_SKIP_REASONS.get(result.skip_reason or '', '来源当前不可调用')}"
            elif result.skip_reason:
                conclusion = f"未执行：{SOURCE_SKIP_REASONS.get(result.skip_reason, '缺少可用查询条件')}"
            elif run_status is SourceRunStatus.ERROR:
                conclusion = (
                    f"失败：{result.error_summary or '字幕源请求异常'}；"
                    f"字幕站已返回 {raw_count} 个候选，自动规则保留 {admitted_count} 个，"
                    f"其中 {media_matched_count} 个适用于当前目标"
                )
            elif run_status is SourceRunStatus.LIMITED:
                conclusion = (
                    f"受限：{result.error_summary or '字幕源暂时限制请求'}；"
                    f"字幕站已返回 {raw_count} 个候选，自动规则保留 {admitted_count} 个，"
                    f"其中 {media_matched_count} 个适用于当前目标"
                )
            elif result.details.get("pagination_complete") is False:
                conclusion = (
                    f"部分完成：当前已取得 {raw_count} 个候选，自动规则保留 {admitted_count} 个，"
                    f"其中 {media_matched_count} 个适用于当前目标"
                )
            elif raw_count == 0:
                conclusion = "完成：字幕站没有返回候选"
            elif media_matched_count == 0:
                conclusion = (
                    f"完成：字幕站返回 {raw_count} 个候选，自动规则保留 {admitted_count} 个，但没有适用于当前目标的候选"
                )
            else:
                conclusion = (
                    f"完成：字幕站返回 {raw_count} 个候选，自动规则保留 {admitted_count} 个，"
                    f"其中 {media_matched_count} 个适用于当前目标"
                )
            context_summary = f"；{query_summary}" if query_summary else ""
            rejection_text = self._rejection_summary(rejection_summary)
            rejection_part = (
                f"；自动规则排除：{rejection_text}" if result.skip_reason is None and rejection_text != "无" else ""
            )
            duration_part = f"；耗时 {result.duration_ms} 毫秒" if result.duration_ms is not None else ""
            log(f"{prefix}{conclusion}{context_summary}{rejection_part}{duration_part}")
        await self._save(task)
        if handles:
            logger.info(
                f"{self._task_label(task)}已汇总 {len(enabled)} 个已启用来源的处理结果，"
                f"共获得 {len(handles)} 个适用于当前目标的候选"
            )
        else:
            logger.warning(
                f"{self._task_label(task)}已汇总 {len(enabled)} 个已启用来源的处理结果，但没有获得适用于当前目标的候选"
            )
        return handles

    async def _save_source_success(
        self,
        source: SubtitleSource,
        details: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源成功搜索的非敏感状态。"""

        existing = {item.source: item for item in await self.store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        adapter = self.sources[source]
        status.enabled = bool(getattr(adapter, "enabled", False))
        status.configured = bool(getattr(adapter, "configured", True))
        status.health = SourceHealth.HEALTHY
        status.last_checked_at = utc_now()
        status.last_success_at = status.last_checked_at
        status.last_duration_ms = duration_ms
        status.details = {**status.details, **getattr(adapter, "runtime_details", dict)(), **(details or {})}
        await self.store.save_source_status(status)

    async def _save_source_failure(
        self,
        source: SubtitleSource,
        summary: str | None,
        limited: bool = False,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源失败或限流的脱敏状态。"""

        existing = {item.source: item for item in await self.store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        adapter = self.sources[source]
        status.enabled = bool(getattr(adapter, "enabled", False))
        status.configured = bool(getattr(adapter, "configured", True))
        status.health = SourceHealth.LIMITED if limited else SourceHealth.ERROR
        status.last_checked_at = utc_now()
        status.last_error_at = status.last_checked_at
        status.last_error_summary = summary
        status.last_duration_ms = duration_ms
        await self.store.save_source_status(status)

    async def _save_source_unavailable(
        self,
        source: SubtitleSource,
        summary: str,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源当前不可调用且未发出搜索请求的状态。"""

        existing = {item.source: item for item in await self.store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        adapter = self.sources[source]
        status.enabled = bool(getattr(adapter, "enabled", False))
        status.configured = False
        status.health = SourceHealth.DISABLED
        status.last_checked_at = utc_now()
        status.last_error_at = status.last_checked_at
        status.last_error_summary = summary
        status.last_duration_ms = duration_ms
        status.details = {
            **status.details,
            **getattr(adapter, "runtime_details", dict)(),
        }
        await self.store.save_source_status(status)

    async def _consume_inventory(self, task: SubtitleTask, item: TaskWorkItem) -> InventoryConsumeResult:
        """在外部搜索前查询并消费精确库存字幕。"""

        await self._set_stage(task, TaskStage.INVENTORY)
        result = await self.inventory.consume(
            item.context,
            task.id,
            target_history_id=task.target_history_id,
            history_target_path=task.history_target_path,
            matched_path_mapping=task.matched_path_mapping,
            target_file_exists=task.target_file_exists,
        )
        task.inventory_result = {
            "matched": result.matched,
            "record_id": result.record.id if result.record else None,
            "warning": result.warning,
        }
        if result.warning:
            task.warning_count += 1
            task.warning_summaries.append(result.warning)
        await self._save(task)
        if result.matched and result.record is not None:
            logger.info(
                f"{self._task_label(task)}命中字幕库存记录 {result.record.id}，"
                f"字幕已写入“{result.record.final_subtitle_path or result.record.path}”"
            )
        elif result.warning:
            logger.warning(f"{self._task_label(task)}查询字幕库存时出现警告：{result.warning}")
        else:
            logger.info(f"{self._task_label(task)}没有找到对应的暂存字幕，将继续查询字幕源")
        return result

    async def _run_ai_takeover(
        self,
        task: SubtitleTask,
        item: TaskWorkItem,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        attributed: list[AttributedSubtitle],
    ) -> _AiTakeoverMetrics:
        """对常规归属后的模糊字幕执行可选 AI 接管并返回安全聚合。"""

        adapter = self.ai_adapter
        if adapter is None:
            return {}
        should = getattr(adapter, "should_takeover", None)
        authorized = getattr(adapter, "authorized", None)
        # 应用层也必须闭合适配器协议；缺少触发器或授权检查时拒绝调用，
        # 防止异常替身把明确归属项或未授权请求送入模型。
        if not callable(should) or not callable(authorized):
            return {}
        try:
            if not bool(authorized()):
                return {}
        except Exception:  # noqa: BLE001 - 不可信 AI 适配器失败时必须拒绝调用
            return {}
        ai_inputs: list[AiAttributionInput] = []
        local_map: dict[str, AttributedSubtitle] = {}
        trigger_by_key: dict[str, str] = {}
        # 适配器自身按触发规则过滤；这里必须传入全部常规归属结果，
        # 否则无 TMDB/IMDb 身份的“当前集”会在首次分类后被错误绕过 AI。
        for index, result in enumerate(attributed, start=1):
            # 只把语义不确定项交给适配器；明确其他媒体、范围冲突和格式错误
            # 由常规状态机处理，不能被 AI 覆盖。
            if self._ai_original_rejection_reason(result.evidence, item.context) is not None:
                continue
            canonical_trigger = self._canonical_ai_trigger(
                result.evidence,
                item.context,
                snapshot,
            )
            if canonical_trigger is None:
                continue
            try:
                trigger = should(result.evidence, item.context, snapshot)
            except Exception:  # noqa: BLE001 - 不可信 AI 适配器失败时必须拒绝调用
                trigger = None
            if trigger != canonical_trigger or trigger not in AI_TAKEOVER_TRIGGER_REASONS:
                continue
            local_key = f"file_{index:04d}"
            ai_inputs.append(
                AiAttributionInput(
                    local_key=local_key,
                    logical_source_path=result.extracted.logical_source_path,
                    evidence=result.evidence,
                )
            )
            local_map[local_key] = result
            trigger_by_key[local_key] = canonical_trigger
        if not ai_inputs:
            return {}

        batch_started = False

        async def on_batch_start(data: dict[str, Any]) -> None:
            """为每个真实 LLM 批次建立独立阶段轨迹。"""

            nonlocal batch_started
            batch_started = True
            await self._set_stage(task, TaskStage.AI_ATTRIBUTION)

        async def on_batch_end(data: dict[str, Any]) -> None:
            """结束 AI 阶段并保存不含上下文的批次摘要。"""

            raw_reasons = data.get("reason_codes")
            reason_text = ""
            if isinstance(raw_reasons, dict):
                reason_parts = [
                    f"{str(key)[:64]}={int(value)}"
                    for key, value in sorted(raw_reasons.items())[:12]
                    if isinstance(value, int) and value > 0
                ]
                if reason_parts:
                    reason_text = f"，原因 {'、'.join(reason_parts)}"
            summary = (
                f"候选 {str(data.get('candidate_key') or '')[:128]}，"
                f"第 {int(data.get('batch_number') or 0)} 批，"
                f"提交 {int(data.get('submitted_count') or 0)} 项，"
                f"采纳 {int(data.get('accepted_count') or 0)} 项，"
                f"拒绝 {int(data.get('rejected_count') or 0)} 项，"
                f"错误 {int(data.get('error_count') or 0)} 项，"
                f"结果 {str(data.get('call_result') or 'unknown')[:64]}"
                f"{reason_text}"
            )
            await self._finish_stage(task, summary)
            await self._save(task)

        try:
            call = getattr(adapter, "attribute_files", None) or getattr(adapter, "takeover", None)
            if call is None:
                return {}
            try:
                parameters = inspect.signature(call).parameters.values()
                supports_callbacks = any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
                ) or {"on_batch_start", "on_batch_end"}.issubset(inspect.signature(call).parameters)
            except (TypeError, ValueError):
                # 无法反射的可调用对象按完整协议调用一次；绝不以 TypeError 重试。
                supports_callbacks = True
            try:
                if not bool(authorized()):
                    return {}
            except Exception:  # noqa: BLE001 - 不可信 AI 适配器失败时必须拒绝调用
                return {}
            if supports_callbacks:
                ai_result = await call(
                    item.context,
                    candidate,
                    snapshot,
                    ai_inputs,
                    task.package_attribution_strategy,
                    on_batch_start=on_batch_start,
                    on_batch_end=on_batch_end,
                )
            else:
                # 兼容只实现核心五参数的旧测试替身，调用仍只发生一次。
                ai_result = await call(
                    item.context,
                    candidate,
                    snapshot,
                    ai_inputs,
                    task.package_attribution_strategy,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - AI 接管错误必须软失败
            # AI 普通错误必须软失败，不能污染候选通用异常路径。
            logger.warning(f"{self._task_label(task)}执行字幕 AI 接管失败，将保留常规归属证据：{type(exc).__name__}")
            if batch_started:
                await self._set_stage(task, TaskStage.MATCH)
            return {"error_count": len(ai_inputs), "reason_summary": {"adapter_error": len(ai_inputs)}}

        if ai_result is None:
            return {}
        normalized_result = self._normalize_ai_takeover_result(
            ai_result,
            local_keys=set(local_map),
        )
        if normalized_result is None:
            logger.warning(f"{self._task_label(task)}收到非法字幕 AI 接管结果，将保留常规归属证据")
            if batch_started:
                await self._set_stage(task, TaskStage.MATCH)
            return {
                "attempt_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "error_count": len(ai_inputs),
                "over_limit_count": 0,
                "reason_summary": {"adapter_result_invalid": len(ai_inputs)},
            }
        proposed_by_key = normalized_result["evidence_by_key"]
        audits_by_key = normalized_result["audits_by_key"]
        reason_summary = normalized_result["reason_summary"]
        valid_evidence_by_key: dict[str, FileAttributionEvidence] = {}
        application_rejections = 0

        adoption_authorized = False
        try:
            adoption_authorized = bool(authorized())
        except Exception:  # noqa: BLE001 - 不可信 AI 适配器失败时必须拒绝采用
            adoption_authorized = False

        for local_key, proposed in proposed_by_key.items():
            target = local_map.get(local_key)
            if target is None:
                continue
            if not adoption_authorized:
                validated = None
                rejection_reason = "authorization_revoked_before_adoption"
            else:
                validated, rejection_reason = self._validate_ai_takeover_evidence(
                    original=target.evidence,
                    proposed=proposed,
                    context=item.context,
                    snapshot=snapshot,
                    strategy=task.package_attribution_strategy,
                    expected_trigger=trigger_by_key.get(local_key),
                )
            if validated is not None:
                valid_evidence_by_key[local_key] = validated
                target.evidence = validated
                continue
            application_rejections += 1
            reason = rejection_reason or "application_validation_failed"
            reason_summary[reason] = reason_summary.get(reason, 0) + 1
            audit = self._application_rejection_audit(
                audits_by_key.get(local_key),
                proposed,
                target.evidence,
                task.package_attribution_strategy,
                reason,
            )
            target.evidence = target.evidence.model_copy(update={"ai_takeover_audit": audit})
        # 被拒绝/错误的项目也保存脱敏单文件审计，但不改写原方法、字段证据或原因。
        for local_key, audit in audits_by_key.items():
            target = local_map.get(local_key)
            if target is None or local_key in valid_evidence_by_key or local_key in proposed_by_key:
                continue
            try:
                normalized_audit = AiAttributionAudit.model_validate(self._sanitize_audit_payload(audit))
            except (TypeError, ValueError, ValidationError):
                application_rejections += 1
                reason = "application_audit_invalid"
                reason_summary[reason] = reason_summary.get(reason, 0) + 1
                normalized_audit = self._application_rejection_audit(
                    None,
                    None,
                    target.evidence,
                    task.package_attribution_strategy,
                    reason,
                )
            if normalized_audit.outcome is AiAttributionOutcome.ACCEPTED:
                application_rejections += 1
                reason = "application_evidence_missing"
                reason_summary[reason] = reason_summary.get(reason, 0) + 1
                normalized_audit = self._application_rejection_audit(
                    normalized_audit,
                    None,
                    target.evidence,
                    task.package_attribution_strategy,
                    reason,
                )
            target.evidence = target.evidence.model_copy(update={"ai_takeover_audit": normalized_audit})
        # 若至少有真实批次，确保后续状态机回到匹配阶段；未调用不伪造阶段。
        if batch_started or normalized_result["request_count"] > 0:
            await self._set_stage(task, TaskStage.MATCH)
        return {
            "attempt_count": normalized_result["submitted_count"],
            "accepted_count": len(valid_evidence_by_key),
            "rejected_count": normalized_result["rejected_count"] + application_rejections,
            "error_count": normalized_result["error_count"],
            "over_limit_count": normalized_result["over_limit_count"],
            "reason_summary": reason_summary,
        }

    @staticmethod
    def _unique_scope_value(evidence: FileAttributionEvidence, field: str) -> tuple[int | None, int]:
        """读取保留基数的季集字段，兼容旧测试替身的单值证据。"""

        values = list(getattr(evidence, f"{field}_values", []) or [])
        scalar = getattr(evidence, field, None)
        if not values and scalar is not None:
            values = [scalar]
        return (values[0], 1) if len(values) == 1 else (None, len(values))

    @staticmethod
    def _normalized_imdb_id(value: str | None) -> str | None:
        """规范化 IMDb ID 用于应用层确定性比较。"""

        if not value:
            return None
        normalized = value.strip().lower().removeprefix("tt").lstrip("0")
        return normalized or "0"

    @staticmethod
    def _model_payload(value: Any) -> Any:
        """把 Pydantic 实例展开为字典，强制边界重新执行模型校验。"""

        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return dump(mode="python")
        return value

    @staticmethod
    def _normalize_ai_takeover_result(
        value: Any,
        *,
        local_keys: set[str],
    ) -> dict[str, Any] | None:
        """把适配器结果收敛为有界安全结构，非法结果整体拒绝。"""

        if isinstance(value, (Mapping, list, tuple, set, str, bytes, bytearray)):
            return None
        try:
            if not any(hasattr(value, field) for field in ("evidence_by_key", "audits_by_key", "reason_summary")):
                return None
        except Exception:  # noqa: BLE001 - 不可信适配器对象的属性探测必须失败关闭
            return None
        item_limit = max(1, len(local_keys))

        def normalize_mapping(field: str) -> dict[str, Any] | None:
            """读取并过滤一项有限大小的适配器映射。"""

            try:
                raw = getattr(value, field, {})
                if raw is None:
                    return {}
                if not isinstance(raw, Mapping) or len(raw) > item_limit * 2:
                    return None
                return {key: item for key, item in raw.items() if isinstance(key, str) and key in local_keys}
            except Exception:  # noqa: BLE001 - 不可信适配器映射读取必须失败关闭
                return None

        evidence_by_key = normalize_mapping("evidence_by_key")
        audits_by_key = normalize_mapping("audits_by_key")
        if evidence_by_key is None or audits_by_key is None:
            return None

        try:
            raw_reasons = getattr(value, "reason_summary", {})
            if raw_reasons is None:
                raw_reasons = {}
            if not isinstance(raw_reasons, Mapping) or len(raw_reasons) > 32:
                return None
            reason_summary: dict[str, int] = {}
            for key, count in raw_reasons.items():
                if (
                    not isinstance(key, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or count > item_limit
                ):
                    return None
                if count > 0:
                    reason_summary[key] = count

            counts: dict[str, int] = {}
            for field in (
                "request_count",
                "submitted_count",
                "accepted_count",
                "rejected_count",
                "error_count",
                "over_limit_count",
            ):
                count = getattr(value, field, 0)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > item_limit:
                    return None
                counts[field] = count
        except Exception:  # noqa: BLE001 - 不可信适配器统计读取必须失败关闭
            return None

        return {
            "evidence_by_key": evidence_by_key,
            "audits_by_key": audits_by_key,
            "reason_summary": reason_summary,
            **counts,
        }

    @staticmethod
    def _safe_audit_label(value: Any) -> str | None:
        """清洗 AI 审计中的 Provider/模型标签，拒绝控制字符和超长自由文本。"""

        if not isinstance(value, str):
            return None
        # Provider 与模型名只用于观测；isprintable 同时拒绝 C0/DEL 与
        # U+200B、U+202E 等不可见 Unicode 格式控制字符。
        if any(not char.isprintable() for char in value):
            return None
        cleaned = " ".join(value.split()).strip()
        if not cleaned or len(cleaned) > 128:
            return None
        return cleaned

    @classmethod
    def _sanitize_audit_payload(cls, value: Any) -> Any:
        """在重新验证审计模型前清洗动态 Provider/模型字段。"""

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return payload
        sanitized = dict(payload)
        sanitized["provider"] = cls._safe_audit_label(payload.get("provider"))
        sanitized["model"] = cls._safe_audit_label(payload.get("model"))
        return sanitized

    @classmethod
    def _sanitize_evidence_audit_payload(cls, value: Any) -> Any:
        """复制 AI 证据载荷并清洗其中的审计标签，不触碰其它字段。"""

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return payload
        audit = payload.get("ai_takeover_audit")
        if audit is None:
            return payload
        sanitized = dict(payload)
        sanitized["ai_takeover_audit"] = cls._sanitize_audit_payload(audit)
        return sanitized

    @classmethod
    def _raw_audit_evidence_reason(cls, value: Any) -> str | None:
        """在强类型模型校验前识别证据码篡改，保留稳定拒绝原因。

        ``model_copy(update=...)`` 不会重新运行 Pydantic 校验，因此测试替身或
        恶意适配器可能把未知、重复证据码嵌入一个看似合法的模型实例。若直接
        重新验证外层 ``FileAttributionEvidence``，这些情况都会被压缩成泛化的
        ``application_evidence_schema_invalid``，既不利于审计也无法区分契约
        违规类型。这里只读取证据码这一项，不信任或持久化其它原始字段；后续
        仍必须经过完整的 Pydantic 边界校验。
        """

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return None
        audit_payload = cls._model_payload(payload.get("ai_takeover_audit"))
        if not isinstance(audit_payload, dict):
            return None
        codes = audit_payload.get("evidence_codes")
        if not isinstance(codes, list):
            return None
        if any(not isinstance(code, str) or code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in codes):
            return "application_evidence_code_invalid"
        if len(set(codes)) != len(codes):
            return "application_evidence_code_duplicate"
        return None

    @staticmethod
    def _ai_original_rejection_reason(
        original: FileAttributionEvidence,
        context: MediaContext,
    ) -> str | None:
        """返回不允许进入 AI 接管边界的原始文件证据原因。"""

        if original.method not in {
            FileAttributionMethod.TRUST_PACKAGE,
            FileAttributionMethod.HOST_RECOGNITION,
        }:
            return "application_original_method_invalid"
        if original.belongs_to_target_media is False:
            return "application_original_media_binding_invalid"
        if original.unmatched_reason in {
            UnmatchedReason.CANDIDATE_FILE_SCOPE_CONFLICT,
            UnmatchedReason.UNSUPPORTED_FORMAT,
        }:
            return "application_original_reason_invalid"
        if (
            original.media_type is not MediaType.UNKNOWN
            and context.media_type is not MediaType.UNKNOWN
            and original.media_type is not context.media_type
        ):
            return "application_original_media_type_invalid"
        return None

    @staticmethod
    def _canonical_ai_trigger(
        evidence: FileAttributionEvidence,
        context: MediaContext,
        snapshot: CandidateAttributionSnapshot,
    ) -> str | None:
        """根据原始证据重新计算唯一允许的 AI 接管触发原因。"""

        del snapshot  # 候选范围只在建议校验阶段使用，触发判定不猜测范围。
        if evidence.method not in {
            FileAttributionMethod.TRUST_PACKAGE,
            FileAttributionMethod.HOST_RECOGNITION,
        }:
            return None
        if evidence.belongs_to_target_media is False:
            return None
        if (
            evidence.media_type is not MediaType.UNKNOWN
            and context.media_type is not MediaType.UNKNOWN
            and evidence.media_type is not context.media_type
        ):
            return None
        if evidence.unmatched_reason in {
            UnmatchedReason.CANDIDATE_FILE_SCOPE_CONFLICT,
            UnmatchedReason.UNSUPPORTED_FORMAT,
        }:
            return None
        if evidence.unmatched_reason is UnmatchedReason.MEDIA_UNRECOGNIZED:
            return "media_unrecognized"

        if context.canonical_identity is None:
            if context.media_type is MediaType.TV:
                season_value, season_count = TaskCoordinator._unique_scope_value(
                    evidence,
                    "season",
                )
                episode_value, episode_count = TaskCoordinator._unique_scope_value(
                    evidence,
                    "episode",
                )
                if (
                    season_count == 1
                    and episode_count == 1
                    and context.season is not None
                    and context.episode is not None
                    and (season_value != context.season or episode_value != context.episode)
                ):
                    return None
            return "identity_missing"
        if evidence.belongs_to_target_media is not True:
            return "media_unrecognized"
        if context.media_type is MediaType.TV:
            _season_value, season_count = TaskCoordinator._unique_scope_value(
                evidence,
                "season",
            )
            _episode_value, episode_count = TaskCoordinator._unique_scope_value(
                evidence,
                "episode",
            )
            if season_count != 1:
                return "season_ambiguous"
            if episode_count != 1:
                return "episode_ambiguous"
        return None

    @classmethod
    def _validate_ai_takeover_evidence(
        cls,
        *,
        original: FileAttributionEvidence,
        proposed: Any,
        context: MediaContext,
        snapshot: CandidateAttributionSnapshot,
        strategy: PackageAttributionStrategy,
        expected_trigger: str | None = None,
    ) -> tuple[FileAttributionEvidence | None, str | None]:
        """在应用边界复核 AI 证据，防止适配器绕过既定媒体与来源约束。"""

        # AI 只能接管常规的候选包/宿主识别结果。直接字幕文件已经由用户
        # 选择的目标确定归属，已有 AI 结果也不得再次被包装成新的接管；明确
        # 属于其它媒体、候选范围冲突或格式不支持的结果同样不能由模型覆盖。
        original_rejection = cls._ai_original_rejection_reason(original, context)
        if original_rejection is not None:
            return None, original_rejection

        raw_evidence_code_reason = cls._raw_audit_evidence_reason(proposed)
        if raw_evidence_code_reason is not None:
            return None, raw_evidence_code_reason
        try:
            evidence = FileAttributionEvidence.model_validate(cls._sanitize_evidence_audit_payload(proposed))
        except (TypeError, ValueError, ValidationError):
            return None, "application_evidence_schema_invalid"
        if evidence.method is not FileAttributionMethod.AI_TAKEOVER:
            return None, "application_method_invalid"
        if evidence.logical_source_path != original.logical_source_path:
            return None, "application_logical_path_changed"
        if evidence.belongs_to_target_media is not True or evidence.unmatched_reason is not None:
            return None, "application_media_binding_invalid"
        if context.media_type is MediaType.UNKNOWN or evidence.media_type is not context.media_type:
            return None, "application_media_type_invalid"
        if evidence.year != original.year:
            return None, "application_year_changed"
        if context.tmdb_id is None:
            if evidence.tmdb_id is not None:
                return None, "application_tmdb_identity_invalid"
        elif evidence.tmdb_id != context.tmdb_id:
            return None, "application_tmdb_identity_invalid"
        if context.imdb_id is None:
            if evidence.imdb_id is not None:
                return None, "application_imdb_identity_invalid"
        elif cls._normalized_imdb_id(evidence.imdb_id) != cls._normalized_imdb_id(context.imdb_id):
            return None, "application_imdb_identity_invalid"

        original_season, original_season_count = cls._unique_scope_value(original, "season")
        original_episode, original_episode_count = cls._unique_scope_value(original, "episode")
        if context.media_type is MediaType.MOVIE:
            if (
                evidence.season is not None
                or evidence.episode is not None
                or evidence.season_values
                or evidence.episode_values
            ):
                return None, "application_movie_scope_invalid"
        else:
            if evidence.season is None or evidence.episode is None:
                return None, "application_tv_scope_incomplete"
            if evidence.season_values != [evidence.season] or evidence.episode_values != [evidence.episode]:
                return None, "application_scope_not_unique"
            if original_season_count == 1 and evidence.season != original_season:
                return None, "application_locked_season_changed"
            if original_episode_count == 1 and evidence.episode != original_episode:
                return None, "application_locked_episode_changed"
            if snapshot.seasons and evidence.season not in snapshot.seasons:
                return None, "application_season_out_of_candidate_scope"
            if snapshot.episodes and evidence.episode not in snapshot.episodes:
                return None, "application_episode_out_of_candidate_scope"
            expected_season_evidence = (
                original.season_evidence if original_season_count == 1 else AttributionEvidence.AI_TAKEOVER
            )
            expected_episode_evidence = (
                original.episode_evidence if original_episode_count == 1 else AttributionEvidence.AI_TAKEOVER
            )
            if evidence.season_evidence is not expected_season_evidence:
                return None, "application_season_provenance_invalid"
            if evidence.episode_evidence is not expected_episode_evidence:
                return None, "application_episode_provenance_invalid"

        if evidence.ai_before_method is not original.method:
            return None, "application_before_method_invalid"
        if evidence.ai_before_unmatched_reason is not original.unmatched_reason:
            return None, "application_before_reason_invalid"
        if evidence.host_recognition_summary != original.host_recognition_summary:
            return None, "application_host_summary_changed"
        raw_audit = evidence.ai_takeover_audit
        if raw_audit is None:
            return None, "application_audit_invalid"
        # 在强类型重验前保留稳定的应用层原因码；否则领域模型会把未知或
        # 重复证据码统一归类为 schema_invalid，丢失对调用方有用的拒绝原因。
        raw_audit_payload = cls._sanitize_audit_payload(raw_audit)
        if isinstance(raw_audit_payload, dict):
            raw_codes = raw_audit_payload.get("evidence_codes")
            if isinstance(raw_codes, list):
                if any(not isinstance(code, str) for code in raw_codes):
                    return None, "application_evidence_code_invalid"
                if len(set(raw_codes)) != len(raw_codes):
                    return None, "application_evidence_code_duplicate"
                if any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in raw_codes):
                    return None, "application_evidence_code_invalid"
        try:
            audit = AiAttributionAudit.model_validate(raw_audit_payload)
        except (TypeError, ValueError, ValidationError):
            return None, "application_audit_invalid"
        evidence = evidence.model_copy(update={"ai_takeover_audit": audit})
        if audit.outcome is not AiAttributionOutcome.ACCEPTED:
            return None, "application_audit_invalid"
        if expected_trigger is None or audit.trigger_reason != expected_trigger:
            return None, "application_trigger_reason_mismatch"
        if audit.confidence is not AiAttributionConfidence.HIGH:
            return None, "application_confidence_not_high"
        if not audit.evidence_codes or any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in audit.evidence_codes):
            return None, "application_evidence_code_invalid"
        if len(set(audit.evidence_codes)) != len(audit.evidence_codes):
            return None, "application_evidence_code_duplicate"
        no_target_identity = context.tmdb_id is None and context.imdb_id is None
        expected_reason = "accepted_without_identity_title" if no_target_identity else "accepted"
        if audit.reason_code != expected_reason:
            return None, "application_accepted_reason_invalid"
        if no_target_identity:
            if not ({"title", "alias"} & set(audit.evidence_codes)):
                return None, "application_identity_title_evidence_missing"
            reference_years = {value for value in (original.year, snapshot.year) if isinstance(value, int)}
            if context.year is not None and any(year != context.year for year in reference_years):
                return None, "application_year_mismatch"
        if (
            audit.before_strategy is not strategy
            or audit.original_unmatched_reason is not original.unmatched_reason
            or audit.media_type is not evidence.media_type
            or audit.tmdb_id != evidence.tmdb_id
            or cls._normalized_imdb_id(audit.imdb_id) != cls._normalized_imdb_id(evidence.imdb_id)
            or audit.season != evidence.season
            or audit.episode != evidence.episode
        ):
            return None, "application_audit_mismatch"
        return evidence, None

    @staticmethod
    def _application_rejection_audit(
        audit_value: Any,
        proposed_value: Any,
        original: FileAttributionEvidence,
        strategy: PackageAttributionStrategy,
        reason: str,
    ) -> AiAttributionAudit:
        """把应用层拒绝转换为不保留错误建议字段的强类型审计。"""

        # 应用层拒绝时不信任适配器提供的 Provider、模型、触发文本或任何
        # 建议字段，统一生成最小脱敏审计，避免异常替身把秘密写入持久化记录。
        del audit_value, proposed_value
        return AiAttributionAudit(
            before_strategy=strategy,
            original_unmatched_reason=original.unmatched_reason,
            trigger_reason="application_validation",
            outcome=AiAttributionOutcome.REJECTED,
            reason_code=reason,
        )

    async def _candidate_files(
        self,
        task: SubtitleTask,
        item: TaskWorkItem,
        handle: CandidateHandle,
        files: list[ExtractedSubtitle],
    ) -> tuple[
        AttributedSubtitle | None,
        list[AttributedSubtitle],
        CandidateAttributionSnapshot,
        _CandidateFileMetrics,
    ]:
        """逐文件归属并选出当前目标第一优先字幕。"""

        candidate = handle.candidate
        snapshot = self.matcher.candidate_snapshot(candidate)
        task.candidate_attribution_snapshot = snapshot
        attributed: list[AttributedSubtitle] = []
        other_media_count = 0
        host_file_count = sum(1 for extracted in files if not extracted.is_direct_file)
        if task.package_attribution_strategy is PackageAttributionStrategy.HOST_RECOGNITION and host_file_count:
            logger.info(
                f"{self._task_label(task)}开始调用 MoviePilot 文件识别处理"
                f"{self._candidate_label(candidate)}中的 {host_file_count} 个字幕"
            )
        for extracted in files:
            if extracted.is_direct_file:
                context = item.context
                evidence = FileAttributionEvidence(
                    logical_source_path=extracted.logical_source_path,
                    method=FileAttributionMethod.DIRECT_FILE,
                    belongs_to_target_media=True,
                    media_type=context.media_type,
                    tmdb_id=context.tmdb_id,
                    imdb_id=context.imdb_id,
                    season=context.season,
                    episode=context.episode,
                    season_evidence=AttributionEvidence.NOT_APPLICABLE,
                    episode_evidence=AttributionEvidence.NOT_APPLICABLE,
                )
            else:
                evidence = await self.matcher.attribute_file(
                    extracted.physical_path,
                    extracted.logical_source_path,
                    item.context,
                    snapshot,
                    task.package_attribution_strategy,
                )
            if evidence.belongs_to_target_media is False:
                other_media_count += 1
                continue
            attributed.append(AttributedSubtitle(extracted=extracted, evidence=evidence))
        if task.package_attribution_strategy is PackageAttributionStrategy.HOST_RECOGNITION and host_file_count:
            logger.info(
                f"{self._task_label(task)}已完成 MoviePilot 文件识别，"
                f"处理 {host_file_count} 个字幕，其中明确属于其他媒体 {other_media_count} 个"
            )

        # 常规归属完成后只把模糊项交给 AI；明确归属、其他媒体和非语义故障
        # 在这里先分类，避免模型覆盖确定性冲突。
        current_files: list[AttributedSubtitle] = []
        additional: list[AttributedSubtitle] = []
        ambiguous_count = 0
        same_media_other_episode_count = 0

        def classify() -> tuple[list[AttributedSubtitle], list[AttributedSubtitle], int, int]:
            """按最新证据重算当前集、附加集和漏斗计数。"""

            current: list[AttributedSubtitle] = []
            extra: list[AttributedSubtitle] = []
            ambiguous = 0
            other_episode = 0
            for candidate_result in attributed:
                candidate_evidence = candidate_result.evidence
                season_value, season_count = self._unique_scope_value(candidate_evidence, "season")
                episode_value, episode_count = self._unique_scope_value(candidate_evidence, "episode")
                complete = candidate_evidence.belongs_to_target_media is True and (
                    item.context.media_type is MediaType.MOVIE
                    or (
                        season_count == 1
                        and episode_count == 1
                        and season_value is not None
                        and episode_value is not None
                    )
                )
                if not complete or candidate_evidence.unmatched_reason is not None:
                    ambiguous += 1
                    extra.append(candidate_result)
                    continue
                is_current = item.context.media_type is MediaType.MOVIE or (
                    season_value == item.context.season and episode_value == item.context.episode
                )
                if is_current:
                    current.append(candidate_result)
                else:
                    other_episode += 1
                    extra.append(candidate_result)
            return current, extra, ambiguous, other_episode

        current_files, additional, ambiguous_count, same_media_other_episode_count = classify()

        ai_metrics = await self._run_ai_takeover(
            task,
            item,
            candidate,
            snapshot,
            attributed,
        )
        if ai_metrics:
            # AI 采纳可能把原本模糊项变成当前集，需重新计算选择漏斗。
            current_files, additional, ambiguous_count, same_media_other_episode_count = classify()
        metrics: _CandidateFileMetrics = {
            "extracted_count": len(files),
            "current_target_count": len(current_files),
            "same_media_other_episode_count": same_media_other_episode_count,
            "ambiguous_count": ambiguous_count,
            "other_media_count": other_media_count,
            "ai_attempt_count": int(ai_metrics.get("attempt_count", 0) or 0),
            "ai_accepted_count": int(ai_metrics.get("accepted_count", 0) or 0),
            "ai_rejected_count": int(ai_metrics.get("rejected_count", 0) or 0),
            "ai_error_count": int(ai_metrics.get("error_count", 0) or 0),
            "ai_over_limit_count": int(ai_metrics.get("over_limit_count", 0) or 0),
            "ai_reason_summary": dict(ai_metrics.get("reason_summary", {}) or {}),
        }
        if not current_files:
            return None, additional, snapshot, metrics
        format_order = {value.upper().lstrip("."): index for index, value in enumerate(self.config.format_priority)}
        current_files.sort(
            key=lambda result: (
                format_order.get(result.extracted.physical_path.suffix.lstrip(".").upper(), 999),
                result.extracted.logical_source_path,
            )
        )
        selected = current_files[0]
        additional.extend(result for result in current_files[1:] if result is not selected)
        return selected, additional, snapshot, metrics

    async def _make_record(
        self,
        task: SubtitleTask,
        context: MediaContext,
        candidate: SubtitleCandidate,
        result: AttributedSubtitle,
        snapshot: CandidateAttributionSnapshot,
        status: RecordStatus,
        location: FileLocation,
        path: str,
        final_path: str | None,
        bind_target: bool,
        *,
        persist: bool = True,
    ) -> MatchRecord:
        """构造一条安全匹配记录，并按需立即持久化。"""

        source_path = result.extracted.physical_path
        evidence = result.evidence
        size: int | None = None
        try:
            size = (await AsyncPath(source_path).stat()).st_size
        except OSError:
            size = None
        identity = context.canonical_identity if evidence.belongs_to_target_media is True else None
        record = MatchRecord(
            subtitle_file_name=source_path.name,
            format=source_path.suffix.lstrip(".").upper(),
            size=size,
            media_title=context.title if evidence.belongs_to_target_media is True else None,
            year=context.year if evidence.belongs_to_target_media is True else None,
            media_type=evidence.media_type,
            season=evidence.season,
            episode=evidence.episode,
            status=status,
            source=candidate.source,
            package_scope=candidate.package_scope,
            location=location,
            path=path,
            canonical_identity_type=identity[0] if identity else None,
            canonical_identity_value=identity[1] if identity else None,
            tmdb_id=evidence.tmdb_id,
            imdb_id=evidence.imdb_id,
            target_history_id=task.target_history_id if bind_target else None,
            history_target_path=task.history_target_path if bind_target else None,
            target_path=task.target_path if bind_target else None,
            matched_path_mapping=task.matched_path_mapping if bind_target else None,
            target_file_exists=task.target_file_exists if bind_target else None,
            final_subtitle_path=final_path,
            source_task_id=task.id,
            candidate_key=candidate.stable_key,
            candidate_name=candidate.name,
            candidate_attribution_snapshot=snapshot,
            logical_source_path=evidence.logical_source_path,
            file_attribution_method=evidence.method,
            season_evidence=evidence.season_evidence,
            episode_evidence=evidence.episode_evidence,
            unmatched_reason=evidence.unmatched_reason,
            host_recognition_summary=evidence.host_recognition_summary,
            language=candidate.language,
            translation_type=candidate.translation_type,
            hearing_impaired=candidate.hearing_impaired,
            exact_id_match=candidate.exact_id_match,
            site_priority=candidate.site_priority,
            trusted=candidate.trusted,
            score=candidate.score,
            votes=candidate.votes,
            download_count=candidate.download_count,
            uploaded_at=candidate.uploaded_at,
            revision=candidate.revision,
            ai_takeover_audit=evidence.ai_takeover_audit,
        )
        if status is RecordStatus.STAGED:
            record.staged_at = record.created_at
        if persist:
            await self.store.save_record(record)
        task.record_ids.append(record.id)
        task.record_counts[status.value] = task.record_counts.get(status.value, 0) + 1
        return record

    @staticmethod
    def _can_stage(result: AttributedSubtitle, context: MediaContext) -> bool:
        """判断具体字幕归属是否足够进入暂存库存。"""

        evidence = result.evidence
        if evidence.belongs_to_target_media is not True or evidence.unmatched_reason is not None:
            return False
        if context.media_type is MediaType.MOVIE:
            return context.canonical_identity is not None
        return bool(
            context.canonical_identity is not None and evidence.season is not None and evidence.episode is not None
        )

    async def _save_plugin_result(
        self,
        task: SubtitleTask,
        context: MediaContext,
        candidate: SubtitleCandidate,
        result: AttributedSubtitle,
        snapshot: CandidateAttributionSnapshot,
        *,
        bind_target: bool,
    ) -> MatchRecord:
        """把归属完整或不完整的字幕保存为暂存或未匹配记录。"""

        status = RecordStatus.STAGED if self._can_stage(result, context) else RecordStatus.UNMATCHED
        record = await self._make_record(
            task,
            context,
            candidate,
            result,
            snapshot,
            status,
            FileLocation.PLUGIN_DATA,
            "",
            None,
            bind_target,
            persist=False,
        )
        try:
            record.path = await self.filesystem.save_plugin_file(
                result.extracted.physical_path,
                record.id,
                status,
            )
            publisher = getattr(self.inventory, "publish", None)
            if callable(publisher):
                await publisher(record)
            else:
                # 兼容只实现旧库存协议的测试替身；生产 SubtitleInventory 会在
                # 与删除、改配相同的 mutation 锁内完成记录发布和索引加入。
                await self.store.save_record(record)
                if status is RecordStatus.STAGED:
                    await self.inventory.add(record)
        except BaseException as exc:
            cleanup_errors: list[BaseException] = []
            try:
                await self.store.delete_record(record.id)
            except BaseException as cleanup_exc:  # noqa: BLE001 - 取消期间的清理失败也必须审计
                cleanup_errors.append(cleanup_exc)
            if record.path:
                try:
                    await self.filesystem.delete_plugin_file(record.path)
                except BaseException as cleanup_exc:  # noqa: BLE001 - 取消期间的清理失败也必须审计
                    cleanup_errors.append(cleanup_exc)
            if cleanup_errors:
                logger.error(
                    f"匹配记录 {record.id} 发布失败且清理未完整完成，"
                    f"可能残留插件数据文件或记录；异常类型为 "
                    f"{type(cleanup_errors[0]).__name__}"
                )
            task.record_ids = [record_id for record_id in task.record_ids if record_id != record.id]
            task.record_counts[status.value] = max(0, task.record_counts.get(status.value, 1) - 1)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise
        return record

    async def _save_additional_results(
        self,
        task: SubtitleTask,
        context: MediaContext,
        candidate: SubtitleCandidate,
        results: list[AttributedSubtitle],
        snapshot: CandidateAttributionSnapshot,
    ) -> tuple[int, int]:
        """保存附加字幕，单文件失败仅形成任务警告。"""

        staged_count = 0
        unmatched_count = 0
        for result in results:
            evidence = result.evidence
            bind_target = bool(
                evidence.belongs_to_target_media is True
                and (
                    context.media_type is MediaType.MOVIE
                    or (evidence.season == context.season and evidence.episode == context.episode)
                )
            )
            try:
                record = await self._save_plugin_result(
                    task,
                    context,
                    candidate,
                    result,
                    snapshot,
                    bind_target=bind_target,
                )
            except Exception as exc:  # noqa: BLE001 - 附加字幕失败不能中断候选处理
                task.warning_count += 1
                task.warning_summaries.append(f"附加字幕保存失败：{type(exc).__name__}")
                logger.warning(
                    f"{self._task_label(task)}保存附加字幕"
                    f"“{result.extracted.logical_source_path}”失败，将继续处理任务："
                    f"{type(exc).__name__}"
                )
                continue
            if record.status is RecordStatus.STAGED:
                staged_count += 1
                logger.info(
                    f"{self._task_label(task)}已将附加字幕"
                    f"“{result.extracted.logical_source_path}”保存为暂存记录 {record.id}"
                )
            else:
                unmatched_count += 1
                logger.info(
                    f"{self._task_label(task)}无法完整确认附加字幕"
                    f"“{result.extracted.logical_source_path}”的归属，"
                    f"已保存为未匹配记录 {record.id}"
                )
        return staged_count, unmatched_count

    async def _try_candidate(self, task: SubtitleTask, item: TaskWorkItem, handle: CandidateHandle) -> bool:
        """下载、解包、匹配并落盘一个候选。"""

        candidate = handle.candidate
        snapshot = self.matcher.candidate_snapshot(candidate)
        task.candidate_attribution_snapshot = snapshot
        attempt_result = AttemptResult.NO_MATCH
        error_summary: str | None = None
        active_stage = TaskStage.DOWNLOAD
        metrics: _CandidateFileMetrics = {
            "extracted_count": 0,
            "current_target_count": 0,
            "same_media_other_episode_count": 0,
            "ambiguous_count": 0,
            "other_media_count": 0,
            "ai_attempt_count": 0,
            "ai_accepted_count": 0,
            "ai_rejected_count": 0,
            "ai_error_count": 0,
            "ai_over_limit_count": 0,
            "ai_reason_summary": {},
        }
        written_count = 0
        staged_count = 0
        unmatched_count = 0
        task_dir = await self.filesystem.make_task_directory(task.id)
        candidate_dir = task_dir / f"candidate-{len(task.candidate_attempts) + 1}"
        await AsyncPath(candidate_dir).mkdir(parents=True, exist_ok=True)
        try:
            await self._set_stage(task, TaskStage.DOWNLOAD)
            active_stage = TaskStage.DOWNLOAD
            logger.info(f"{self._task_label(task)}开始下载{self._candidate_label(candidate)}")
            asset = await self.sources[candidate.source].download(handle, candidate_dir)
            logger.info(
                f"{self._task_label(task)}已下载{self._candidate_label(candidate)}，得到文件“{asset.file_name}”"
            )
            asset_extension = asset.path.suffix.lower().lstrip(".")
            archive_extensions = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "cab", "iso"}
            allowed_extensions = {value.lower().lstrip(".") for value in self.config.format_priority}
            if item.manual_handle is not None and asset_extension not in allowed_extensions | archive_extensions:
                unsupported = AttributedSubtitle(
                    extracted=ExtractedSubtitle(
                        physical_path=asset.path,
                        logical_source_path=asset.file_name,
                        is_direct_file=True,
                    ),
                    evidence=FileAttributionEvidence(
                        logical_source_path=asset.file_name,
                        method=FileAttributionMethod.DIRECT_FILE,
                        belongs_to_target_media=None,
                        unmatched_reason=UnmatchedReason.UNSUPPORTED_FORMAT,
                    ),
                )
                record = await self._save_plugin_result(
                    task,
                    item.context,
                    candidate,
                    unsupported,
                    snapshot,
                    bind_target=True,
                )
                unmatched_count = 1
                attempt_result = AttemptResult.NO_MATCH
                error_summary = "字幕格式未知或不受宿主支持，文件已保存为未匹配"
                logger.warning(
                    f"{self._task_label(task)}下载的文件“{asset.file_name}”格式不受宿主支持，"
                    f"已保存为未匹配记录 {record.id}"
                )
                return False
            await self._set_stage(task, TaskStage.EXTRACT)
            active_stage = TaskStage.EXTRACT
            extracted = await self.archive.extract(asset, candidate_dir / "extracted", set(self.config.format_priority))
            if not extracted:
                attempt_result = AttemptResult.NO_MATCH
                error_summary = "候选包没有允许格式字幕"
                return False
            logger.info(
                f"{self._task_label(task)}已从{self._candidate_label(candidate)}中取得 "
                f"{len(extracted)} 个受支持的字幕文件"
            )
            await self._set_stage(task, TaskStage.MATCH)
            active_stage = TaskStage.MATCH
            selected, additional, snapshot, metrics = await self._candidate_files(
                task,
                item,
                handle,
                extracted,
            )
            if selected is None:
                attempt_result = AttemptResult.NO_MATCH
                if item.manual_handle is not None:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        item.context,
                        candidate,
                        additional,
                        snapshot,
                    )
                    error_summary = "候选包未找到当前目标字幕，其他有效结果已保留"
                else:
                    error_summary = "候选包未找到当前目标字幕"
                return False
            if item.manual_handle is not None:
                directory_available, directory_error = await self.filesystem.target_directory_status(
                    Path(task.target_path)
                )
                if not directory_available:
                    staged = await self._save_plugin_result(
                        task,
                        item.context,
                        candidate,
                        selected,
                        snapshot,
                        bind_target=True,
                    )
                    staged_count = 1 if staged.status is RecordStatus.STAGED else 0
                    unmatched_count = 1 if staged.status is RecordStatus.UNMATCHED else 0
                    extra_staged, extra_unmatched = await self._save_additional_results(
                        task,
                        item.context,
                        candidate,
                        additional,
                        snapshot,
                    )
                    staged_count += extra_staged
                    unmatched_count += extra_unmatched
                    attempt_result = AttemptResult.WRITE_FAILED
                    error_summary = f"目标目录不可用：{directory_error or '无法写入'}，下载结果已保留"
                    return False
            await self._set_stage(task, TaskStage.WRITE)
            active_stage = TaskStage.WRITE
            selected_path = selected.extracted.physical_path
            destination = await self.filesystem.write_media_subtitle(selected_path, Path(task.target_path))
            task.final_subtitle_path = str(destination)
            main_record = await self._make_record(
                task,
                item.context,
                candidate,
                selected,
                snapshot,
                RecordStatus.MATCHED,
                FileLocation.MEDIA_DIRECTORY,
                str(destination),
                str(destination),
                True,
            )
            written_count = 1
            logger.info(
                f"{self._task_label(task)}已将字幕"
                f"“{selected.extracted.logical_source_path}”写入“{destination}”，"
                f"匹配记录为 {main_record.id}"
            )
            await self._set_stage(task, TaskStage.MATCH)
            active_stage = TaskStage.MATCH
            staged_count, unmatched_count = await self._save_additional_results(
                task,
                item.context,
                candidate,
                additional,
                snapshot,
            )
            task.result_source = candidate.source
            task.result_package_scope = candidate.package_scope
            task.result_format = selected_path.suffix.lstrip(".").upper()
            attempt_result = AttemptResult.SUCCESS
            return True
        except asyncio.CancelledError:
            attempt_result = AttemptResult.INTERRUPTED
            raise
        except FileExistsError:
            attempt_result = AttemptResult.WRITE_FAILED
            error_summary = "目标字幕已存在，未覆盖"
            if item.manual_handle is not None and "selected" in locals() and selected is not None:
                try:
                    staged = await self._save_plugin_result(
                        task,
                        item.context,
                        candidate,
                        selected,
                        snapshot,
                        bind_target=True,
                    )
                    staged_count = 1 if staged.status is RecordStatus.STAGED else 0
                    unmatched_count = 1 if staged.status is RecordStatus.UNMATCHED else 0
                    extra_staged, extra_unmatched = await self._save_additional_results(
                        task,
                        item.context,
                        candidate,
                        additional,
                        snapshot,
                    )
                    staged_count += extra_staged
                    unmatched_count += extra_unmatched
                    error_summary = "目标字幕已存在，下载结果已保留"
                    logger.warning(
                        f"{self._task_label(task)}的目标字幕路径已存在，"
                        f"当前字幕已保存为{('暂存' if staged.status is RecordStatus.STAGED else '未匹配')}"
                        f"记录 {staged.id}"
                    )
                except Exception as exc:  # noqa: BLE001 - 保留下载结果失败应返回安全失败
                    error_summary = f"目标字幕已存在，下载结果保留失败：{type(exc).__name__}"
            return False
        except (SourceRequestError, SourceLimitedError) as exc:
            attempt_result = AttemptResult.DOWNLOAD_FAILED
            error_summary = str(exc)
            return False
        except OSError as exc:
            attempt_result = AttemptResult.WRITE_FAILED
            error_summary = f"文件操作失败：{type(exc).__name__}"
            if (
                item.manual_handle is not None
                and active_stage is TaskStage.WRITE
                and written_count == 0
                and "selected" in locals()
                and selected is not None
            ):
                try:
                    preserved = await self._save_plugin_result(
                        task,
                        item.context,
                        candidate,
                        selected,
                        snapshot,
                        bind_target=True,
                    )
                    staged_count = 1 if preserved.status is RecordStatus.STAGED else 0
                    unmatched_count = 1 if preserved.status is RecordStatus.UNMATCHED else 0
                    extra_staged, extra_unmatched = await self._save_additional_results(
                        task,
                        item.context,
                        candidate,
                        additional,
                        snapshot,
                    )
                    staged_count += extra_staged
                    unmatched_count += extra_unmatched
                    error_summary += "，下载结果已保留"
                except Exception as preserve_exc:  # noqa: BLE001 - 保留下载结果失败应返回安全失败
                    error_summary += f"，下载结果保留失败：{type(preserve_exc).__name__}"
            return False
        except RuntimeError as exc:
            attempt_result = {
                TaskStage.DOWNLOAD: AttemptResult.DOWNLOAD_FAILED,
                TaskStage.EXTRACT: AttemptResult.EXTRACT_FAILED,
                TaskStage.MATCH: AttemptResult.NO_MATCH,
                TaskStage.WRITE: AttemptResult.WRITE_FAILED,
            }[active_stage]
            error_summary = f"{STAGE_NAMES[active_stage]}阶段失败：{exc}"
            return False
        except Exception as exc:  # noqa: BLE001 - 候选处理边界必须收敛运行时失败
            attempt_result = {
                TaskStage.DOWNLOAD: AttemptResult.DOWNLOAD_FAILED,
                TaskStage.EXTRACT: AttemptResult.EXTRACT_FAILED,
                TaskStage.MATCH: AttemptResult.NO_MATCH,
                TaskStage.WRITE: AttemptResult.WRITE_FAILED,
            }[active_stage]
            error_summary = f"候选处理失败：{type(exc).__name__}"
            return False
        finally:
            attempt = CandidateAttempt(
                candidate_key=candidate.stable_key,
                source=candidate.source,
                package_scope=candidate.package_scope,
                language=candidate.language,
                format=candidate.format,
                translation_type=candidate.translation_type,
                hearing_impaired=candidate.hearing_impaired,
                attribution_strategy=task.package_attribution_strategy,
                candidate_snapshot=snapshot,
                extracted_count=metrics["extracted_count"],
                current_target_count=metrics["current_target_count"],
                same_media_other_episode_count=metrics["same_media_other_episode_count"],
                ambiguous_count=metrics["ambiguous_count"],
                other_media_count=metrics["other_media_count"],
                written_count=written_count,
                staged_count=staged_count,
                unmatched_count=unmatched_count,
                result=attempt_result,
                error_summary=error_summary,
                ai_attempt_count=metrics["ai_attempt_count"],
                ai_accepted_count=metrics["ai_accepted_count"],
                ai_rejected_count=metrics["ai_rejected_count"],
                ai_error_count=metrics["ai_error_count"],
                ai_over_limit_count=metrics["ai_over_limit_count"],
                ai_reason_summary=metrics["ai_reason_summary"],
            )
            task.candidate_attempts.append(attempt)
            await self._save(task)
            attempt_number = len(task.candidate_attempts)
            log = logger.info if attempt_result is AttemptResult.SUCCESS else logger.warning
            if attempt_result is AttemptResult.SUCCESS:
                message = (
                    f"{self._task_label(task)}第 {attempt_number} 次候选尝试成功："
                    f"{self._candidate_label(candidate)}按“{task.package_attribution_strategy.value}”处理，"
                    f"解包 {metrics['extracted_count']} 个，当前目标 {metrics['current_target_count']} 个，"
                    f"其他季集 {metrics['same_media_other_episode_count']} 个，"
                    f"归属不明确 {metrics['ambiguous_count']} 个，"
                    f"其他媒体排除 {metrics['other_media_count']} 个；"
                    f"落盘 {written_count} 个、暂存 {staged_count} 个、未匹配 {unmatched_count} 个"
                )
            else:
                message = (
                    f"{self._task_label(task)}第 {attempt_number} 次候选尝试未成功："
                    f"{self._candidate_label(candidate)}在“{STAGE_NAMES[active_stage]}”阶段结束，"
                    f"原因是“{error_summary or ATTEMPT_RESULT_NAMES[attempt_result]}”；"
                    f"解包 {metrics['extracted_count']} 个，当前目标 {metrics['current_target_count']} 个，"
                    f"其他季集 {metrics['same_media_other_episode_count']} 个，"
                    f"归属不明确 {metrics['ambiguous_count']} 个，"
                    f"其他媒体排除 {metrics['other_media_count']} 个；"
                    f"落盘 {written_count} 个、暂存 {staged_count} 个、未匹配 {unmatched_count} 个"
                )
            log(message)

    async def _process(self, task: SubtitleTask, item: TaskWorkItem) -> None:
        """执行单个字幕任务的完整状态机。"""

        task.status = TaskStatus.PROCESSING
        task.started_at = utc_now()
        await self._save(task)
        try:
            if item.manual_handle is not None:
                await self._prepare_manual_target(task, item)
                if await self._try_candidate(task, item, item.manual_handle):
                    await self._finish_task(task, TaskStatus.SUCCESS, "subtitle_written", "人工选择的字幕已落盘")
                else:
                    attempt = task.candidate_attempts[-1] if task.candidate_attempts else None
                    reason = attempt.error_summary if attempt else None
                    reason_code = "manual_candidate_failed"
                    if reason and reason.startswith("目标目录不可用"):
                        reason_code = "target_directory_unavailable"
                    elif reason and reason.startswith("目标字幕已存在"):
                        reason_code = "subtitle_destination_conflict"
                    elif reason and "格式未知或不受宿主支持" in reason:
                        reason_code = UnmatchedReason.UNSUPPORTED_FORMAT.value
                    elif reason and "未找到当前目标字幕" in reason:
                        reason_code = "candidate_missing_target_subtitle"
                    await self._finish_task(
                        task,
                        TaskStatus.FAILED,
                        reason_code,
                        f"人工选择的字幕处理失败：{reason or '没有得到可落盘字幕'}",
                    )
                return
            await self._set_stage(task, TaskStage.PREFLIGHT)
            if not await self._preflight(task, item):
                return
            inventory = await self._consume_inventory(task, item)
            if inventory.matched:
                task.result_source = inventory.record.source if inventory.record else None
                task.result_package_scope = inventory.record.package_scope if inventory.record else None
                task.result_format = inventory.record.format if inventory.record else None
                task.final_subtitle_path = inventory.record.final_subtitle_path if inventory.record else None
                if inventory.record:
                    task.record_ids.append(inventory.record.id)
                    task.record_counts[RecordStatus.MATCHED.value] = 1
                await self._finish_task(task, TaskStatus.SUCCESS, "staged_inventory_consumed", "已消费字幕库存并落盘")
                return
            handles = await self._search_sources(task, item)
            if not handles:
                await self._finish_task(
                    task, TaskStatus.FAILED, "no_qualified_candidates", "没有可用的合格简中字幕候选"
                )
                return
            ordered = sorted(
                handles,
                key=lambda handle: candidate_rank(
                    handle.candidate,
                    self.config.format_priority,
                    self.config.source_priority,
                ),
            )
            for handle in ordered[: self.config.max_candidate_attempts]:
                if await self._try_candidate(task, item, handle):
                    await self._finish_task(task, TaskStatus.SUCCESS, "subtitle_written", "字幕已落盘")
                    return
            summaries = [
                f"{SOURCE_NAMES[attempt.source]} 候选“{attempt.candidate_key}”："
                f"{attempt.error_summary or ATTEMPT_RESULT_NAMES[attempt.result]}"
                for attempt in task.candidate_attempts
            ]
            attempted_count = len(task.candidate_attempts)
            unattempted_count = max(0, len(ordered) - attempted_count)
            if unattempted_count:
                reason_prefix = (
                    f"已达到最大候选尝试数 {self.config.max_candidate_attempts}；"
                    f"本次 {attempted_count} 次尝试均未成功，另有 {unattempted_count} 个候选未尝试"
                )
            else:
                reason_prefix = f"全部 {attempted_count} 个候选均已尝试但未成功"
            reason_message = f"{reason_prefix}：" + "；".join(summaries)
            await self._finish_task(task, TaskStatus.FAILED, "candidate_attempts_exhausted", reason_message)
        except asyncio.CancelledError:
            await self._finish_task(task, TaskStatus.INTERRUPTED, "service_interrupted", "插件停止时任务被中断")
            raise
        except Exception as exc:  # noqa: BLE001 - 任务边界必须收敛运行时失败
            logger.error(
                f"{self._task_label(task)}发生非预期处理异常：{type(exc).__name__}；"
                f"插件调用栈：{self._safe_traceback()}"
            )
            await self._finish_task(task, TaskStatus.FAILED, "processing_error", "字幕任务处理异常")
        finally:
            try:
                await self.filesystem.cleanup_task_directory(task.id)
            except Exception as exc:  # noqa: BLE001 - 临时目录清理失败不能覆盖任务结果
                logger.warning(f"{self._task_label(task)}的临时目录清理失败：{type(exc).__name__}")

    async def refresh_sources(self, manual: bool = True) -> list[SourceStatus]:
        """并发刷新三个字幕源且互不连带失败。"""

        tasks = [self.sources[source].refresh(manual=manual) for source in SubtitleSource]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        previous = {item.source: item for item in await self.store.list_source_statuses()}
        statuses: list[SourceStatus] = []
        for source, result in zip(SubtitleSource, results, strict=True):
            if isinstance(result, SourceStatus):
                status = result
            else:
                status = SourceStatus(
                    source=source,
                    enabled=bool(getattr(self.sources[source], "enabled", False)),
                    configured=bool(getattr(self.sources[source], "configured", True)),
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
            await self.store.save_source_status(status)
            statuses.append(status)
        return statuses

    def stop_sync(self, reason: str = "插件已停用，未完成任务已中断") -> None:
        """同步停止接收事件、取消 worker 并标记未完成任务。"""

        self._accepting = False
        self._generation += 1
        worker_loop: asyncio.AbstractEventLoop | None = None
        if self._worker and not self._worker.done():
            worker_loop = self._worker.get_loop()
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if worker_loop is current_loop:
                self._worker.cancel()
            elif worker_loop.is_running():
                worker_loop.call_soon_threadsafe(self._worker.cancel)
        self._active_paths.clear()
        self._active_manual.clear()
        self._active_items.clear()
        if hasattr(self.store, "mark_nonterminal_interrupted_sync"):
            self.store.mark_nonterminal_interrupted_sync(reason)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        cleanup_loop = loop or worker_loop
        if cleanup_loop and cleanup_loop.is_running():
            if cleanup_loop is loop:
                cleanup_loop.create_task(self._cleanup_runtime())
            else:
                asyncio.run_coroutine_threadsafe(self._cleanup_runtime(), cleanup_loop)

    async def _close_sources(self) -> None:
        """异步关闭全部字幕源。"""

        await asyncio.gather(*(source.close() for source in self.sources.values()), return_exceptions=True)

    async def _cleanup_runtime(self) -> None:
        """终止解包并关闭全部字幕源。"""

        await self.archive.cancel()
        await self._close_sources()

    async def shutdown(self, reason: str = "插件已停用，未完成任务已中断") -> None:
        """异步停止并等待运行资源释放。"""

        worker = self._worker
        self.stop_sync(reason)
        if worker and worker.get_loop() is asyncio.get_running_loop():
            await asyncio.gather(worker, return_exceptions=True)
        await self._cleanup_runtime()

    async def reset(self) -> None:
        """停止运行并清理插件数据目录及四个分区。"""

        await self.shutdown("插件数据重置，未完成任务已中断")
        await self.filesystem.clear_data_directory()
        await self.store.reset()
