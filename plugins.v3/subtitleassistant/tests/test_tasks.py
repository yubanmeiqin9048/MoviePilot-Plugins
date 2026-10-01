"""整理事件与串行任务协调器并发、库存和停止语义测试。"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anyio import Path as AsyncPath

from app.sdk.events import Event
from app.plugins.subtitleassistant.attribution import AttributionService
from app.plugins.subtitleassistant.plugin import PluginRuntime
from app.plugins.subtitleassistant.file import ArchiveExtractor, SubtitleFiles
from app.plugins.subtitleassistant.record import RecordCommitter
from app.plugins.subtitleassistant.target import TargetCatalog
from app.plugins.subtitleassistant.schemas.attribution import (
    AttributionEvidence,
    CandidateAttributionSnapshot,
    CandidateMatchContext,
    FileAttributionEvidence,
    FileAttributionMethod,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from app.plugins.subtitleassistant.schemas.base import utc_now
from app.plugins.subtitleassistant.schemas.candidate import (
    PackageScope,
    SubtitleCandidate,
    TranslationType,
)
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.event import SubtitleWrittenEvent, SubtitleWrittenOperation
from app.plugins.subtitleassistant.schemas.file import ExtractedSubtitle
from app.plugins.subtitleassistant.schemas.record import (
    CommittedFileFact,
    FileLocation,
    InventoryConsumeResult,
    MatchRecord,
    RecordStatus,
)
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    SourceSearchBatch,
    DownloadedAsset,
    MoviePilotDownloadHandle,
    OpenSubtitlesDownloadHandle,
    AssrtDownloadHandle,
    SourceHealth,
    SourceSearchResult,
    SourceStatus,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, PathMapping, PathMappingResolution, SubtitleTarget
from app.plugins.subtitleassistant.schemas.task import (
    AttemptResult,
    SubtitleTask,
    TaskStatus,
    TaskTrigger,
    TaskWorkItem,
)
from app.plugins.subtitleassistant.source import SourceAdministration
from app.plugins.subtitleassistant.task import TaskOperations
from app.plugins.subtitleassistant.task import service as task_service
from app.schemas.types import EventType

pytestmark = pytest.mark.anyio


class _TaskStore:
    """任务协调器使用的可观测内存存储。"""

    def __init__(self) -> None:
        """初始化任务、记录、来源状态和调用记录。"""

        self.tasks: dict[str, SubtitleTask] = {}
        self.records: dict[str, MatchRecord] = {}
        self.statuses: dict[SubtitleSource, SourceStatus] = {}
        self.save_task_calls: list[str] = []
        self.interrupt_messages: list[str] = []

    async def save_task(self, task: SubtitleTask) -> None:
        """保存任务快照。"""

        self.tasks[task.id] = task.model_copy(deep=True)
        self.save_task_calls.append(task.id)

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """返回任务快照。"""

        task = self.tasks.get(task_id)
        return task.model_copy(deep=True) if task else None

    async def list_tasks(self) -> list[SubtitleTask]:
        """返回全部任务快照。"""

        return [task.model_copy(deep=True) for task in self.tasks.values()]

    async def delete_task(self, task_id: str) -> bool:
        """删除指定任务快照。"""

        return self.tasks.pop(task_id, None) is not None

    async def save_record(self, record: MatchRecord) -> None:
        """保存匹配记录快照。"""

        self.records[record.id] = record.model_copy(deep=True)

    async def list_source_statuses(self) -> list[SourceStatus]:
        """返回来源状态快照。"""

        return [item.model_copy(deep=True) for item in self.statuses.values()]

    async def save_source_status(self, status: SourceStatus) -> None:
        """保存来源状态快照。"""

        self.statuses[status.source] = status.model_copy(deep=True)

    def mark_nonterminal_interrupted_sync(self, message: str) -> list[str]:
        """同步把内存中的等待和处理任务标记为中断。"""

        self.interrupt_messages.append(message)
        changed: list[str] = []
        for task in self.tasks.values():
            if task.status in {TaskStatus.QUEUED, TaskStatus.PROCESSING}:
                task.status = TaskStatus.INTERRUPTED
                task.reason_code = "service_interrupted"
                task.reason_message = message
                task.finished_at = utc_now()
                changed.append(task.id)
        return changed


class _TaskFileSystem:
    """只在临时目录操作的任务文件系统替身。"""

    def __init__(self, root: Path) -> None:
        """绑定任务临时根目录。"""

        self.root = root
        self.cleanup_calls: list[str] = []

    async def has_standard_subtitle(self, _target: Path) -> None:
        """默认没有已有标准字幕。"""

        return

    async def make_task_directory(self, task_id: str) -> Path:
        """创建任务临时目录。"""

        path = AsyncPath(self.root / task_id)
        await path.mkdir(parents=True, exist_ok=True)
        return Path(path)

    async def cleanup_task_directory(self, task_id: str) -> None:
        """记录任务目录清理。"""

        self.cleanup_calls.append(task_id)

    async def write_media_subtitle(self, source: Path, target: Path) -> Path:
        """返回标准目标字幕路径。"""

        return target.with_name(f"{target.stem}.chi.zh-cn{source.suffix.lower()}")

    async def save_plugin_file(self, _source: Path, record_id: str, status: RecordStatus) -> str:
        """返回测试用插件相对路径。"""

        return f"{status.value}/{record_id}.srt"

    async def clear_data_directory(self) -> None:
        """测试替身无需清理实际数据目录。"""

        return


class _TaskArchive:
    """记录解包和取消的归档替身。"""

    def __init__(self, extracted: list[Path] | None = None) -> None:
        """设置每次解包返回的文件。"""

        self.extracted = list(extracted or [])
        self.extract_calls: list[str] = []
        self.cancel_calls = 0

    async def extract(
        self,
        asset: DownloadedAsset,
        _output: Path,
        _allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """记录解包并返回预设文件。"""

        self.extract_calls.append(asset.file_name)
        return [
            ExtractedSubtitle(
                physical_path=path,
                logical_source_path=Path(path.name),
                is_direct_file=False,
            )
            for path in self.extracted
        ]

    async def cancel(self) -> None:
        """记录当前解包取消。"""

        self.cancel_calls += 1


class _TaskMatcher(AttributionService):
    """默认保留候选的媒体匹配替身。"""

    def normalize_candidate(
        self,
        candidate: SubtitleCandidate,
        _context: SubtitleTarget,
        _match_context: CandidateMatchContext | None,
    ) -> SubtitleCandidate:
        """原样返回候选。"""

        return candidate

    def candidate_snapshot(self, candidate: SubtitleCandidate) -> CandidateAttributionSnapshot:
        """返回候选自身的单集归属快照。"""

        return CandidateAttributionSnapshot(
            tmdb_id=candidate.tmdb_id,
            seasons=[candidate.season] if candidate.season is not None else [1],
            episodes=[candidate.episode] if candidate.episode is not None else [1],
            package_scope=candidate.package_scope,
        )

    async def attribute_file(
        self,
        logical_source_path: Path,
        context: SubtitleTarget,
        _snapshot: CandidateAttributionSnapshot,
        _strategy: Any,
    ) -> FileAttributionEvidence:
        """把测试字幕归属到当前目标。"""

        return FileAttributionEvidence(
            logical_source_path=logical_source_path,
            method=FileAttributionMethod.TRUST_PACKAGE,
            belongs_to_target_media=True,
            media_type=context.media_type,
            tmdb_id=context.tmdb_id,
            imdb_id=context.imdb_id,
            season=context.season,
            episode=context.episode,
            season_evidence=AttributionEvidence.PATH,
            episode_evidence=AttributionEvidence.PATH,
        )


class _EpisodeMatcher(_TaskMatcher):
    """按逻辑文件名返回指定集号的包内字幕归属。"""

    def __init__(self, episodes: dict[str, int]) -> None:
        """保存逻辑文件名到集号的映射。"""

        self._episodes = dict(episodes)

    async def attribute_file(
        self,
        logical_source_path: Path,
        context: SubtitleTarget,
        _snapshot: CandidateAttributionSnapshot,
        _strategy: Any,
    ) -> FileAttributionEvidence:
        """把测试字幕归属到目标媒体及指定集号。"""

        return FileAttributionEvidence(
            logical_source_path=logical_source_path,
            method=FileAttributionMethod.TRUST_PACKAGE,
            belongs_to_target_media=True,
            media_type=context.media_type,
            tmdb_id=context.tmdb_id,
            imdb_id=context.imdb_id,
            season=context.season,
            episode=self._episodes[str(logical_source_path)],
            season_evidence=AttributionEvidence.PATH,
            episode_evidence=AttributionEvidence.PATH,
        )


class _TaskInventory:
    """返回预设消费结果的库存替身。"""

    def __init__(self, result: InventoryConsumeResult | None = None) -> None:
        """保存预设结果。"""

        self.result = result or InventoryConsumeResult()
        self.consume_calls: list[tuple[Path, str]] = []

    async def consume(
        self,
        context: SubtitleTarget,
        task_id: str,
        **_kwargs: Any,
    ) -> InventoryConsumeResult:
        """记录库存查询并返回独立结果。"""

        self.consume_calls.append((context.target_path, task_id))
        return self.result

    async def add(self, _record: MatchRecord) -> None:
        """当前测试不维护额外库存索引。"""

        return


DownloadCallback = Callable[[CandidateHandle, Path], Awaitable[DownloadedAsset]]


class _TaskSource:
    """可注入搜索和下载协程的字幕源替身。"""

    def __init__(
        self,
        source: SubtitleSource,
        download_callback: DownloadCallback | None = None,
    ) -> None:
        """创建启用且已配置的来源。"""

        self.source = source
        self.enabled = True
        self.configured = True
        self._download_callback = download_callback
        self.close_calls = 0

    def plan(self, _context: SubtitleTarget, _custom_query: str | None) -> Any:
        """提供不参与本测试的来源查询计划边界。"""

        raise AssertionError("自动任务测试不应直接调用来源查询计划")

    async def execute(self, _query: Any, _page_number: int) -> Any:
        """提供不参与本测试的来源分页边界。"""

        raise AssertionError("自动任务测试不应直接调用来源分页")

    def normalize(self, _execution: Any, _query: Any) -> Any:
        """提供不参与本测试的来源归一化边界。"""

        raise AssertionError("自动任务测试不应直接调用来源归一化")

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """调用预设下载或返回内存定位。"""

        if self._download_callback:
            return await self._download_callback(handle, directory)
        return DownloadedAsset(path=directory / "candidate.zip", file_name="candidate.zip")

    async def close(self) -> None:
        """记录来源关闭。"""

        self.close_calls += 1

    def runtime_details(self) -> dict[str, Any]:
        """返回空的安全运行观测。"""

        return {}


class _TaskCandidatePool:
    """为自动任务 seam 提供可观测的共享候选池替身。"""

    def __init__(
        self,
        sources: dict[SubtitleSource, _TaskSource],
        results: dict[SubtitleSource, SourceSearchResult] | None = None,
        query_callback: Callable[[SubtitleTarget], Awaitable[SourceSearchBatch]] | None = None,
    ) -> None:
        """保存来源结果与可选的批量查询行为。"""

        self.sources = sources
        self.results = dict(results or {})
        self.query_callback = query_callback
        self.query_calls: list[SubtitleTarget] = []

    async def query(
        self,
        context: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> SourceSearchBatch:
        """记录一次批量查询并返回逐来源候选池运行结果。"""

        del custom_queries
        self.query_calls.append(context)
        if self.query_callback is not None:
            return await self.query_callback(context)
        results = {
            source: self.results.get(
                source,
                SourceSearchResult(source=source, status="success"),
            )
            for source in self.sources
        }
        return SourceSearchBatch(sources=results)

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """通过统一来源服务边界转交测试下载。"""

        return await self.sources[handle.candidate.source].download(handle, directory)

    def status_snapshot(self, source: SubtitleSource) -> SourceStatus:
        """返回测试来源的当前配置。"""

        adapter = self.sources.get(source)
        return SourceStatus(
            source=source,
            enabled=adapter.enabled if adapter is not None else False,
            configured=adapter.configured if adapter is not None else False,
            details=adapter.runtime_details() if adapter is not None else {},
        )

    async def close(self) -> None:
        """关闭统一来源服务持有的测试来源。"""

        await asyncio.gather(*(source.close() for source in self.sources.values()))


class _CaptureCoordinator:
    """记录 TransferComplete 入队任务。"""

    def __init__(self) -> None:
        """创建空入队列表。"""

        self.items: list[TaskWorkItem] = []

    async def enqueue(self, item: TaskWorkItem) -> str:
        """记录任务并返回固定 ID。"""

        self.items.append(item)
        return "task-id"


class _CapturePublisher:
    """收集任务协调器发布的字幕落盘事件。"""

    def __init__(
        self,
        *,
        error: BaseException | None = None,
        fail_on_calls: set[int] | None = None,
    ) -> None:
        """初始化事件列表与可选发布异常。"""

        self.error = error
        self.fail_on_calls = fail_on_calls or set()
        self.call_count = 0
        self.events: list[SubtitleWrittenEvent] = []

    async def publish(self, event: SubtitleWrittenEvent) -> None:
        """收集事件或抛出预设异常。"""

        self.call_count += 1
        if self.error is not None or self.call_count in self.fail_on_calls:
            raise self.error or RuntimeError("模拟发布失败")
        self.events.append(event)


class _TargetCatalog:
    """测试任务能力时提供字幕目标路径解析 facade。"""

    def __init__(self, mappings: tuple[PathMapping, ...]) -> None:
        """保存任务执行所需的路径映射。"""

        self._mappings = mappings
        self.calls: list[Path] = []

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> PathMappingResolution:
        """通过目标能力公开路径解析接口返回结果。"""

        self.calls.append(target.target_path)
        for mapping in self._mappings:
            try:
                relative = target.target_path.relative_to(mapping.source_prefix)
            except ValueError:
                continue
            return PathMappingResolution(
                original_path=target.target_path,
                resolved_path=mapping.target_prefix / relative,
                mapping=mapping,
            )
        return PathMappingResolution(original_path=target.target_path, resolved_path=target.target_path)


class _LogCapture:
    """收集协调器结构化日志文本。"""

    def __init__(self) -> None:
        """初始化各级别日志列表。"""

        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []
        self.error_messages: list[str] = []
        self.debug_messages: list[str] = []

    def debug(self, message: str) -> None:
        """收集调试日志。"""

        self.debug_messages.append(message)

    def info(self, message: str) -> None:
        """收集信息日志。"""

        self.info_messages.append(message)

    def warning(self, message: str) -> None:
        """收集警告日志。"""

        self.warning_messages.append(message)

    def error(self, message: str) -> None:
        """收集错误日志。"""

        self.error_messages.append(message)


def _config(all_sources: bool = False, max_attempts: int = 3) -> PluginConfig:
    """构造任务协调器测试配置。"""

    return PluginConfig(
        enabled=True,
        moviepilot_enabled=True,
        opensubtitles_enabled=all_sources,
        assrt_enabled=all_sources,
        max_candidate_attempts=max_attempts,
        source_priority=["moviepilot", "opensubtitles", "assrt"],
        format_priority=["ASS", "SRT", "SUP"],
    )


def _work_item(tmp_path: Path, name: str) -> TaskWorkItem:
    """构造本地文件型媒体工作项。"""

    target_path = tmp_path / f"{name}.mkv"
    context = SubtitleTarget(
        title=name,
        year=2024,
        media_type=MediaType.TV,
        season=1,
        episode=1,
        tmdb_id=123,
        target_path=target_path,
        target_file_name=target_path.name,
        target_storage="local",
    )
    return TaskWorkItem(context=context)


def _candidate(candidate_key: str, source: SubtitleSource = SubtitleSource.MOVIEPILOT) -> CandidateHandle:
    """构造任务候选句柄。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key=candidate_key,
            source=source,
            name=candidate_key,
            file_name=f"{candidate_key}.srt",
            language="简体中文",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
        ),
        download_handle=(
            MoviePilotDownloadHandle(site_id=1, enclosure=f"https://example.invalid/{candidate_key}")
            if source is SubtitleSource.MOVIEPILOT
            else OpenSubtitlesDownloadHandle(file_id=1)
            if source is SubtitleSource.OPENSUBTITLES
            else AssrtDownloadHandle(subtitle_id=1)
        ),
    )


