"""字幕助手串行任务编排服务。"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from anyio import Path as AsyncPath

from app import log as app_log

from ..attribution import CandidateRecognizer, FileAttributor
from ..candidate import candidate_is_allowed, candidate_rank, has_simplified_chinese
from ..record import RecordCommitter
from ..schemas.base import elapsed_ms, utc_now
from ..schemas.candidate import SubtitleCandidate, TranslationType
from ..schemas.config import PluginConfig
from ..schemas.file import ExtractedSubtitle
from ..schemas.record import InventoryConsumeResult, MatchRecord, RecordStatus
from ..schemas.source import (
    CandidateHandle,
    DownloadedAsset,
    SourceCandidatePoolResult,
    SourceHealth,
    SourceRun,
    SourceRunStatus,
    SourceStatus,
    SubtitleSource,
)
from ..schemas.target import MediaType, PathMappingResolution, PathMappingSnapshot, SubtitleTarget
from ..schemas.task import (
    AttemptResult,
    CandidateAttemptReasonCode,
    ManualEnqueueResult,
    StageTrace,
    SubtitleTask,
    TaskStage,
    TaskStatus,
    TaskTrigger,
    TaskWorkItem,
)
from ..source import CandidatePool, SourceAdministration
from .attempt import (
    CandidateAttemptResult,
    CandidateAttemptService,
    CandidateAttemptSourcePort,
    FailureResultRetention,
)


class _TaskStorePort(Protocol):
    """字幕任务生命周期所需的持久化操作。"""

    async def list_tasks(self) -> list[SubtitleTask]:
        """读取全部字幕任务快照。"""

    async def save_task(self, task: SubtitleTask) -> None:
        """保存字幕任务快照。"""

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """按标识读取字幕任务快照。"""

    async def delete_task(self, task_id: str) -> bool:
        """删除字幕任务快照。"""

    async def list_source_statuses(self) -> list[SourceStatus]:
        """读取来源状态快照。"""

    async def save_source_status(self, status: SourceStatus) -> None:
        """保存来源状态快照。"""

    def mark_nonterminal_interrupted_sync(self, message: str) -> list[str]:
        """同步标记未完成任务为已中断。"""

    async def reset(self) -> None:
        """清理插件持久化分区。"""


class _TaskFilePort(Protocol):
    """字幕任务生命周期所需的文件操作。"""

    async def has_standard_subtitle(self, target: Path) -> Path | None:
        """查找目标关联的标准简中外挂字幕。"""

    async def make_task_directory(self, task_id: str) -> Path:
        """创建字幕任务临时目录。"""

    async def cleanup_task_directory(self, task_id: str) -> None:
        """清理字幕任务临时目录。"""

    async def clear_data_directory(self) -> None:
        """清理插件数据目录。"""

    async def target_directory_status(self, target: Path) -> tuple[bool, str | None]:
        """检查字幕目标目录是否可写。"""


class _TaskArchivePort(Protocol):
    """字幕任务候选尝试所需的归档操作。"""

    async def extract(
        self,
        asset: DownloadedAsset,
        output: Path,
        allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """解包候选下载结果。"""

    async def cancel(self) -> None:
        """终止当前归档解包。"""


class _TaskSourcePort(Protocol):
    """任务停止时所需的来源资源释放操作。"""

    async def close(self) -> None:
        """关闭来源运行态资源。"""


class _TaskTargetPort(Protocol):
    """任务人工下载所需的字幕目标路径解析能力。"""

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> PathMappingResolution:
        """解析整理历史目标的实际字幕路径。"""


class _UnavailableCandidatePool:
    """未由组合根注入候选池时的安全占位。"""

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        """拒绝在未完成装配的运行态执行来源查询。"""

        raise RuntimeError("来源候选池未注入")


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


@dataclass(frozen=True, slots=True)
class _SourceQueryFacts:
    """归一化来源查询详情中的缓存、分页和查询事实。"""

    cache_reused: bool = False
    cache_requested: bool = False
    cache_stored_at: Any | None = None
    page_count: int = 0
    pagination_incomplete: bool = False
    query: str = ""
    query_type: str = ""


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


class TaskOperations:
    """统一管理字幕任务的队列、worker、候选尝试与持久化快照。"""

    def __init__(
        self,
        store: _TaskStorePort,
        filesystem: _TaskFilePort,
        archive: _TaskArchivePort,
        matcher: CandidateRecognizer,
        sources: SourceAdministration | Mapping[SubtitleSource, _TaskSourcePort],
        config: PluginConfig,
        inventory: RecordCommitter,
        media_extensions: Sequence[str],
        attributor: FileAttributor | None = None,
        candidate_pool: CandidatePool | None = None,
        target_catalog: _TaskTargetPort | None = None,
        manage_resources: bool = True,
    ) -> None:
        """创建可注入依赖的字幕任务操作 facade。"""

        self._store = store
        self._filesystem = filesystem
        self._archive = archive
        self._matcher = matcher
        if attributor is not None:
            self._attribution = attributor
        elif callable(getattr(matcher, "attribute_requests", None)):
            self._attribution = cast(FileAttributor, matcher)
        else:
            raise TypeError("任务协调器必须注入文件归属 facade")
        self._sources = sources
        self._target_catalog = target_catalog
        self._config = config
        self._inventory = inventory
        self._media_extensions = frozenset(f".{value.lower().lstrip('.')}" for value in media_extensions)
        self._candidate_pool = candidate_pool or _UnavailableCandidatePool()
        self._manage_resources = manage_resources
        self._candidate_attempt = CandidateAttemptService(
            filesystem=self._filesystem,
            archive=self._archive,
            matcher=self._matcher,
            sources=cast(CandidatePool, self._candidate_pool),
            config=self._config,
            inventory=self._inventory,
            # 生产归属实现通过统一批量 facade 注入；仅提供旧式
            # attribute_file 的测试/宿主替身由候选模块在组合边界适配。
            attributor=self._attribution,
            source_adapters=cast(Mapping[SubtitleSource, CandidateAttemptSourcePort] | None, self._sources),
            task_label=self._task_label,
            candidate_label=self._candidate_label,
        )
        self._queue: asyncio.Queue[TaskWorkItem] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._active_paths: dict[str, str] = {}
        self._active_items: dict[str, TaskWorkItem] = {}
        self._active_manual: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._accepting = True
        self._generation = 0
        self._cleanup_started = False

    def _path_key(self, path: str | Path) -> str:
        """生成同一路径任务合并键。"""

        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _manual_key(session_id: str, candidate: SubtitleCandidate) -> str:
        """生成同会话、来源与候选的人工任务合并键。"""

        return f"{session_id}:{candidate.source.value}:{candidate.stable_key}"

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

        facts = TaskOperations._normalize_source_query_details(details)
        parts: list[str] = []
        if facts.cache_reused:
            parts.append(f"复用了缓存{f'（写入时间 {facts.cache_stored_at}）' if facts.cache_stored_at else ''}")
        if facts.cache_requested:
            parts.append("本次实际请求了字幕站")
        if facts.page_count > 0:
            parts.append(f"读取 {facts.page_count} 页")
        if facts.pagination_incomplete:
            parts.append("分页未完整读取")
        if facts.query:
            query_name = QUERY_TYPE_NAMES.get(facts.query_type, "查询词")
            parts.append(f"命中{query_name}“{facts.query}”")
        return "，".join(parts)

    @staticmethod
    def _normalize_source_query_details(details: Mapping[str, Any]) -> _SourceQueryFacts:
        """把结构化和旧版来源详情合并为一组查询事实。"""

        legacy_cache_hit = details.get("cache_hit")
        cache_reused = legacy_cache_hit is True
        cache_requested = legacy_cache_hit is False
        cache_stored_at = details.get("cache_stored_at")
        structured_cache = details.get("cache")
        if isinstance(structured_cache, list):
            cache_items = [item for item in structured_cache if isinstance(item, Mapping)]
            cache_hits = [item for item in cache_items if item.get("hit") is True]
            cache_requests = [
                item for item in cache_items if item.get("hit") is False or item.get("state") in {"miss", "invalid"}
            ]
            cache_reused = cache_reused or bool(cache_hits)
            cache_requested = cache_requested or bool(cache_requests)
            matched_query = str(details.get("matched_query") or "").strip()
            selected_hit = next(
                (item for item in cache_hits if item.get("query") == matched_query),
                None,
            ) or next(iter(cache_hits), None)
            if selected_hit is not None:
                cache_stored_at = selected_hit.get("stored_at") or cache_stored_at

        legacy_page_count = details.get("page_count")
        page_count = legacy_page_count if isinstance(legacy_page_count, int) and legacy_page_count > 0 else 0
        pagination_incomplete = details.get("pagination_complete") is False
        structured_pagination = details.get("pagination")
        if isinstance(structured_pagination, list):
            pagination_items = [item for item in structured_pagination if isinstance(item, Mapping)]
            structured_page_count = sum(
                item["pages_fetched"]
                for item in pagination_items
                if isinstance(item.get("pages_fetched"), int) and item["pages_fetched"] > 0
            )
            page_count = max(page_count, structured_page_count)
            pagination_incomplete = pagination_incomplete or any(
                item.get("complete") is False for item in pagination_items
            )

        query = str(details.get("matched_query") or details.get("query") or "").strip()
        query_type = str(details.get("query_type") or "").strip()
        return _SourceQueryFacts(
            cache_reused=cache_reused,
            cache_requested=cache_requested,
            cache_stored_at=cache_stored_at,
            page_count=page_count,
            pagination_incomplete=pagination_incomplete,
            query=query,
            query_type=query_type,
        )

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
                package_attribution_strategy=self._config.package_attribution_strategy,
            )
            await self._store.save_task(task)
            app_log.logger.info(f"{self._task_label(task)}已创建，目标文件为“{task.target_path}”")
            item.task_id = task.id
            self._active_paths[key] = task.id
            self._active_items[task.id] = item
            await self._queue.put(item)
            self._ensure_worker()
            return task.id

    async def list_tasks(self) -> list[SubtitleTask]:
        """读取全部字幕任务快照。"""

        return await self._store.list_tasks()

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """按标识读取字幕任务快照。"""

        return await self._store.get_task(task_id)

    async def delete_task(self, task_id: str) -> bool:
        """删除指定字幕任务快照。"""

        return await self._store.delete_task(task_id)

    async def enqueue_manual(self, item: TaskWorkItem) -> ManualEnqueueResult | None:
        """把用户选定候选提交到单 worker，并合并同会话同候选的非终态任务。"""

        if not self._accepting or item.manual_handle is None or not item.manual_session_id:
            return None
        candidate = item.manual_handle.candidate
        manual_key = self._manual_key(item.manual_session_id, candidate)
        async with self._lock:
            existing_id = self._active_manual.get(manual_key)
            if existing_id:
                existing_task = await self._store.get_task(existing_id)
                if existing_task is not None and not existing_task.is_terminal:
                    return ManualEnqueueResult(task=existing_task, reused=True)
                self._active_manual.pop(manual_key, None)
                self._active_items.pop(existing_id, None)
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
                package_attribution_strategy=self._config.package_attribution_strategy,
                manual_source=candidate.source,
                manual_candidate_key=candidate.stable_key,
                manual_candidate_summary=candidate.model_dump(mode="json"),
                actual_search_query=item.actual_search_query,
            )
            await self._store.save_task(task)
            app_log.logger.info(
                f"{self._task_label(task)}已创建，将下载{self._candidate_label(candidate)}，"
                f"目标文件为“{task.target_path}”"
            )
            item.task_id = task.id
            self._active_manual[manual_key] = task.id
            self._active_items[task.id] = item
            await self._queue.put(item)
            self._ensure_worker()
            return ManualEnqueueResult(task=task.model_copy(deep=True), reused=False)

    async def _worker_loop(self) -> None:
        """串行消费运行期队列。"""

        while self._accepting:
            item = await self._queue.get()
            try:
                task = await self._store.get_task(item.task_id) if item.task_id else None
                if task is not None:
                    await self._process(task, item)
                else:
                    app_log.logger.error(
                        f"字幕任务 {item.task_id or '未知'} 无法开始处理：持久化记录不存在，"
                        f"触发方式为“{'人工选择字幕' if item.manual_handle else '媒体整理事件'}”"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 必须隔离单项任务异常
                app_log.logger.error(
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
                        manual_key = self._manual_key(item.manual_session_id, item.manual_handle.candidate)
                        manual_task_id = self._active_manual.get(manual_key)
                        if manual_task_id == item.task_id:
                            self._active_manual.pop(manual_key, None)
                        if item.task_id:
                            self._active_items.pop(item.task_id, None)

    async def _task_for_path(self, path_key: str) -> SubtitleTask | None:
        """按运行期路径键读取当前任务。"""

        async with self._lock:
            task_id = self._active_paths.get(path_key)
        if task_id is None:
            return None
        return await self._store.get_task(task_id)

    async def _save(self, task: SubtitleTask) -> None:
        """持久化任务快照。"""

        await self._store.save_task(task)

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
        app_log.logger.debug(f"{self._task_label(task)}进入“{STAGE_NAMES[stage]}”阶段")

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
        log = app_log.logger.warning if status is TaskStatus.FAILED else app_log.logger.info
        status_name = {
            TaskStatus.SUCCESS: "成功",
            TaskStatus.SKIPPED: "已跳过",
            TaskStatus.FAILED: "失败",
            TaskStatus.INTERRUPTED: "已中断",
        }.get(status, status.value)
        log(f"{self._task_label(task)}处理{status_name}：{reason_message}")

    async def _preflight(self, task: SubtitleTask, item: TaskWorkItem) -> bool:
        """完成本地文件、扩展名和已有字幕前置检查。"""

        context = item.context
        if context.target_storage != "local":
            await self._finish_task(task, TaskStatus.SKIPPED, "non_local_storage", "目标不是本地存储")
            return False
        if context.target_type != "file":
            await self._finish_task(task, TaskStatus.SKIPPED, "unsupported_media_container", "目标不是文件型媒体")
            return False
        extension = str(context.target_extension or Path(context.target_path).suffix.lstrip(".")).lower()
        if f".{extension}" not in self._media_extensions:
            await self._finish_task(
                task, TaskStatus.SKIPPED, "unsupported_media_format", "目标格式不在宿主媒体格式集合中"
            )
            return False
        task.target_file_exists = await AsyncPath(task.target_path).is_file()
        await self._save(task)
        if not task.target_file_exists:
            await self._finish_task(task, TaskStatus.FAILED, "target_missing", "整理目标文件不存在")
            return False
        subtitle = await self._filesystem.has_standard_subtitle(Path(task.target_path))
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
        if self._target_catalog is None:
            task.history_target_path = history_path
            task.target_path = history_path
            task.matched_path_mapping = None
            task.target_file_exists = await AsyncPath(task.target_path).is_file()
            item.context = item.context.model_copy(
                update={"target_path": task.target_path, "target_file_name": Path(task.target_path).name}
            )
            await self._save(task)
            return
        resolution = self._target_catalog.resolve_actual_subtitle_path(
            item.context.model_copy(update={"target_path": history_path})
        )
        task.history_target_path = resolution.original_path
        task.target_path = resolution.resolved_path
        task.matched_path_mapping = (
            PathMappingSnapshot(
                source_prefix=Path(resolution.mapping.source_prefix),
                target_prefix=Path(resolution.mapping.target_prefix),
            )
            if resolution.mapping is not None
            else None
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
            app_log.logger.info(
                f"{self._task_label(task)}执行人工下载时应用整理历史路径映射："
                f"历史路径为“{resolution.original_path}”，实际路径为“{resolution.resolved_path}”"
            )

    async def _search_sources(self, task: SubtitleTask, item: TaskWorkItem) -> list[CandidateHandle]:
        """查询共享来源候选池并汇总通过自动规则与当前目标匹配的候选。"""

        await self._set_stage(task, TaskStage.SEARCH)
        batch = await self._candidate_pool.query(item.context)
        configured_sources = self._sources.keys() if isinstance(self._sources, Mapping) else ()
        sources_to_audit = [
            source for source in SubtitleSource if source in batch.sources or source in configured_sources
        ]
        handles: list[CandidateHandle] = []
        for source in sources_to_audit:
            result = batch.sources.get(source)
            if result is None:
                run = SourceRun(
                    source=source,
                    status=SourceRunStatus.ERROR,
                    error_summary=f"{SOURCE_NAMES[source]}候选池运行结果缺失",
                )
                await self._save_source_failure(source, run.error_summary)
                task.source_runs.append(run)
                app_log.logger.error(f"{self._task_label(task)}查询{SOURCE_NAMES[source]}时缺少共享候选池运行结果")
                continue

            raw_count = max(len(result.candidates), int(result.raw_count))
            candidates, rejection_summary = self._admit_automatic_candidates(
                result,
                item.context,
            )
            admitted_count = len(candidates)
            if result.download_locator_excluded_count:
                rejection_summary["download_locator"] = result.download_locator_excluded_count
            if result.status == "disabled":
                run_status = SourceRunStatus.DISABLED
            elif result.status == "unconfigured":
                run_status = SourceRunStatus.UNCONFIGURED
            elif result.status == "limited" or (result.status == "partial" and not result.candidates):
                run_status = SourceRunStatus.LIMITED
            elif result.status == "error":
                run_status = SourceRunStatus.ERROR
            else:
                run_status = SourceRunStatus.EMPTY
            if result.status == "error":
                await self._save_source_failure(
                    source,
                    result.error_summary,
                    False,
                    result.duration_ms,
                )
            elif result.status == "limited" or (result.status == "partial" and not result.candidates):
                await self._save_source_failure(
                    source,
                    result.error_summary,
                    True,
                    result.duration_ms,
                )
            elif result.status == "partial" and result.candidates:
                await self._save_source_success(source, result.details, result.duration_ms)
            elif result.status == "unconfigured":
                await self._save_source_unavailable(
                    source,
                    SOURCE_SKIP_REASONS.get(result.skip_reason or "", result.error_summary or "来源当前不可调用"),
                    result.duration_ms,
                )
            elif result.skip_reason is None and result.status not in {"disabled", "unconfigured"}:
                await self._save_source_success(source, result.details, result.duration_ms)
            media_matched_count = 0
            for handle in candidates:
                normalized = self._matcher.normalize_candidate(handle.candidate, item.context, item.match_context)
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
                    error_summary=(None if result.status == "partial" and result.candidates else result.error_summary),
                    details=result.details,
                )
            )
            log = (
                app_log.logger.warning
                if run_status in {SourceRunStatus.ERROR, SourceRunStatus.LIMITED}
                or self._has_incomplete_pagination(result.details)
                else app_log.logger.info
            )
            query_summary = self._source_query_summary(result.details)
            prefix = f"{self._task_label(task)}的 {SOURCE_NAMES[source]} 搜索"
            if run_status is SourceRunStatus.DISABLED:
                conclusion = f"未执行：{SOURCE_STATUS_REASONS[SourceRunStatus.DISABLED]}"
            elif run_status is SourceRunStatus.UNCONFIGURED:
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
            elif self._has_incomplete_pagination(result.details):
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
            app_log.logger.info(
                f"{self._task_label(task)}已汇总 {len(sources_to_audit)} 个字幕来源的处理结果，"
                f"共获得 {len(handles)} 个适用于当前目标的候选"
            )
        else:
            app_log.logger.warning(
                f"{self._task_label(task)}已汇总 {len(sources_to_audit)} 个字幕来源的处理结果，但没有获得适用于当前目标的候选"
            )
        return handles

    def _admit_automatic_candidates(
        self,
        result: SourceCandidatePoolResult,
        context: SubtitleTarget,
    ) -> tuple[list[CandidateHandle], dict[str, int]]:
        """在来源候选池之后执行自动语言、翻译和内容范围准入。"""

        candidates: list[CandidateHandle] = []
        rejected: dict[str, int] = {}
        for handle in result.candidates:
            candidate = handle.candidate.model_copy(deep=True)
            flags = candidate.metadata.get("language_flags")
            if not has_simplified_chinese(
                candidate.source,
                candidate.language,
                flags if isinstance(flags, Mapping) else None,
            ):
                rejected["language"] = rejected.get("language", 0) + 1
                continue
            if not candidate_is_allowed(candidate, self._config.allow_machine_translation):
                reason = (
                    "machine_translation"
                    if candidate.translation_type in {TranslationType.MACHINE, TranslationType.AI}
                    else "foreign_parts_only"
                )
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            candidate.exact_id_match = self._has_exact_media_identity(candidate, context)
            candidates.append(CandidateHandle(candidate=candidate, opaque=handle.opaque))
        return candidates, rejected

    @classmethod
    def _has_exact_media_identity(cls, candidate: SubtitleCandidate, context: SubtitleTarget) -> bool:
        """判断候选是否携带与当前目标相同的 TMDB 或 IMDb 身份。"""

        if context.tmdb_id is not None and candidate.tmdb_id == context.tmdb_id:
            return True
        return bool(
            context.imdb_id
            and candidate.imdb_id
            and cls._normalized_imdb_id(candidate.imdb_id) == cls._normalized_imdb_id(context.imdb_id)
        )

    @classmethod
    def _has_incomplete_pagination(cls, details: Mapping[str, Any]) -> bool:
        """判断共享候选池轨迹是否包含未完整读取的分页。"""

        return cls._normalize_source_query_details(details).pagination_incomplete

    def _source_status_snapshot(self, source: SubtitleSource) -> SourceStatus | None:
        """读取来源 facade 提供的当前配置与运行详情。"""

        snapshot = getattr(self._sources, "status_snapshot", None)
        if callable(snapshot):
            return cast(SourceStatus, snapshot(source))
        if isinstance(self._sources, Mapping):
            adapter = self._sources.get(source)
            runtime_details = getattr(adapter, "runtime_details", dict)
            return SourceStatus(
                source=source,
                enabled=bool(getattr(adapter, "enabled", False)),
                configured=bool(getattr(adapter, "configured", adapter is not None)),
                details=runtime_details() if callable(runtime_details) else {},
            )
        return None

    async def _save_source_success(
        self,
        source: SubtitleSource,
        details: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源成功搜索的非敏感状态。"""

        existing = {item.source: item for item in await self._store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        snapshot = self._source_status_snapshot(source)
        if snapshot is not None:
            status.enabled = snapshot.enabled
            status.configured = snapshot.configured
            status.details = {**status.details, **snapshot.details}
        status.health = SourceHealth.HEALTHY
        status.last_checked_at = utc_now()
        status.last_success_at = status.last_checked_at
        status.last_duration_ms = duration_ms
        status.details = {**status.details, **(details or {})}
        await self._store.save_source_status(status)

    async def _save_source_failure(
        self,
        source: SubtitleSource,
        summary: str | None,
        limited: bool = False,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源失败或限流的脱敏状态。"""

        existing = {item.source: item for item in await self._store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        snapshot = self._source_status_snapshot(source)
        if snapshot is not None:
            status.enabled = snapshot.enabled
            status.configured = snapshot.configured
            status.details = {**status.details, **snapshot.details}
        status.health = SourceHealth.LIMITED if limited else SourceHealth.ERROR
        status.last_checked_at = utc_now()
        status.last_error_at = status.last_checked_at
        status.last_error_summary = summary
        status.last_duration_ms = duration_ms
        await self._store.save_source_status(status)

    async def _save_source_unavailable(
        self,
        source: SubtitleSource,
        summary: str,
        duration_ms: int | None = None,
    ) -> None:
        """保存来源当前不可调用且未发出搜索请求的状态。"""

        existing = {item.source: item for item in await self._store.list_source_statuses()}.get(source)
        status = existing or SourceStatus(source=source)
        snapshot = self._source_status_snapshot(source)
        if snapshot is not None:
            status.enabled = snapshot.enabled
            status.configured = False
            status.details = {**status.details, **snapshot.details}
        else:
            status.enabled = False
            status.configured = False
        status.health = SourceHealth.DISABLED
        status.last_checked_at = utc_now()
        status.last_error_at = status.last_checked_at
        status.last_error_summary = summary
        status.last_duration_ms = duration_ms
        await self._store.save_source_status(status)

    async def _consume_inventory(self, task: SubtitleTask, item: TaskWorkItem) -> InventoryConsumeResult:
        """在外部搜索前查询并消费精确库存字幕。"""

        await self._set_stage(task, TaskStage.INVENTORY)
        result = await self._inventory.consume(
            item.context,
            task.id,
            target_history_id=task.target_history_id,
            history_target_path=task.history_target_path,
            matched_path_mapping=task.matched_path_mapping,
            target_file_exists=task.target_file_exists,
        )
        records = self._normalize_records(result.records or result.record)
        task.inventory_result = {
            "matched": bool(records),
            "record_id": records[0].id if records else None,
            "record_ids": [record.id for record in records],
            "warning": result.warning,
        }
        if result.warning:
            task.warning_count += 1
            task.warning_summaries.append(result.warning)
        try:
            await self._save(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not records:
                raise
            app_log.logger.error(
                f"{self._task_label(task)}库存结果快照保存失败，已提交文件事实仍将继续发布；异常类型为 {type(exc).__name__}"
            )
        if records:
            log = app_log.logger.warning if result.warning else app_log.logger.info
            log(f"{self._task_label(task)}命中 {len(records)} 条字幕库存记录，字幕已写入媒体目录")
        elif result.warning:
            app_log.logger.warning(f"{self._task_label(task)}查询字幕库存时出现警告：{result.warning}")
        else:
            app_log.logger.info(f"{self._task_label(task)}没有找到对应的暂存字幕，将继续查询字幕源")
        return result

    @staticmethod
    def _normalize_records(value: MatchRecord | Sequence[MatchRecord] | None) -> list[MatchRecord]:
        """把单条或多条业务返回统一为逐文件匹配记录列表。"""

        if value is None:
            return []
        if isinstance(value, MatchRecord):
            return [value]
        return [record for record in value if isinstance(record, MatchRecord)]

    @staticmethod
    def _normalized_imdb_id(value: str | None) -> str | None:
        """规范化 IMDb 编号用于媒体身份比较。"""

        if not value:
            return None
        normalized = value.strip().lower().removeprefix("tt").lstrip("0")
        return normalized or "0"

    async def _try_candidate(
        self,
        task: SubtitleTask,
        item: TaskWorkItem,
        handle: CandidateHandle,
    ) -> CandidateAttemptResult:
        """委托候选尝试模块处理单个候选并在协调器记录结论。"""

        retention = (
            FailureResultRetention.PRESERVE if item.manual_handle is not None else FailureResultRetention.DISCARD
        )
        active_stage = TaskStage.DOWNLOAD

        async def on_stage(stage: TaskStage) -> None:
            """推进候选阶段并由协调器持久化阶段快照。"""

            nonlocal active_stage
            active_stage = stage
            await self._set_stage(task, stage)

        result = await self._candidate_attempt.attempt(
            task,
            item.context,
            handle,
            retention,
            on_stage=on_stage,
        )
        await self._finalize_candidate_attempt(task, handle.candidate, result, active_stage)
        if result.attempt.result is AttemptResult.INTERRUPTED:
            raise asyncio.CancelledError
        return result

    async def _finalize_candidate_attempt(
        self,
        task: SubtitleTask,
        candidate: SubtitleCandidate,
        result: CandidateAttemptResult,
        active_stage: TaskStage,
    ) -> None:
        """追加候选尝试、保存任务快照并输出一次候选漏斗结论。"""

        task.candidate_attempts.append(result.attempt)
        written_count = result.attempt.written_count
        try:
            await self._save(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if written_count == 0:
                raise
            app_log.logger.error(
                f"{self._task_label(task)}候选结果快照保存失败，已提交文件事实仍将继续发布；"
                f"异常类型为 {type(exc).__name__}"
            )
        attempt_number = len(task.candidate_attempts)
        attempt_result = result.attempt.result
        error_summary = result.attempt.error_summary
        log = (
            app_log.logger.warning
            if attempt_result is not AttemptResult.SUCCESS or error_summary and written_count > 0
            else app_log.logger.info
        )
        if attempt_result is AttemptResult.SUCCESS:
            message = (
                f"{self._task_label(task)}第 {attempt_number} 次候选尝试成功："
                f"{self._candidate_label(candidate)}按“{task.package_attribution_strategy.value}”处理，"
                f"解包 {result.attempt.extracted_count} 个，当前目标 {result.attempt.current_target_count} 个，"
                f"其他季集 {result.attempt.same_media_other_episode_count} 个，"
                f"归属不明确 {result.attempt.ambiguous_count} 个，"
                f"其他媒体排除 {result.attempt.other_media_count} 个；"
                f"落盘 {written_count} 个、暂存 {result.attempt.staged_count} 个、"
                f"未匹配 {result.attempt.unmatched_count} 个"
            )
        else:
            message = (
                f"{self._task_label(task)}第 {attempt_number} 次候选尝试未成功："
                f"{self._candidate_label(candidate)}在“{STAGE_NAMES[active_stage]}”阶段结束，"
                f"原因是“{error_summary or ATTEMPT_RESULT_NAMES[attempt_result]}”；"
                f"解包 {result.attempt.extracted_count} 个，当前目标 {result.attempt.current_target_count} 个，"
                f"其他季集 {result.attempt.same_media_other_episode_count} 个，"
                f"归属不明确 {result.attempt.ambiguous_count} 个，"
                f"其他媒体排除 {result.attempt.other_media_count} 个；"
                f"落盘 {written_count} 个、暂存 {result.attempt.staged_count} 个、"
                f"未匹配 {result.attempt.unmatched_count} 个"
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
                candidate_result = await self._try_candidate(task, item, item.manual_handle)
                records = self._normalize_records(candidate_result.records)
                if records:
                    await self._finish_task(task, TaskStatus.SUCCESS, "subtitle_written", "人工选择的字幕已落盘")
                else:
                    reason = candidate_result.attempt.error_summary
                    if candidate_result.reason_code is not None:
                        match candidate_result.reason_code:
                            case CandidateAttemptReasonCode.MANUAL_CANDIDATE_FAILED:
                                reason_code = candidate_result.reason_code.value
                            case CandidateAttemptReasonCode.TARGET_DIRECTORY_UNAVAILABLE:
                                reason_code = candidate_result.reason_code.value
                            case CandidateAttemptReasonCode.SUBTITLE_DESTINATION_CONFLICT:
                                reason_code = candidate_result.reason_code.value
                            case CandidateAttemptReasonCode.UNSUPPORTED_FORMAT:
                                reason_code = candidate_result.reason_code.value
                            case CandidateAttemptReasonCode.CANDIDATE_MISSING_TARGET_SUBTITLE:
                                reason_code = candidate_result.reason_code.value
                            case _:
                                raise AssertionError("未知候选尝试原因码")
                    else:
                        reason_code = CandidateAttemptReasonCode.MANUAL_CANDIDATE_FAILED.value
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
            inventory_records = self._normalize_records(inventory.records or inventory.record)
            if inventory_records:
                first_record = inventory_records[0]
                task.result_source = first_record.source
                task.result_package_scope = first_record.package_scope
                task.result_format = first_record.format
                task.final_subtitle_path = first_record.final_subtitle_path
                task.record_ids.extend(record.id for record in inventory_records)
                task.record_counts[RecordStatus.MATCHED.value] = task.record_counts.get(
                    RecordStatus.MATCHED.value,
                    0,
                ) + len(inventory_records)
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
                    self._config.format_priority,
                    [source.value for source in self._config.source_priority],
                ),
            )
            for handle in ordered[: self._config.max_candidate_attempts]:
                candidate_result = await self._try_candidate(task, item, handle)
                records = self._normalize_records(candidate_result.records)
                if records:
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
                    f"已达到最大候选尝试数 {self._config.max_candidate_attempts}；"
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
            app_log.logger.error(
                f"{self._task_label(task)}发生非预期处理异常：{type(exc).__name__}；"
                f"插件调用栈：{self._safe_traceback()}"
            )
            await self._finish_task(task, TaskStatus.FAILED, "processing_error", "字幕任务处理异常")
        finally:
            try:
                await self._filesystem.cleanup_task_directory(task.id)
            except Exception as exc:  # noqa: BLE001 - 临时目录清理失败不能覆盖任务结果
                app_log.logger.warning(f"{self._task_label(task)}的临时目录清理失败：{type(exc).__name__}")

    async def refresh_sources(self, manual: bool = True) -> list[SourceStatus]:
        """并发刷新三个字幕源且互不连带失败。"""

        refresher = getattr(self._sources, "refresh", None)
        if callable(refresher):
            return await refresher(manual=manual)
        return []

    def stop_sync(self, reason: str = "插件已停用，未完成任务已中断") -> None:
        """同步停止接收事件、取消 worker 并标记未完成任务。"""

        if not self._accepting:
            return
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
        mark_interrupted = getattr(self._store, "mark_nonterminal_interrupted_sync", None)
        if callable(mark_interrupted):
            cast(Callable[[str], object], mark_interrupted)(reason)
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

        closer = getattr(self._sources, "close", None)
        if callable(closer):
            await closer()
            return
        source_map = cast(Mapping[SubtitleSource, _TaskSourcePort], self._sources)
        await asyncio.gather(*(source.close() for source in source_map.values()), return_exceptions=True)

    async def _cleanup_runtime(self) -> None:
        """终止解包并关闭全部字幕源。"""

        if self._cleanup_started:
            return
        self._cleanup_started = True
        if not self._manage_resources:
            return
        await self._archive.cancel()
        await self._close_sources()
        closer = getattr(self._candidate_pool, "close", None)
        if callable(closer):
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - 查询缓存关闭失败不能覆盖任务结果
                app_log.logger.error(f"共享字幕候选池缓存关闭失败：{type(exc).__name__}")

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
        await self._filesystem.clear_data_directory()
        await self._store.reset()