def _candidate_result(records: list[MatchRecord], candidate: SubtitleCandidate) -> Any:
    """构造替身接线使用的候选尝试业务结果对象。"""

    del candidate
    return SimpleNamespace(
        records=records,
        committed_media_records=records,
        committed_files=tuple(
            SimpleNamespace(
                record=record,
                target_path=record.target_path or record.path,
                subtitle_path=record.final_subtitle_path or record.path,
            )
            for record in records
        ),
        result=AttemptResult.SUCCESS,
        error_summary=None,
        reason_code=None,
    )


@dataclass
class _AttributedSubtitle:
    """归属 facade 测试使用的最小文件和证据值。"""

    extracted: ExtractedSubtitle
    evidence: FileAttributionEvidence


def _inventory_record(context: SubtitleTarget) -> MatchRecord:
    """构造已由库存消费并迁移的记录。"""

    return MatchRecord(
        subtitle_file_name="inventory.srt",
        format="SRT",
        media_title=context.title,
        year=context.year,
        media_type=context.media_type,
        season=context.season,
        episode=context.episode,
        status=RecordStatus.MATCHED,
        source=SubtitleSource.ASSRT,
        package_scope=PackageScope.SEASON_PACK,
        location=FileLocation.MEDIA_DIRECTORY,
        path=Path("/media/inventory.default.chi.zh-cn.srt"),
        source_task_id="source-task",
        consumed_task_id="current-task",
        candidate_key="inventory-candidate",
        language="简体中文",
    )


def _coordinator(
    tmp_path: Path,
    *,
    store: _TaskStore | None = None,
    filesystem: _TaskFileSystem | None = None,
    archive: _TaskArchive | None = None,
    matcher: _TaskMatcher | None = None,
    sources: dict[SubtitleSource, _TaskSource] | None = None,
    config: PluginConfig | None = None,
    inventory: _TaskInventory | RecordCommitter | None = None,
    candidate_pool: _TaskCandidatePool | SourceAdministration | None = None,
    publisher: _CapturePublisher | None = None,
    target_catalog: TargetCatalog | None = None,
    manage_resources: bool = True,
) -> TaskOperations:
    """使用轻量依赖创建任务协调器。"""

    source_map = sources or {SubtitleSource.MOVIEPILOT: _TaskSource(SubtitleSource.MOVIEPILOT)}
    active_store = store or _TaskStore()
    active_filesystem = filesystem or _TaskFileSystem(tmp_path / "tasks")
    active_config = config or _config()
    active_inventory = inventory or RecordCommitter(
        store=active_store,
        filesystem=active_filesystem,
        records=[],
        format_priority=active_config.format_priority,
        source_priority=[source.value for source in active_config.source_priority],
    )
    active_matcher = matcher or _TaskMatcher()
    return TaskOperations(
        store=active_store,
        filesystem=active_filesystem,
        archive=archive or _TaskArchive(),
        matcher=active_matcher,
        sources=candidate_pool or _TaskCandidatePool(source_map),
        config=active_config,
        inventory=active_inventory,
        media_extensions=["mkv"],
        attributor=active_matcher,
        target_catalog=target_catalog or TargetCatalog(config_provider=lambda: active_config),
        publisher=publisher or _CapturePublisher(),
        manage_resources=manage_resources,
    )


async def test_transfer_complete_uses_transfer_target_and_builds_safe_context() -> None:
    """整理事件只取 transferinfo.target_item 并构造安全媒体上下文。"""

    target = SimpleNamespace(
        path="/media/Target.S02E03.mkv",
        name="Target.S02E03.mkv",
        storage="local",
        type="file",
        extension="mkv",
    )
    decoy = SimpleNamespace(path="/downloads/source.mkv")
    meta = SimpleNamespace(name="元数据标题", year="2023", begin_season=2, begin_episode=3)
    mediainfo = SimpleNamespace(
        title="识别标题",
        original_title="Original",
        en_title="English",
        year=2024,
        season=2,
        tmdb_id=987,
        imdb_id="tt1234567",
        type=SimpleNamespace(name="TV", value="TV"),
    )
    capture = _CaptureCoordinator()
    runtime = object.__new__(PluginRuntime)
    runtime._enabled = True
    runtime.coordinator = capture
    event = Event(
        EventType.TransferComplete,
        {
            "fileitem": decoy,
            "transferinfo": SimpleNamespace(target_item=target),
            "meta": meta,
            "mediainfo": mediainfo,
            "transfer_history_id": 73,
        },
    )

    await runtime.on_transfer_complete(event)

    assert len(capture.items) == 1
    item = capture.items[0]
    assert item.target_history_id == 73
    assert item.match_context is not None
    assert item.match_context.title == "识别标题"
    assert item.match_context.original_title == "Original"
    assert "English" in item.match_context.aliases
    assert item.match_context.year == 2024
    assert item.match_context.tmdb_id == 987
    assert item.match_context.imdb_id == "tt1234567"
    assert item.context.title == "识别标题"
    assert item.context.target_path == Path(target.path)
    assert item.context.target_file_name == target.name
    assert item.context.media_type is MediaType.TV
    assert (item.context.season, item.context.episode) == (2, 3)
    assert (item.context.tmdb_id, item.context.imdb_id) == (987, "tt1234567")


@pytest.mark.parametrize("history_id", [None, 73])
@pytest.mark.parametrize("video_exists", [False, True])
async def test_live_transfer_checks_mapped_video_and_existing_subtitle(
    tmp_path: Path,
    history_id: int | None,
    video_exists: bool,
) -> None:
    """实时事件无论有无历史编号，都在映射目录检查视频及已有字幕。"""

    original_root = tmp_path / "original"
    current_root = tmp_path / "current"
    await AsyncPath(current_root).mkdir()
    item = _work_item(original_root, "live-event")
    original_path = item.context.target_path
    actual_path = current_root / original_path.name
    if video_exists:
        await AsyncPath(actual_path).write_bytes(b"video")
    await AsyncPath(actual_path.with_suffix(".chi.zh-cn.srt")).write_text("已有字幕")
    item.target_history_id = history_id
    item.history_target = False
    config = _config()
    config.path_mappings = (PathMapping(original_root, current_root),)
    pool = _TaskCandidatePool({})
    inventory = _TaskInventory()
    coordinator = _coordinator(
        tmp_path,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        target_catalog=TargetCatalog(config_provider=lambda: config),
        inventory=inventory,
        candidate_pool=pool,
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
        target_history_id=item.target_history_id,
    )

    await coordinator._process(task, item)

    assert task.status is (TaskStatus.SKIPPED if video_exists else TaskStatus.FAILED)
    assert task.reason_code == ("existing_standard_subtitle" if video_exists else "target_missing")
    assert task.target_path == actual_path
    assert task.history_target_path == original_path
    assert task.target_file_exists is video_exists
    assert task.matched_path_mapping is not None
    assert pool.query_calls == []
    assert inventory.consume_calls == []


@pytest.mark.parametrize("history_target", [False, True])
async def test_automatic_target_resolves_once_before_inventory(
    tmp_path: Path,
    history_target: bool,
) -> None:
    """实时和历史自动目标都在前置检查及库存消费前解析一次。"""

    history_root = tmp_path / "history"
    current_root = tmp_path / "current"
    await AsyncPath(current_root).mkdir(parents=True)
    history_path = history_root / "Show.S01E01.mkv"
    actual_target = current_root / history_path.name
    await AsyncPath(actual_target).write_bytes(b"video")
    item = _work_item(tmp_path, "historical")
    item.context = item.context.model_copy(update={"target_path": history_path})
    item.target_history_id = 91
    item.history_target = history_target
    catalog = _TargetCatalog((PathMapping(history_root, current_root),))
    inventory = _TaskInventory(InventoryConsumeResult(matched=True, record=_inventory_record(item.context)))
    coordinator = _coordinator(tmp_path, target_catalog=catalog, inventory=inventory)
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=history_path,
        target_history_id=item.target_history_id,
    )

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert catalog.calls == [history_path]
    assert inventory.consume_calls == [(actual_target, task.id)]
    assert task.history_target_path == history_path
    assert task.target_path == actual_target
    assert task.matched_path_mapping is not None


async def test_historical_target_allows_missing_video_when_mapped_parent_is_available(
    tmp_path: Path,
) -> None:
    """映射后的目标视频缺失但父目录可用时仍通过前置检查。"""

    history_root = tmp_path / "history"
    current_root = tmp_path / "current"
    await AsyncPath(current_root).mkdir(parents=True)
    history_target = history_root / "Show.S01E01.mkv"
    item = _work_item(tmp_path, "mapped-missing-video")
    item.context = item.context.model_copy(update={"target_path": history_target})
    item.target_history_id = 93
    item.history_target = True
    coordinator = _coordinator(
        tmp_path,
        target_catalog=_TargetCatalog((PathMapping(history_root, current_root),)),
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=history_target,
        target_history_id=item.target_history_id,
    )

    await coordinator._prepare_target(task, item)

    assert await coordinator._preflight(task, item) is True
    assert task.target_file_exists is False
    assert task.target_path == current_root / history_target.name


async def test_historical_manual_target_without_catalog_is_assembly_failure(
    tmp_path: Path,
) -> None:
    """人工整理历史目标缺少目标目录能力时不得回退到历史路径写入。"""

    item = _work_item(tmp_path, "manual-without-catalog")
    item.target_history_id = 92
    item.history_target = True
    item.manual_handle = _candidate("manual-without-catalog")
    coordinator = _coordinator(tmp_path)
    coordinator._target_catalog = None
    task = SubtitleTask(
        trigger=TaskTrigger.MANUAL_CANDIDATE,
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
        target_history_id=item.target_history_id,
    )

    await coordinator._process(task, item)

    assert task.status is TaskStatus.FAILED
    assert task.reason_code == "processing_error"
    assert task.reason_message == "字幕任务处理异常"


async def test_enqueue_merges_same_path_while_task_is_nonterminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一路径仍在等待或处理时只保留一个任务与队列项。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)
    item = _work_item(tmp_path, "same")

    first = await coordinator.enqueue(item)
    second = await coordinator.enqueue(item)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(store.tasks) == 1
    assert len(store.save_task_calls) == 1
    assert coordinator._queue.qsize() == 1


async def test_enqueue_double_click_merges_manual_candidate_without_second_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动双击同一候选两次只创建一个任务并返回既有任务快照。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)
    item = _work_item(tmp_path, "manual-double-click")
    item.manual_handle = _candidate("manual-double-click")

    first = await coordinator.enqueue(item)
    second_item = _work_item(tmp_path, "manual-double-click")
    second_item.manual_handle = _candidate("manual-double-click")
    second = await coordinator.enqueue(second_item)

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(store.tasks) == 1
    assert len(store.save_task_calls) == 1
    assert coordinator._queue.qsize() == 1


async def test_automatic_event_on_same_path_merges_into_running_manual_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动任务运行中同路径自动事件合并到既有任务，不创建第二个任务。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)
    manual_item = _work_item(tmp_path, "manual-then-auto")
    manual_item.manual_handle = _candidate("manual-then-auto")

    manual = await coordinator.enqueue(manual_item)
    automatic = await coordinator.enqueue(_work_item(tmp_path, "manual-then-auto"))

    assert manual is not None
    assert automatic is not None
    assert automatic.id == manual.id
    assert manual.trigger is TaskTrigger.MANUAL_CANDIDATE
    assert len(store.tasks) == 1
    assert len(store.save_task_calls) == 1
    assert coordinator._queue.qsize() == 1


async def test_enqueue_creates_new_task_after_previous_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前一任务进入终态后同路径重新入队会创建新任务。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)

    first = await coordinator.enqueue(_work_item(tmp_path, "retry-after-terminal"))
    assert first is not None
    store.tasks[first.id].status = TaskStatus.FAILED

    second = await coordinator.enqueue(_work_item(tmp_path, "retry-after-terminal"))

    assert second is not None
    assert second.id != first.id
    assert second.status is TaskStatus.QUEUED
    coordinator.stop_sync()


async def test_task_operations_manage_task_snapshots_and_stop_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务 facade 提供快照操作，重复停止不重复标记未完成任务。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)

    task = await coordinator.enqueue(_work_item(tmp_path, "facade"))

    assert task is not None
    assert (await coordinator.get_task(task.id)) is not None
    assert [item.id for item in await coordinator.list_tasks()] == [task.id]
    assert await coordinator.delete_task(task.id) is True
    assert await coordinator.get_task(task.id) is None

    coordinator.stop_sync("测试停止")
    coordinator.stop_sync("再次停止")

    assert store.interrupt_messages == ["测试停止"]


async def test_single_worker_processes_distinct_tasks_strictly_serially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一 worker 在前一任务完成前不会启动下一任务。"""

    coordinator = _coordinator(tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    timeline: list[str] = []
    active = 0
    max_active = 0

    async def fake_process(_task: SubtitleTask, item: TaskWorkItem) -> None:
        """阻塞首项并记录处理并发度。"""

        nonlocal active, max_active
        name = item.context.title
        active += 1
        max_active = max(max_active, active)
        timeline.append(f"start:{name}")
        if name == "first":
            first_started.set()
            await release_first.wait()
        await asyncio.sleep(0)
        timeline.append(f"end:{name}")
        active -= 1

    monkeypatch.setattr(coordinator, "_process", fake_process)
    await coordinator.enqueue(_work_item(tmp_path, "first"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    await coordinator.enqueue(_work_item(tmp_path, "second"))
    await asyncio.sleep(0)
    assert timeline == ["start:first"]

    release_first.set()
    await asyncio.wait_for(coordinator._queue.join(), timeout=1)
    assert timeline == ["start:first", "end:first", "start:second", "end:second"]
    assert max_active == 1
    await coordinator.shutdown()


async def test_automatic_task_uses_one_shared_candidate_pool_query(tmp_path: Path) -> None:
    """自动任务只调用一次共享批量查询并消费三源结果。"""

    sources = {source: _TaskSource(source) for source in SubtitleSource}
    candidate_pool = _TaskCandidatePool(sources)
    coordinator = _coordinator(
        tmp_path,
        sources=sources,
        config=_config(all_sources=True),
        candidate_pool=candidate_pool,
    )
    item = _work_item(tmp_path, "concurrent")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await asyncio.wait_for(coordinator._search_sources(task, item), timeout=1)

    assert handles == []
    assert candidate_pool.query_calls == [item.context]


async def test_search_logs_each_source_result_and_post_match_filtering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐源日志区分空结果、错误、返回候选与宿主匹配过滤。"""

    sources = {source: _TaskSource(source) for source in SubtitleSource}
    candidate_pool = _TaskCandidatePool(
        sources,
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="success",
            ),
            SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                source=SubtitleSource.OPENSUBTITLES,
                status="error",
                error_summary="OpenSubtitles 搜索失败",
            ),
            SubtitleSource.ASSRT: SourceSearchResult(
                source=SubtitleSource.ASSRT,
                status="success",
                candidates=[
                    _candidate("assrt-qualified", SubtitleSource.ASSRT),
                    _candidate("assrt-filtered", SubtitleSource.ASSRT),
                ],
            ),
        },
    )
    coordinator = _coordinator(
        tmp_path,
        sources=sources,
        config=_config(all_sources=True),
        candidate_pool=candidate_pool,
    )
    original_normalize = coordinator._matcher.normalize_candidate

    def normalize(
        candidate: SubtitleCandidate,
        context: SubtitleTarget,
        match_context: CandidateMatchContext | None,
    ) -> SubtitleCandidate | None:
        """过滤一个 ASSRT 候选以暴露协调层计数。"""

        if candidate.candidate_key == "assrt-filtered":
            return None
        return original_normalize(candidate, context, match_context)

    monkeypatch.setattr(coordinator._matcher, "normalize_candidate", normalize)
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "observable-search")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert [handle.candidate.candidate_key for handle in handles] == ["assrt-qualified"]
    messages = logs.info_messages + logs.warning_messages
    assert any("MoviePilot 站点字幕源 搜索完成：字幕站没有返回候选" in item for item in messages)
    assert any("OpenSubtitles 搜索失败：OpenSubtitles 搜索失败" in item for item in messages)
    assert any(
        "ASSRT 搜索完成：字幕站返回 2 个候选" in item and "自动规则保留 2 个，其中 1 个适用于当前目标" in item
        for item in messages
    )
    assert any("共获得 1 个适用于当前目标的候选" in item for item in messages)
    assert not any("event=" in item or "source=" in item for item in messages)


async def test_search_records_disabled_and_unconfigured_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享候选池返回的未启用与未配置来源仍更新来源状态与日志。"""

    config = _config(all_sources=True)
    sources = {source: _TaskSource(source) for source in SubtitleSource}
    candidate_pool = _TaskCandidatePool(
        sources,
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="disabled",
                duration_ms=11,
                skip_reason="source_disabled",
            ),
            SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                source=SubtitleSource.OPENSUBTITLES,
                status="unconfigured",
                duration_ms=22,
                skip_reason="no_subtitle_sites",
            ),
            SubtitleSource.ASSRT: SourceSearchResult(
                source=SubtitleSource.ASSRT,
                status="success",
                duration_ms=33,
            ),
        },
    )
    store = _TaskStore()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        sources=sources,
        config=config,
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "source-participation")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    await coordinator._search_sources(task, item)

    assert len(candidate_pool.query_calls) == 1
    messages = logs.info_messages + logs.warning_messages
    assert any("MoviePilot 站点字幕源 搜索未执行：该来源未启用" in item for item in messages)
    assert any("OpenSubtitles 搜索未执行：没有启用且支持字幕搜索的站点" in item for item in messages)


async def test_source_raw_results_fully_rejected_are_logged_as_no_target_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源有返回但自动规则全部排除时日志明确说明没有适用候选。"""

    sources = {SubtitleSource.MOVIEPILOT: _TaskSource(SubtitleSource.MOVIEPILOT)}
    candidate_pool = _TaskCandidatePool(
        sources,
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="success",
                candidates=[
                    CandidateHandle(
                        candidate=_candidate("english-1").candidate.model_copy(update={"language": "en"}),
                        download_handle=MoviePilotDownloadHandle(
                            site_id=1,
                            enclosure="https://example.invalid/english-1",
                        ),
                    ),
                    CandidateHandle(
                        candidate=_candidate("english-2").candidate.model_copy(update={"language": "en"}),
                        download_handle=MoviePilotDownloadHandle(
                            site_id=1,
                            enclosure="https://example.invalid/english-2",
                        ),
                    ),
                ],
            )
        },
    )
    coordinator = _coordinator(
        tmp_path,
        sources=sources,
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "fully-filtered")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert handles == []
    assert len(candidate_pool.query_calls) == 1
    assert any(
        "字幕站返回 2 个候选" in message
        and "自动规则保留 0 个" in message
        and "自动规则排除：语言不符合自动规则 2 个" in message
        for message in logs.info_messages
    )
    # 无候选任务摘要：固定开头 + 各来源一句短结论（任务列表自解释）。
    reason_message = coordinator._no_candidate_reason_message()
    assert reason_message.startswith("没有可用的合格简中字幕候选；各来源结论：")
    assert "MoviePilot 站点字幕源：完成：字幕站返回 2 个候选" in reason_message


async def test_automatic_admission_preserves_exact_media_identity_from_candidate_pool(tmp_path: Path) -> None:
    """自动准入在候选池之后执行，并保留精确媒体身份供排序使用。"""

    source = _TaskSource(SubtitleSource.OPENSUBTITLES)
    accepted = _candidate("exact", SubtitleSource.OPENSUBTITLES)
    accepted = CandidateHandle(
        candidate=accepted.candidate.model_copy(update={"language": "zh-cn", "tmdb_id": 123}),
        download_handle=accepted.download_handle,
    )
    machine = _candidate("machine", SubtitleSource.OPENSUBTITLES)
    machine = CandidateHandle(
        candidate=machine.candidate.model_copy(
            update={"language": "zh-cn", "translation_type": TranslationType.MACHINE}
        ),
        download_handle=machine.download_handle,
    )
    foreign_parts = _candidate("foreign-parts", SubtitleSource.OPENSUBTITLES)
    foreign_parts = CandidateHandle(
        candidate=foreign_parts.candidate.model_copy(update={"language": "zh-cn", "foreign_parts_only": True}),
        download_handle=foreign_parts.download_handle,
    )
    candidate_pool = _TaskCandidatePool(
        {SubtitleSource.OPENSUBTITLES: source},
        results={
            SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                source=SubtitleSource.OPENSUBTITLES,
                status="success",
                candidates=[accepted, machine, foreign_parts],
            )
        },
    )
    coordinator = _coordinator(
        tmp_path,
        sources={SubtitleSource.OPENSUBTITLES: source},
        config=_config(all_sources=True),
        candidate_pool=candidate_pool,
    )
    item = _work_item(tmp_path, "automatic-admission")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert [handle.candidate.candidate_key for handle in handles] == ["exact"]
    assert handles[0].candidate.exact_id_match is True
    assert len(candidate_pool.query_calls) == 1


async def test_incomplete_source_pagination_logs_partial_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源分页未完整读取时明确记录部分结果警告。"""

    sources = {SubtitleSource.MOVIEPILOT: _TaskSource(SubtitleSource.MOVIEPILOT)}
    store = _TaskStore()
    candidate_pool = _TaskCandidatePool(
        sources,
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="partial",
                candidates=[_candidate("partial")],
                error_summary="字幕源分页未完整读取",
            )
        },
    )
    coordinator = _coordinator(
        tmp_path,
        store=store,
        sources=sources,
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "partial-pagination")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert len(handles) == 1
    assert store.statuses[SubtitleSource.MOVIEPILOT].health.value == "healthy"
    assert any(
        "MoviePilot 站点字幕源 搜索部分完成" in message and "分页未完整读取" in message
        for message in logs.warning_messages
    )


async def test_candidate_pool_cleanup_failure_is_logged_as_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享候选池关闭异常按非预期清理错误记录。"""

    class _FailingCandidatePool(_TaskCandidatePool):
        async def close(self) -> None:
            """模拟候选池缓存关闭失败。"""

            raise RuntimeError("cache close failed")

    source = _TaskSource(SubtitleSource.MOVIEPILOT)
    candidate_pool = _FailingCandidatePool({SubtitleSource.MOVIEPILOT: source})
    coordinator = _coordinator(
        tmp_path,
        sources={SubtitleSource.MOVIEPILOT: source},
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)

    await coordinator._cleanup_runtime()

    assert any("字幕来源关闭失败：RuntimeError" in message for message in logs.error_messages)
    assert not any("字幕来源关闭失败" in message for message in logs.warning_messages)


async def test_shared_candidate_pool_error_isolated_in_source_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享候选池返回单源错误时任务保留该来源的独立失败语义。"""

    sources = {SubtitleSource.MOVIEPILOT: _TaskSource(SubtitleSource.MOVIEPILOT)}
    candidate_pool = _TaskCandidatePool(
        sources,
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="error",
                error_summary="MoviePilot 站点字幕源查询失败",
            )
        },
    )
    store = _TaskStore()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        sources=sources,
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "source-exception")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert handles == []
    assert store.statuses[SubtitleSource.MOVIEPILOT].health.value == "error"
    assert any(
        "MoviePilot 站点字幕源 搜索失败：MoviePilot 站点字幕源查询失败" in message for message in logs.warning_messages
    )


async def test_source_status_updates_share_shape_and_preserve_differences(tmp_path: Path) -> None:
    """三个来源状态入口共用统一形状，健康值/错误/时间与 facade 快照合并保持一致。"""

    source = _TaskSource(SubtitleSource.MOVIEPILOT)
    store = _TaskStore()
    store.statuses[SubtitleSource.MOVIEPILOT] = SourceStatus(
        source=SubtitleSource.MOVIEPILOT,
        details={"site_count": 1},
    )
    coordinator = _coordinator(tmp_path, store=store, sources={SubtitleSource.MOVIEPILOT: source})

    await coordinator._save_source_success(SubtitleSource.MOVIEPILOT, duration_ms=12)
    success = store.statuses[SubtitleSource.MOVIEPILOT]
    assert success.health is SourceHealth.HEALTHY
    assert success.last_success_at == success.last_checked_at
    assert success.last_duration_ms == 12
    assert success.enabled is True and success.configured is True
    assert success.details == {"site_count": 1}

    await coordinator._save_source_failure(SubtitleSource.MOVIEPILOT, "来源失败", limited=True, duration_ms=34)
    limited = store.statuses[SubtitleSource.MOVIEPILOT]
    assert limited.health is SourceHealth.LIMITED
    assert limited.last_error_at == limited.last_checked_at
    assert limited.last_error_summary == "来源失败"
    assert limited.last_duration_ms == 34
    assert limited.last_success_at == success.last_success_at

    await coordinator._save_source_unavailable(SubtitleSource.MOVIEPILOT, "来源未配置", duration_ms=56)
    unavailable = store.statuses[SubtitleSource.MOVIEPILOT]
    assert unavailable.health is SourceHealth.DISABLED
    assert unavailable.configured is False
    assert unavailable.last_error_summary == "来源未配置"
    assert unavailable.last_duration_ms == 56


async def test_moviepilot_without_subtitle_sites_is_unconfigured_and_not_successful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MoviePilot 没有可用字幕站点时记录未配置，不误报空结果或健康。"""

    config = _config()
    config.moviepilot_enabled = True
    source = _TaskSource(SubtitleSource.MOVIEPILOT)
    store = _TaskStore()
    candidate_pool = _TaskCandidatePool(
        {SubtitleSource.MOVIEPILOT: source},
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT,
                status="unconfigured",
                skip_reason="no_subtitle_sites",
            )
        },
    )
    coordinator = _coordinator(
        tmp_path,
        store=store,
        sources={SubtitleSource.MOVIEPILOT: source},
        config=config,
        candidate_pool=candidate_pool,
    )
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)
    item = _work_item(tmp_path, "no-subtitle-sites")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    handles = await coordinator._search_sources(task, item)

    assert handles == []
    assert store.statuses[SubtitleSource.MOVIEPILOT].health.value == "disabled"
    assert store.statuses[SubtitleSource.MOVIEPILOT].configured is False
    messages = logs.info_messages + logs.warning_messages
    assert any("MoviePilot 站点字幕源 搜索未执行：没有启用且支持字幕搜索的站点" in message for message in messages)
    assert not any("MoviePilot 站点字幕源 搜索完成：字幕站没有返回候选" in message for message in messages)


async def test_tv_episode_without_season_fails_before_inventory_and_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """电视剧已有集号但缺季号时直接失败，且不查询库存或字幕源。"""

    coordinator = _coordinator(tmp_path)
    item = _work_item(tmp_path, "missing-season")
    await AsyncPath(item.context.target_path).write_bytes(b"video")
    item.context = item.context.model_copy(update={"season": None, "episode": 2})
    task = SubtitleTask(
        media_title=item.context.title,
        media_type=MediaType.TV,
        season=None,
        episode=2,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def fail_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """如果错误进入库存阶段则令测试失败。"""

        raise AssertionError("缺少季号时不应查询字幕库存")

    async def fail_search(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """如果错误进入来源搜索则令测试失败。"""

        raise AssertionError("缺少季号时不应查询字幕源")

    monkeypatch.setattr(coordinator, "_consume_inventory", fail_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", fail_search)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.FAILED
    assert task.reason_code == "season_missing"
    assert task.reason_message is not None and "未搜索字幕源" in task.reason_message


async def test_existing_subtitle_precedes_missing_season_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺季号电视剧已有标准简中字幕时仍优先按已有字幕跳过。"""

    coordinator = _coordinator(tmp_path)
    item = _work_item(tmp_path, "missing-season-with-subtitle")
    await AsyncPath(item.context.target_path).write_bytes(b"video")
    item.context = item.context.model_copy(update={"season": None, "episode": 2})
    subtitle_path = Path(item.context.target_path).with_suffix(".default.chi.zh-cn.srt")

    async def existing_subtitle(_target: Path) -> Path:
        """返回测试用已有标准简中字幕。"""

        return subtitle_path

    monkeypatch.setattr(coordinator._filesystem, "has_standard_subtitle", existing_subtitle)
    task = SubtitleTask(
        media_title=item.context.title,
        media_type=MediaType.TV,
        season=None,
        episode=2,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SKIPPED
    assert task.reason_code == "existing_standard_subtitle"
    assert task.reason_message == "目标已有标准简中外挂字幕"


async def test_candidate_download_attempts_are_strictly_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选按排序结果逐个下载，任一时刻最多一个下载进行中。"""

    active = 0
    max_active = 0
    download_order: list[str] = []

    async def download(handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """记录下载并主动让出事件循环以暴露并发。"""

        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        download_order.append(handle.candidate.candidate_key)
        await asyncio.sleep(0)
        active -= 1
        return DownloadedAsset(path=directory / "package.zip", file_name="package.zip")

    source = _TaskSource(SubtitleSource.MOVIEPILOT, download_callback=download)
    store = _TaskStore()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        sources={SubtitleSource.MOVIEPILOT: source},
        config=_config(max_attempts=3),
    )
    item = _work_item(tmp_path, "downloads")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )
    handles = [
        _candidate("candidate-a"),
        _candidate("candidate-b"),
        _candidate("candidate-c"),
        _candidate("candidate-d"),
    ]

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的文件前置检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回四个待下载候选以验证最大尝试数。"""

        return handles

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)

    await coordinator._process(task, item)

    assert download_order == ["candidate-a", "candidate-b", "candidate-c"]
    assert max_active == 1
    assert task.status is TaskStatus.FAILED
    assert task.reason_message is not None
    assert "已达到最大候选尝试数 3" in task.reason_message
    assert "另有 1 个候选未尝试" in task.reason_message
    assert "MoviePilot 站点字幕源 候选“candidate-a”：候选包没有允许格式字幕" in task.reason_message
    assert any(
        "候选尝试未成功：MoviePilot 站点字幕源 候选“candidate-a”结束原因是“候选包没有允许格式字幕”" in message
        for message in logs.warning_messages
    )
    assert any("处理失败：已达到最大候选尝试数 3" in message for message in logs.warning_messages)
    assert not logs.error_messages


async def test_candidate_attempt_finishes_task_without_result_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选业务过程以任务终态收尾，字幕产物不复制到任务。"""

    subtitle_path = tmp_path / "trace.srt"
    await AsyncPath(subtitle_path).write_text("trace", encoding="utf-8")
    handle = _candidate("stage-trace")
    store = _TaskStore()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        archive=_TaskArchive([subtitle_path]),
    )
    item = _work_item(tmp_path, "stage-trace")
    task = SubtitleTask(
        media_title=item.context.title,
        year=item.context.year,
        media_type=item.context.media_type,
        season=item.context.season,
        episode=item.context.episode,
        tmdb_id=item.context.tmdb_id,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试不关注的媒体文件检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个用于终态验证的候选。"""

        return [handle]

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert not hasattr(task, "stage_traces")


async def test_automatic_package_keeps_additional_subtitle_after_current_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动压缩包当前字幕落盘后，其他集进入暂存且不绑定当前目标。"""

    subtitle_root = tmp_path / "package-files"
    current_file = subtitle_root / "Show.S01E01.srt"
    other_file = subtitle_root / "Show.S01E02.srt"
    await AsyncPath(subtitle_root).mkdir(parents=True)
    await AsyncPath(current_file).write_text("current", encoding="utf-8")
    await AsyncPath(other_file).write_text("other", encoding="utf-8")
    handle = _candidate("season-package")
    handle.candidate.package_scope = PackageScope.SEASON_PACK
    archive = _TaskArchive([current_file, other_file])
    store = _TaskStore()
    publisher = _CapturePublisher()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        archive=archive,
        matcher=_EpisodeMatcher({current_file.name: 1, other_file.name: 2}),
        publisher=publisher,
    )
    item = _work_item(tmp_path, "Show.S01E01")
    task = SubtitleTask(
        media_title=item.context.title,
        year=item.context.year,
        media_type=item.context.media_type,
        season=item.context.season,
        episode=item.context.episode,
        tmdb_id=item.context.tmdb_id,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
        target_history_id=77,
        history_target_path=item.context.target_path,
        target_file_exists=True,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过与包内产物保留无关的媒体文件检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个包含当前集与附加集的候选包。"""

        return [handle]

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert len(store.records) == 2
    matched = next(record for record in store.records.values() if record.status is RecordStatus.MATCHED)
    staged = next(record for record in store.records.values() if record.status is RecordStatus.STAGED)
    assert (matched.season, matched.episode) == (1, 1)
    assert matched.target_history_id == 77
    assert matched.history_target_path == item.context.target_path
    assert matched.target_path == item.context.target_path
    assert (staged.season, staged.episode) == (1, 2)
    assert staged.target_history_id is None
    assert staged.history_target_path is None
    assert staged.target_path is None
    assert len(publisher.events) == 1
    assert publisher.events[0].operation is SubtitleWrittenOperation.AUTOMATIC_CANDIDATE
    assert publisher.events[0].record_id == matched.id
    assert publisher.events[0].task_id == task.id
    assert publisher.events[0].target_path == item.context.target_path
    assert publisher.events[0].subtitle_path == matched.final_subtitle_path


async def test_manual_package_without_current_keeps_valid_additional_subtitles(
    tmp_path: Path,
) -> None:
    """人工压缩包没有当前集时仍保留有效其他集，并保持目标未绑定。"""

    subtitle_root = tmp_path / "manual-package-files"
    episode_two = subtitle_root / "Show.S01E02.srt"
    episode_three = subtitle_root / "Show.S01E03.srt"
    await AsyncPath(subtitle_root).mkdir(parents=True)
    await AsyncPath(episode_two).write_text("episode two", encoding="utf-8")
    await AsyncPath(episode_three).write_text("episode three", encoding="utf-8")
    handle = _candidate("manual-season-package")
    handle.candidate.package_scope = PackageScope.SEASON_PACK
    archive = _TaskArchive([episode_two, episode_three])
    store = _TaskStore()
    publisher = _CapturePublisher()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        archive=archive,
        matcher=_EpisodeMatcher({episode_two.name: 2, episode_three.name: 3}),
        publisher=publisher,
        target_catalog=_TargetCatalog(()),
    )
    item = _work_item(tmp_path, "Show.S01E01.manual")
    item.manual_handle = handle
    item.target_history_id = 91
    task = SubtitleTask(
        trigger=TaskTrigger.MANUAL_CANDIDATE,
        media_title=item.context.title,
        year=item.context.year,
        media_type=item.context.media_type,
        season=item.context.season,
        episode=item.context.episode,
        tmdb_id=item.context.tmdb_id,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
        target_history_id=91,
        history_target_path=item.context.target_path,
    )

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert task.reason_code == "subtitle_retained"
    assert len(store.records) == 2
    assert {record.episode for record in store.records.values()} == {2, 3}
    assert all(record.status is RecordStatus.STAGED for record in store.records.values())
    assert all(record.target_history_id is None for record in store.records.values())
    assert all(record.history_target_path is None for record in store.records.values())
    assert all(record.target_path is None for record in store.records.values())
    assert publisher.events == []


async def test_extract_runtime_error_is_preserved_in_terminal_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unar 解包错误进入任务终态，不退化为异常类型名。"""

    archive = _TaskArchive()

    async def failed_extract(
        _asset: DownloadedAsset,
        _output: Path,
        _allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """模拟 unar 拒绝下载到的候选文件。"""

        raise RuntimeError("unar 解包失败，退出码 2：文件不是可识别的归档")

    monkeypatch.setattr(archive, "extract", failed_extract)
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, archive=archive, publisher=publisher)
    item = _work_item(tmp_path, "extract-error")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的前置检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个会在解包阶段失败的候选。"""

        return [_candidate("bad-archive")]

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.FAILED
    assert task.reason_message is not None
    assert "下载结果解包阶段失败：unar 解包失败，退出码 2：文件不是可识别的归档" in task.reason_message
    assert publisher.events == []


async def test_unexpected_processing_exception_logs_error_with_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非预期系统异常使用 ERROR 并附插件调用栈，任务仍进入失败终态。"""

    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, publisher=publisher)
    item = _work_item(tmp_path, "unexpected-error")
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def explode(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """模拟前置检查发生非预期系统异常。"""

        raise ValueError("测试系统异常")

    monkeypatch.setattr(coordinator, "_preflight", explode)
    logs = _LogCapture()
    monkeypatch.setattr(task_service, "logger", logs)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.FAILED
    assert task.reason_code == "processing_error"
    assert any(
        "发生非预期处理异常：ValueError；插件调用栈：" in message and "explode" in message
        for message in logs.error_messages
    )
    assert any("处理失败：字幕任务处理异常" in message for message in logs.warning_messages)
    assert publisher.events == []


async def test_inventory_hit_finishes_before_any_external_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库存成功消费后任务直接成功，绝不进入外部搜索。"""

    item = _work_item(tmp_path, "inventory")
    record = _inventory_record(item.context)
    inventory = _TaskInventory(InventoryConsumeResult(matched=True, record=record))
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, inventory=inventory, publisher=publisher)
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的文件前置检查。"""

        return True

    async def fail_search(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """库存命中后若搜索则立即失败。"""

        raise AssertionError("库存命中后不应搜索外部来源")

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_search_sources", fail_search)

    await coordinator._process(task, item)

    assert len(inventory.consume_calls) == 1
    assert task.status is TaskStatus.SUCCESS
    assert task.reason_code == "staged_inventory_consumed"
    assert publisher.events == [
        SubtitleWrittenEvent(
            plugin_id="SubtitleAssistant",
            operation=SubtitleWrittenOperation.INVENTORY_CONSUMPTION,
            task_id=task.id,
            record_id=record.id,
            target_path=record.target_path or record.path,
            subtitle_path=record.final_subtitle_path or record.path,
        )
    ]


async def test_task_coordinator_publishes_automatic_candidate_once_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动候选在任务成功终态保存后只发布一次字幕落盘事实。"""

    item = _work_item(tmp_path, "automatic-publication")
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, publisher=publisher)
    record = _inventory_record(item.context).model_copy(
        update={
            "id": "automatic-record",
            "target_path": item.context.target_path,
            "final_subtitle_path": tmp_path / "automatic-publication.chi.zh-cn.srt",
            "path": tmp_path / "automatic-publication.chi.zh-cn.srt",
        }
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的前置检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个自动候选。"""

        return [_candidate("automatic-candidate")]

    async def succeed(_task: SubtitleTask, _item: TaskWorkItem, _handle: CandidateHandle) -> Any:
        """模拟已经写入并持久化的主匹配记录。"""

        return _candidate_result([record], _handle.candidate)

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)
    monkeypatch.setattr(coordinator, "_try_candidate", succeed)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert publisher.events == [
        SubtitleWrittenEvent(
            plugin_id="SubtitleAssistant",
            operation=SubtitleWrittenOperation.AUTOMATIC_CANDIDATE,
            task_id=task.id,
            record_id=record.id,
            target_path=item.context.target_path,
            subtitle_path=record.final_subtitle_path,
        )
    ]


async def test_candidate_commits_each_current_subtitle_file_as_a_match_record(
    tmp_path: Path,
) -> None:
    """候选包中的多个当前目标字幕都写入媒体目录并形成独立记录。"""

    item = _work_item(tmp_path, "multi-file-candidate")
    archive = _TaskArchive([tmp_path / "first.srt", tmp_path / "second.ass"])
    coordinator = _coordinator(tmp_path, archive=archive)
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    candidate_result = await coordinator._try_candidate(task, item, _candidate("multi-file-candidate"))
    records = candidate_result.records

    assert len(records) == 2
    assert [record.final_subtitle_path for record in records] == [
        item.context.target_path.with_suffix(".chi.zh-cn.ass"),
        item.context.target_path.with_suffix(".chi.zh-cn.srt"),
    ]


async def test_task_coordinator_publishes_all_candidate_records_after_one_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多文件事件逐条发布，单条广播失败不阻断后续文件。"""

    item = _work_item(tmp_path, "multi-file-publication")
    publisher = _CapturePublisher(fail_on_calls={1})
    coordinator = _coordinator(tmp_path, publisher=publisher)
    records = [
        _inventory_record(item.context).model_copy(
            update={
                "id": "multi-record-1",
                "target_path": item.context.target_path,
                "final_subtitle_path": Path("/media/multi-1.chi.zh-cn.srt"),
                "path": Path("/media/multi-1.chi.zh-cn.srt"),
            }
        ),
        _inventory_record(item.context).model_copy(
            update={
                "id": "multi-record-2",
                "target_path": item.context.target_path,
                "final_subtitle_path": Path("/media/multi-2.chi.zh-cn.ass"),
                "path": Path("/media/multi-2.chi.zh-cn.ass"),
            }
        ),
    ]
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的文件前置检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个自动候选。"""

        return [_candidate("multi-file-publication")]

    async def succeed(
        _task: SubtitleTask,
        _item: TaskWorkItem,
        _handle: CandidateHandle,
    ) -> Any:
        """模拟一次候选提交多个匹配记录。"""

        return _candidate_result(records, _handle.candidate)

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)
    monkeypatch.setattr(coordinator, "_try_candidate", succeed)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert publisher.call_count == 2
    assert [event.record_id for event in publisher.events] == ["multi-record-2"]


async def test_interrupted_candidate_still_publishes_already_committed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选写入后被取消时，已提交媒体字幕仍逐文件发布事件。"""

    item = _work_item(tmp_path, "interrupted-publication")
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, publisher=publisher)
    record = _inventory_record(item.context).model_copy(
        update={
            "id": "interrupted-record",
            "target_path": item.context.target_path,
            "path": Path("/media/interrupted.chi.zh-cn.srt"),
            "final_subtitle_path": Path("/media/interrupted.chi.zh-cn.srt"),
        }
    )
    candidate = _candidate("interrupted-candidate")
    fact = CommittedFileFact(
        record=record,
        target_path=item.context.target_path,
        subtitle_path=record.final_subtitle_path,
    )
    result = SimpleNamespace(
        records=(record,),
        result=AttemptResult.INTERRUPTED,
        error_summary=None,
        committed_media_records=(record,),
        committed_files=(fact,),
        warnings=(),
        reason_code=None,
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def interrupted(*_args: Any, **_kwargs: Any) -> Any:
        """模拟写入后阶段取消返回的候选结论。"""

        return result

    monkeypatch.setattr(coordinator._candidate_attempt, "attempt", interrupted)

    with pytest.raises(asyncio.CancelledError):
        await coordinator._try_candidate(task, item, candidate)

    assert [event.record_id for event in publisher.events] == ["interrupted-record"]
    assert publisher.events[0].operation is SubtitleWrittenOperation.AUTOMATIC_CANDIDATE


async def test_task_coordinator_publishes_multiple_manual_and_inventory_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工候选与库存消费都按已提交记录逐文件发布。"""

    manual_item = _work_item(tmp_path, "multi-manual-publication")
    manual_item.manual_handle = _candidate("multi-manual-publication")
    manual_records = [
        _inventory_record(manual_item.context).model_copy(
            update={
                "id": "multi-manual-1",
                "target_path": manual_item.context.target_path,
                "path": Path("/media/multi-manual-1.chi.zh-cn.ass"),
                "final_subtitle_path": Path("/media/multi-manual-1.chi.zh-cn.ass"),
            }
        ),
        _inventory_record(manual_item.context).model_copy(
            update={
                "id": "multi-manual-2",
                "target_path": manual_item.context.target_path,
                "path": Path("/media/multi-manual-2.chi.zh-cn.srt"),
                "final_subtitle_path": Path("/media/multi-manual-2.chi.zh-cn.srt"),
            }
        ),
    ]
    manual_publisher = _CapturePublisher()
    manual_coordinator = _coordinator(tmp_path, publisher=manual_publisher)
    manual_task = SubtitleTask(
        media_title=manual_item.context.title,
        target_file_name=manual_item.context.target_file_name,
        target_path=manual_item.context.target_path,
    )

    async def prepare_manual(_task: SubtitleTask, _item: TaskWorkItem) -> None:
        """跳过本测试无关的人工目标解析。"""

    async def succeed_manual(
        _task: SubtitleTask,
        _item: TaskWorkItem,
        _handle: CandidateHandle,
    ) -> Any:
        """模拟人工候选提交两个媒体目录字幕文件。"""

        return _candidate_result(manual_records, _handle.candidate)

    monkeypatch.setattr(manual_coordinator, "_prepare_target", prepare_manual)
    monkeypatch.setattr(manual_coordinator, "_try_candidate", succeed_manual)
    await manual_coordinator._process(manual_task, manual_item)

    inventory_item = _work_item(tmp_path, "multi-inventory-publication")
    inventory_records = [
        _inventory_record(inventory_item.context).model_copy(
            update={
                "id": "multi-inventory-1",
                "target_path": inventory_item.context.target_path,
                "path": Path("/media/multi-inventory-1.chi.zh-cn.ass"),
                "final_subtitle_path": Path("/media/multi-inventory-1.chi.zh-cn.ass"),
            }
        ),
        _inventory_record(inventory_item.context).model_copy(
            update={
                "id": "multi-inventory-2",
                "target_path": inventory_item.context.target_path,
                "path": Path("/media/multi-inventory-2.chi.zh-cn.srt"),
                "final_subtitle_path": Path("/media/multi-inventory-2.chi.zh-cn.srt"),
            }
        ),
    ]
    inventory_publisher = _CapturePublisher()
    inventory_coordinator = _coordinator(
        tmp_path,
        publisher=inventory_publisher,
        inventory=_TaskInventory(InventoryConsumeResult(records=inventory_records)),
    )
    inventory_task = SubtitleTask(
        media_title=inventory_item.context.title,
        target_file_name=inventory_item.context.target_file_name,
        target_path=inventory_item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的文件前置检查。"""

        return True

    monkeypatch.setattr(inventory_coordinator, "_preflight", pass_preflight)
    await inventory_coordinator._process(inventory_task, inventory_item)

    assert [event.record_id for event in manual_publisher.events] == ["multi-manual-1", "multi-manual-2"]
    assert all(event.operation is SubtitleWrittenOperation.MANUAL_CANDIDATE for event in manual_publisher.events)
    assert [event.record_id for event in inventory_publisher.events] == ["multi-inventory-1", "multi-inventory-2"]
    assert all(
        event.operation is SubtitleWrittenOperation.INVENTORY_CONSUMPTION for event in inventory_publisher.events
    )


async def test_committed_candidate_records_publish_before_task_terminal_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务终态保存失败时，已提交匹配记录仍先形成文件级事件。"""

    item = _work_item(tmp_path, "publish-before-task-save")
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, publisher=publisher)
    record = _inventory_record(item.context).model_copy(
        update={
            "id": "publish-before-task-save-record",
            "target_path": item.context.target_path,
            "path": Path("/media/publish-before-task-save.chi.zh-cn.srt"),
            "final_subtitle_path": Path("/media/publish-before-task-save.chi.zh-cn.srt"),
        }
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )
    finish_calls = 0

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的文件前置检查。"""

        return True

    async def empty_inventory(_task: SubtitleTask, _item: TaskWorkItem) -> InventoryConsumeResult:
        """返回库存未命中。"""

        return InventoryConsumeResult()

    async def searched(_task: SubtitleTask, _item: TaskWorkItem) -> list[CandidateHandle]:
        """返回一个自动候选。"""

        return [_candidate("publish-before-task-save")]

    async def succeed(
        _task: SubtitleTask,
        _item: TaskWorkItem,
        _handle: CandidateHandle,
    ) -> Any:
        """模拟已经提交的匹配记录。"""

        return _candidate_result([record], _handle.candidate)

    async def fail_terminal_save(
        _task: SubtitleTask,
        status: TaskStatus,
        _reason_code: str,
        _reason_message: str,
    ) -> None:
        """模拟首次终态快照保存失败，允许异常处理完成。"""

        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise RuntimeError("任务终态保存失败")
        _task.status = status

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    monkeypatch.setattr(coordinator, "_consume_inventory", empty_inventory)
    monkeypatch.setattr(coordinator, "_search_sources", searched)
    monkeypatch.setattr(coordinator, "_try_candidate", succeed)
    monkeypatch.setattr(coordinator, "_finish_task", fail_terminal_save)

    await coordinator._process(task, item)

    assert finish_calls == 2
    assert [event.record_id for event in publisher.events] == ["publish-before-task-save-record"]


async def test_task_coordinator_publishes_manual_candidate_and_inventory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工候选与库存消费分别在成功后发布准确操作类型。"""

    manual_item = _work_item(tmp_path, "manual-publication")
    manual_item.manual_handle = _candidate("manual-publication")
    manual_publisher = _CapturePublisher()
    manual_coordinator = _coordinator(tmp_path, publisher=manual_publisher)
    manual_record = _inventory_record(manual_item.context).model_copy(
        update={
            "id": "manual-record",
            "target_path": manual_item.context.target_path,
            "final_subtitle_path": tmp_path / "manual-publication.chi.zh-cn.srt",
            "path": tmp_path / "manual-publication.chi.zh-cn.srt",
        }
    )
    manual_task = SubtitleTask(
        media_title=manual_item.context.title,
        target_file_name=manual_item.context.target_file_name,
        target_path=manual_item.context.target_path,
    )

    async def prepare_manual(_task: SubtitleTask, _item: TaskWorkItem) -> None:
        """跳过本测试无关的人工目标解析。"""

    async def succeed_manual(
        _task: SubtitleTask,
        _item: TaskWorkItem,
        _handle: CandidateHandle,
    ) -> Any:
        """模拟人工候选成功记录。"""

        return _candidate_result([manual_record], _handle.candidate)

    monkeypatch.setattr(manual_coordinator, "_prepare_target", prepare_manual)
    monkeypatch.setattr(manual_coordinator, "_try_candidate", succeed_manual)
    await manual_coordinator._process(manual_task, manual_item)

    inventory_item = _work_item(tmp_path, "inventory-publication")
    inventory_record = _inventory_record(inventory_item.context).model_copy(
        update={
            "id": "inventory-record",
            "target_path": inventory_item.context.target_path,
            "final_subtitle_path": tmp_path / "inventory-publication.chi.zh-cn.srt",
            "path": tmp_path / "inventory-publication.chi.zh-cn.srt",
        }
    )
    inventory_publisher = _CapturePublisher()
    inventory_coordinator = _coordinator(
        tmp_path,
        publisher=inventory_publisher,
        inventory=_TaskInventory(InventoryConsumeResult(matched=True, record=inventory_record)),
    )
    inventory_task = SubtitleTask(
        media_title=inventory_item.context.title,
        target_file_name=inventory_item.context.target_file_name,
        target_path=inventory_item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的前置检查。"""

        return True

    monkeypatch.setattr(inventory_coordinator, "_preflight", pass_preflight)
    await inventory_coordinator._process(inventory_task, inventory_item)

    assert manual_task.status is TaskStatus.SUCCESS
    assert [event.record_id for event in manual_publisher.events] == ["manual-record"]
    assert inventory_task.status is TaskStatus.SUCCESS
    assert [event.record_id for event in inventory_publisher.events] == ["inventory-record"]


async def test_task_coordinator_ignores_publisher_failure_after_inventory_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布器失败不改变已成功的库存消费任务结果。"""

    item = _work_item(tmp_path, "publication-failure")
    record = _inventory_record(item.context)
    publisher = _CapturePublisher(error=RuntimeError("敏感路径不应进入日志"))
    coordinator = _coordinator(
        tmp_path,
        publisher=publisher,
        inventory=_TaskInventory(InventoryConsumeResult(matched=True, record=record)),
    )
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """跳过本测试无关的前置检查。"""

        return True

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    await coordinator._process(task, item)

    assert task.status is TaskStatus.SUCCESS
    assert task.reason_code == "staged_inventory_consumed"
    assert publisher.call_count == 1
    assert publisher.events == []


async def test_task_coordinator_does_not_publish_failed_manual_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工候选处理失败时不发布字幕落盘事件。"""

    item = _work_item(tmp_path, "failed-publication")
    item.manual_handle = _candidate("failed-manual")
    publisher = _CapturePublisher()
    coordinator = _coordinator(tmp_path, publisher=publisher)
    task = SubtitleTask(
        media_title=item.context.title,
        target_file_name=item.context.target_file_name,
        target_path=item.context.target_path,
    )

    async def prepare_manual(_task: SubtitleTask, _item: TaskWorkItem) -> None:
        """跳过本测试无关的人工目标解析。"""

    async def fail_candidate(
        _task: SubtitleTask,
        _item: TaskWorkItem,
        _handle: CandidateHandle,
    ) -> Any:
        """模拟候选写入或匹配失败。"""

        return _candidate_result([], _handle.candidate)

    monkeypatch.setattr(coordinator, "_prepare_target", prepare_manual)
    monkeypatch.setattr(coordinator, "_try_candidate", fail_candidate)

    await coordinator._process(task, item)

    assert task.status is TaskStatus.FAILED
    assert publisher.events == []


async def test_shutdown_cancels_current_and_marks_current_and_queued_tasks_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止服务取消当前搜索，并把当前与等待任务都持久化为 interrupted。"""

    search_started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_query(_context: SubtitleTarget) -> SourceSearchBatch:
        """保持共享候选池查询运行直到 worker 被取消。"""

        search_started.set()
        await never.wait()
        return SourceSearchBatch(sources={})

    source = _TaskSource(SubtitleSource.MOVIEPILOT)
    candidate_pool = _TaskCandidatePool(
        {SubtitleSource.MOVIEPILOT: source},
        query_callback=blocking_query,
    )
    store = _TaskStore()
    archive = _TaskArchive()
    coordinator = _coordinator(
        tmp_path,
        store=store,
        archive=archive,
        sources={SubtitleSource.MOVIEPILOT: source},
        candidate_pool=candidate_pool,
    )

    async def pass_preflight(_task: SubtitleTask, _item: TaskWorkItem) -> bool:
        """让任务直接进入库存与来源搜索。"""

        return True

    monkeypatch.setattr(coordinator, "_preflight", pass_preflight)
    current_task = await coordinator.enqueue(_work_item(tmp_path, "current"))
    await asyncio.wait_for(search_started.wait(), timeout=1)
    queued_task = await coordinator.enqueue(_work_item(tmp_path, "queued"))
    assert current_task is not None and queued_task is not None

    await coordinator.shutdown("测试停止")

    current = await store.get_task(current_task.id)
    queued = await store.get_task(queued_task.id)
    assert current is not None and current.status is TaskStatus.INTERRUPTED
    assert queued is not None and queued.status is TaskStatus.INTERRUPTED
    assert current.reason_code == "service_interrupted"
    assert queued.reason_code == "service_interrupted"
    assert store.interrupt_messages == ["测试停止"]
    assert archive.cancel_calls >= 1
    assert source.close_calls >= 1

    await coordinator.shutdown("再次停止")

    assert archive.cancel_calls == 1
    assert source.close_calls == 1


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("history_id", [None, 91])
async def test_download_maps_target_path_at_execution(
    tmp_path: Path,
    manual: bool,
    history_id: int | None,
) -> None:
    """自动事件及人工下载都映射落盘、保存原路径并发布实际路径。"""

    history_root = tmp_path / "history"
    current_root = tmp_path / "current"
    await AsyncPath(current_root).mkdir(parents=True)
    history_target = history_root / "Show.S01E01.mkv"
    resolved_target = current_root / history_target.name
    if not manual:
        await AsyncPath(resolved_target).write_bytes(b"video")

    async def download(_handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """生成一个直接字幕文件，不经过归档策略。"""

        path = AsyncPath(directory / "chosen.srt")
        await path.write_text("简体中文字幕", encoding="utf-8")
        return DownloadedAsset(path=Path(path), file_name="chosen.srt")

    source = _TaskSource(SubtitleSource.MOVIEPILOT, download_callback=download)
    store = _TaskStore()
    publisher = _CapturePublisher()
    config = _config()
    config.path_mappings = (PathMapping(str(history_root), str(current_root)),)
    filesystem = SubtitleFiles(tmp_path / "plugin-data", {"srt"})
    handle = _candidate("direct")
    pool = _TaskCandidatePool(
        {SubtitleSource.MOVIEPILOT: source},
        results={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT, status="success", candidates=[handle]
            )
        },
    )
    coordinator = _coordinator(
        tmp_path,
        store=store,
        filesystem=filesystem,
        archive=ArchiveExtractor(),
        sources={SubtitleSource.MOVIEPILOT: source},
        config=config,
        publisher=publisher,
        target_catalog=TargetCatalog(config_provider=lambda: config),
        candidate_pool=pool,
    )
    context = SubtitleTarget(
        title="Show",
        media_type=MediaType.TV,
        season=1,
        episode=1,
        tmdb_id=123,
        target_path=history_target,
        target_file_name=history_target.name,
        target_storage="local",
    )
    item = TaskWorkItem(
        context=context,
        target_history_id=history_id,
        history_target=True,
        manual_handle=handle,
    )

    if manual:
        enqueue_result = await coordinator.enqueue(item)
        assert enqueue_result is not None
        task_id = enqueue_result.id
    else:
        runtime = object.__new__(PluginRuntime)
        runtime._enabled = True
        runtime.coordinator = coordinator
        await runtime.on_transfer_complete(
            Event(
                EventType.TransferComplete,
                {
                    "transferinfo": SimpleNamespace(
                        target_item=SimpleNamespace(
                            path=str(history_target),
                            name=history_target.name,
                            storage="local",
                            type="file",
                            extension="mkv",
                        )
                    ),
                    "meta": SimpleNamespace(name="Show", begin_season=1, begin_episode=1),
                    "mediainfo": SimpleNamespace(title="Show", tmdb_id=123, type=SimpleNamespace(name="TV")),
                    "transfer_history_id": history_id,
                },
            )
        )
        task_id = next(iter(store.tasks))
    await coordinator._queue.join()

    task = store.tasks[task_id]
    assert task.status is TaskStatus.SUCCESS
    assert task.target_history_id == history_id
    assert task.history_target_path == history_target
    assert task.target_path == resolved_target
    assert task.target_file_exists is (not manual)
    assert task.matched_path_mapping is not None
    record = next(iter(store.records.values()))
    assert record.final_subtitle_path is not None
    assert await AsyncPath(record.final_subtitle_path).is_file()
    assert record.target_history_id == history_id
    assert record.history_target_path == history_target
    assert record.target_path == resolved_target
    assert record.target_file_exists is (not manual)
    assert record.matched_path_mapping == task.matched_path_mapping
    assert record.final_subtitle_path.parent == current_root
    assert not await AsyncPath(history_root).exists()
    assert record.file_attribution_method is FileAttributionMethod.DIRECT_FILE
    assert len(publisher.events) == 1
    assert publisher.events[0].operation is (
        SubtitleWrittenOperation.MANUAL_CANDIDATE if manual else SubtitleWrittenOperation.AUTOMATIC_CANDIDATE
    )
    assert publisher.events[0].record_id == record.id
    assert publisher.events[0].task_id == task.id
    assert publisher.events[0].target_path == resolved_target
    assert publisher.events[0].subtitle_path == record.final_subtitle_path
    assert coordinator._active_paths == {}
    assert [target.target_path for target in pool.query_calls] == ([] if manual else [resolved_target])
    await coordinator.shutdown()


async def test_manual_enqueue_returns_snapshot_and_retries_after_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工入队返回任务快照；终态后同路径重新入队创建新任务。"""

    store = _TaskStore()
    coordinator = _coordinator(tmp_path, store=store)
    monkeypatch.setattr(coordinator, "_ensure_worker", lambda: None)

    first_item = _work_item(tmp_path, "manual-idempotent")
    first_item.manual_handle = _candidate("manual-idempotent")
    first = await coordinator.enqueue(first_item)

    assert first is not None
    assert first.id in store.tasks

    store.tasks[first.id].status = TaskStatus.FAILED
    second_item = _work_item(tmp_path, "manual-idempotent")
    second_item.manual_handle = _candidate("manual-idempotent")
    second = await coordinator.enqueue(second_item)

    assert second is not None
    assert second.id != first.id
    assert second.status is TaskStatus.QUEUED
    coordinator.stop_sync()


@pytest.mark.parametrize("manage_resources", [True, False])
async def test_cleanup_with_source_administration_closes_owned_resources_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manage_resources: bool
) -> None:
    """真实来源服务无需字典接口，且只由资源拥有者关闭一次。"""

    source = _TaskSource(SubtitleSource.MOVIEPILOT)
    sources = SourceAdministration()
    monkeypatch.setattr(sources, "_adapters", {source.source: source})
    archive = _TaskArchive()
    coordinator = _coordinator(tmp_path, archive=archive, candidate_pool=sources, manage_resources=manage_resources)

    await coordinator._cleanup_runtime()
    await coordinator._cleanup_runtime()

    assert source.close_calls == int(manage_resources)
    assert archive.cancel_calls == int(manage_resources)
