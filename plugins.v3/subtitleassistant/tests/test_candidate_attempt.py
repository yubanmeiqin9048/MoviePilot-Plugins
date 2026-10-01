"""候选尝试通过任务生命周期 facade 的业务行为测试。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.plugins.subtitleassistant.schemas.attribution import (
    AttributionEvidence,
    CandidateAttributionSnapshot,
    FileAttributionBatchResult,
    FileAttributionEvidence,
    FileAttributionMethod,
    FileAttributionRequest,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.event import SubtitleWrittenOperation
from app.plugins.subtitleassistant.schemas.file import ExtractedSubtitle
from app.plugins.subtitleassistant.schemas.record import CommittedFileFact, InventoryConsumeResult, RecordStatus
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    SourceSearchBatch,
    DownloadedAsset,
    MoviePilotDownloadHandle,
    SourceSearchResult,
    SourceStatus,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import SubtitleDestination, MediaType, SubtitleTarget
from app.plugins.subtitleassistant.schemas.task import (
    AttemptResult,
    CandidateAttemptReasonCode,
    SubtitleTask,
    TaskStatus,
    TaskWorkItem,
)
from app.plugins.subtitleassistant.task import TaskOperations
from app.plugins.subtitleassistant.target import TargetCatalog

pytestmark = pytest.mark.anyio


class _Store:
    """记录任务快照、记录和来源状态的内存替身。"""

    def __init__(self) -> None:
        """初始化内存状态。"""

        self.records: dict[str, Any] = {}
        self.tasks: dict[str, SubtitleTask] = {}
        self.statuses: dict[SubtitleSource, SourceStatus] = {}

    async def list_tasks(self) -> list[SubtitleTask]:
        """返回全部任务快照。"""

        return [task.model_copy(deep=True) for task in self.tasks.values()]

    async def save_task(self, task: SubtitleTask) -> None:
        """保存任务快照。"""

        self.tasks[task.id] = task.model_copy(deep=True)

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """读取任务快照。"""

        task = self.tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    async def delete_task(self, task_id: str) -> bool:
        """删除任务快照。"""

        return self.tasks.pop(task_id, None) is not None

    async def save_record(self, record: Any) -> None:
        """保存匹配记录。"""

        self.records[record.id] = record

    async def delete_record(self, record_id: str) -> None:
        """删除失败写入的记录。"""

        self.records.pop(record_id, None)

    async def list_source_statuses(self) -> list[SourceStatus]:
        """返回来源状态快照。"""

        return [status.model_copy(deep=True) for status in self.statuses.values()]

    async def save_source_status(self, status: SourceStatus) -> None:
        """保存来源状态快照。"""

        self.statuses[status.source] = status.model_copy(deep=True)

    def mark_nonterminal_interrupted_sync(self, _message: str) -> list[str]:
        """由运行态停止流程同步标记未完成任务。"""

        changed: list[str] = []
        for task in self.tasks.values():
            if not task.is_terminal:
                task.status = TaskStatus.INTERRUPTED
                changed.append(task.id)
        return changed

    async def reset(self) -> None:
        """清空测试数据。"""

        self.records.clear()
        self.tasks.clear()
        self.statuses.clear()


class _RecordCommitter:
    """候选流程使用的记录一致性提交替身。"""

    def __init__(self, store: _Store, filesystem: "_Filesystem") -> None:
        """绑定记录和文件替身。"""

        self._store = store
        self._filesystem = filesystem
        self.records: list[Any] = []

    async def consume(self, *_args: Any, **_kwargs: Any) -> InventoryConsumeResult:
        """自动任务不命中库存，让流程继续到候选尝试。"""

        return InventoryConsumeResult()

    async def add(self, record: Any) -> None:
        """收取暂存记录。"""

        self.records.append(record)

    async def publish(self, record: Any) -> None:
        """保存记录并收取暂存库存。"""

        await self._store.save_record(record)
        if record.status is RecordStatus.STAGED:
            await self.add(record)

    async def commit_media(self, record: Any, source: Path, target: Path, destination: SubtitleDestination | None = None) -> CommittedFileFact:
        """模拟媒体字幕与记录的一致性提交。"""

        destination = await self._filesystem.write_media_subtitle(source, target, destination)
        record.path = destination
        record.final_subtitle_path = destination
        await self.publish(record)
        return CommittedFileFact(record=record, target_path=target, subtitle_path=destination)

    async def commit_plugin(self, record: Any, source: Path) -> Any:
        """模拟插件字幕与记录的一致性提交。"""

        record.path = Path(await self._filesystem.save_plugin_file(source, record.id, record.status))
        await self.publish(record)
        return record


class _Filesystem:
    """在临时目录中模拟媒体字幕与插件文件操作。"""

    def __init__(self, root: Path, *, directory_available: bool = True, conflict: bool = False) -> None:
        """设置目标目录与排他落盘结果。"""

        self.root = root
        self.directory_available = directory_available
        self.conflict = conflict
        self.writes: list[Path] = []
        self.plugin_saves: list[str] = []

    async def make_task_directory(self, task_id: str) -> Path:
        """创建候选临时目录。"""

        path = self.root / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def target_directory_status(self, _target: Path, destination: SubtitleDestination | None = None) -> tuple[bool, str | None]:
        """返回预设目标目录可用性。"""

        return self.directory_available, None if self.directory_available else "目标目录不可写"

    async def write_media_subtitle(self, source: Path, target: Path, destination: SubtitleDestination | None = None) -> Path:
        """模拟媒体目录排他写入。"""

        if self.conflict:
            raise FileExistsError(target.with_suffix(source.suffix))
        destination = (destination.directory if destination else target.parent) / f"{target.stem}.chi.zh-cn{source.suffix.lower()}"
        self.writes.append(destination)
        return destination

    async def save_plugin_file(self, _source: Path, record_id: str, status: RecordStatus) -> str:
        """返回插件数据中的安全相对路径。"""

        relative = f"{status.value}/{record_id}.srt"
        self.plugin_saves.append(relative)
        return relative

    async def delete_plugin_file(self, _relative_path: str) -> None:
        """忽略测试中的插件文件清理。"""

    async def delete_subtitle_file(self, _path: Path) -> None:
        """忽略测试中的媒体文件补偿清理。"""

    async def has_standard_subtitle(self, _target: Path, destination: SubtitleDestination | None = None) -> None:
        """模拟没有既有标准字幕。"""

    async def cleanup_task_directory(self, _task_id: str) -> None:
        """忽略测试中的临时目录清理。"""

    async def clear_data_directory(self) -> None:
        """忽略测试中的数据目录清理。"""


class _Archive:
    """返回预设解包字幕的归档替身。"""

    def __init__(self, extracted: list[ExtractedSubtitle]) -> None:
        """保存解包结果。"""

        self.extracted = extracted

    async def extract(
        self,
        _asset: DownloadedAsset,
        _output: Path,
        _allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """返回预设物理字幕文件。"""

        return self.extracted

    async def cancel(self) -> None:
        """忽略测试中的归档取消。"""


class _Matcher:
    """把包内字幕归属到预设当前集或其它集。"""

    def __init__(self, context: SubtitleTarget, episodes: dict[str, int] | None = None) -> None:
        """保存目标上下文与文件集号映射。"""

        self.context = context
        self.episodes = episodes or {}

    def candidate_snapshot(self, candidate: SubtitleCandidate) -> CandidateAttributionSnapshot:
        """返回候选的单集归属快照。"""

        return CandidateAttributionSnapshot(
            media_type=self.context.media_type,
            tmdb_id=self.context.tmdb_id,
            seasons=[1],
            episodes=[1],
            package_scope=candidate.package_scope,
        )

    def normalize_candidate(
        self,
        candidate: SubtitleCandidate,
        _context: SubtitleTarget,
        _match_context: Any,
    ) -> SubtitleCandidate:
        """保留自动流程中的测试候选。"""

        return candidate

    async def attribute_requests(
        self,
        requests: list[FileAttributionRequest],
    ) -> Any:
        """通过归属 facade 返回当前目标的确定证据。"""

        from app.plugins.subtitleassistant.schemas.attribution import FileAttributionBatchResult

        return FileAttributionBatchResult(
            evidence_by_key={
                f"file_{index:04d}": FileAttributionEvidence(
                    logical_source_path=request.logical_source_path,
                    method=FileAttributionMethod.TRUST_PACKAGE,
                    belongs_to_target_media=True,
                    media_type=request.target.media_type,
                    tmdb_id=request.target.tmdb_id,
                    season=request.target.season,
                    episode=self.episodes.get(str(request.logical_source_path), request.target.episode),
                    season_evidence=AttributionEvidence.PATH,
                    episode_evidence=AttributionEvidence.PATH,
                )
                for index, request in enumerate(requests, start=1)
            }
        )


class _RejectingAttributor:
    """返回其它媒体证据的文件归属替身。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    async def attribute_requests(
        self,
        requests: list[FileAttributionRequest],
    ) -> FileAttributionBatchResult:
        """把直字幕明确标记为其它媒体。"""

        self.calls += 1
        request = requests[0]
        return FileAttributionBatchResult(
            evidence_by_key={
                "file_0001": FileAttributionEvidence(
                    logical_source_path=request.logical_source_path,
                    method=FileAttributionMethod.HOST_RECOGNITION,
                    belongs_to_target_media=False,
                    unmatched_reason=UnmatchedReason.MEDIA_UNRECOGNIZED,
                )
            },
            request_count=len(requests),
            submitted_count=len(requests),
        )


class _UnmatchedAttributor:
    """返回季集不明确规则证据的文件归属替身。"""

    def __init__(self) -> None:
        """初始化调用计数。"""

        self.calls = 0

    async def attribute_requests(
        self,
        requests: list[FileAttributionRequest],
    ) -> FileAttributionBatchResult:
        """把字幕标记为属于当前媒体但季集不明确。"""

        self.calls += 1
        return FileAttributionBatchResult(
            evidence_by_key={
                f"file_{index:04d}": FileAttributionEvidence(
                    logical_source_path=request.logical_source_path,
                    method=FileAttributionMethod.TRUST_PACKAGE,
                    belongs_to_target_media=True,
                    media_type=MediaType.TV,
                    tmdb_id=123,
                    unmatched_reason=UnmatchedReason.SEASON_AMBIGUOUS,
                )
                for index, request in enumerate(requests, start=1)
            }
        )


class _Source:
    """返回预设下载结果的字幕源替身。"""

    def __init__(self, asset: DownloadedAsset, error: BaseException | None = None) -> None:
        """保存下载结果或下载异常。"""

        self.source = SubtitleSource.MOVIEPILOT
        self.asset = asset
        self.error = error
        self.started = asyncio.Event()

    async def download(self, _handle: CandidateHandle, _directory: Path) -> DownloadedAsset:
        """返回预设下载结果。"""

        self.started.set()
        if self.error is not None:
            raise self.error
        return self.asset

    async def close(self) -> None:
        """忽略测试中的来源关闭。"""


class _CandidatePool:
    """为自动任务提供一个候选的共享来源查询替身。"""

    def __init__(self, handle: CandidateHandle, source: _Source) -> None:
        """保存自动流程应消费的候选。"""

        self._handle = handle
        self._source = source

    async def query(self, _context: SubtitleTarget) -> SourceSearchBatch:
        """返回单一可下载候选。"""

        return SourceSearchBatch(
            sources={
                SubtitleSource.MOVIEPILOT: SourceSearchResult(
                    source=SubtitleSource.MOVIEPILOT,
                    status="success",
                    candidates=[self._handle],
                )
            }
        )

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """通过统一来源服务边界返回下载结果。"""

        return await self._source.download(handle, directory)

    def status_snapshot(self, source: SubtitleSource) -> SourceStatus:
        """返回单个测试来源的配置。"""

        return SourceStatus(source=source, enabled=source is self._source.source, configured=True)

    async def close(self) -> None:
        """关闭来源服务持有的下载来源。"""

        await self._source.close()


class _NoopPublisher:
    """在候选尝试测试中忽略字幕落盘事件。"""

    async def publish(self, _event: Any) -> None:
        """不记录任何字幕落盘事件。"""


def _context(tmp_path: Path) -> SubtitleTarget:
    """构造带规范媒体身份的电视剧目标。"""

    target = tmp_path / "Show.S01E01.mkv"
    return SubtitleTarget(
        title="Show",
        year=2024,
        media_type=MediaType.TV,
        season=1,
        episode=1,
        tmdb_id=123,
        target_path=target,
        target_file_name=target.name,
        target_storage="local",
    )


def _handle(key: str = "candidate") -> CandidateHandle:
    """构造一个不含敏感下载定位的候选句柄。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key=key,
            source=SubtitleSource.MOVIEPILOT,
            name=key,
            file_name=f"{key}.srt",
            language="简体中文",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
        ),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure=f"https://example.invalid/{key}"),
    )


def _workflow_case(
    tmp_path: Path,
    *,
    suffix: str = ".zip",
    extracted: list[ExtractedSubtitle] | None = None,
    episodes: dict[str, int] | None = None,
    directory_available: bool = True,
    conflict: bool = False,
    download_error: BaseException | None = None,
    attributor: Any | None = None,
    automatic: bool = False,
    package_attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE,
) -> tuple[TaskOperations, SubtitleTarget, CandidateHandle, _Filesystem, _Store, _Source]:
    """经由公开任务 facade 装配一个候选尝试流程。"""

    context = _context(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    asset_path = tmp_path / f"download{suffix}"
    asset_path.write_text("download", encoding="utf-8")
    handle = _handle()
    source = _Source(DownloadedAsset(path=asset_path, file_name=asset_path.name), download_error)
    filesystem = _Filesystem(tmp_path / "tasks", directory_available=directory_available, conflict=conflict)
    store = _Store()
    inventory = _RecordCommitter(store, filesystem)
    matcher = _Matcher(context, episodes)
    coordinator = TaskOperations(
        store=store,
        filesystem=filesystem,
        archive=_Archive(extracted or []),
        matcher=matcher,
        sources=_CandidatePool(handle, source),
        config=PluginConfig(format_priority=["SRT"], package_attribution_strategy=package_attribution_strategy),
        inventory=inventory,
        media_extensions=["mkv"],
        attributor=attributor or matcher,
        publisher=_NoopPublisher(),
        target_catalog=TargetCatalog(),
    )
    return coordinator, context, handle, filesystem, store, source


def _subtitle(tmp_path: Path, name: str, *, is_direct_file: bool = False) -> ExtractedSubtitle:
    """创建一个可统计物理大小的测试字幕。"""

    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(name, encoding="utf-8")
    return ExtractedSubtitle(physical_path=path, logical_source_path=Path(name), is_direct_file=is_direct_file)


async def _wait_for_terminal(coordinator: TaskOperations, task_id: str) -> SubtitleTask:
    """通过公开查询操作等待任务终态快照。"""

    for _ in range(100):
        task = await coordinator.get_task(task_id)
        if task is not None and task.is_terminal:
            return task
        await asyncio.sleep(0.01)
    raise AssertionError("任务没有在预期时间内结束")


async def _run_manual(
    coordinator: TaskOperations,
    context: SubtitleTarget,
    handle: CandidateHandle,
) -> SubtitleTask:
    """通过公开人工入队操作运行候选流程。"""

    queued = await coordinator.enqueue(TaskWorkItem(context=context, manual_handle=handle))
    assert queued is not None
    return await _wait_for_terminal(coordinator, queued.id)


async def _run_automatic(coordinator: TaskOperations, context: SubtitleTarget) -> SubtitleTask:
    """通过公开自动入队操作运行候选流程。"""

    context.target_path.write_text("video", encoding="utf-8")
    task_id = await coordinator.enqueue(TaskWorkItem(context=context))
    assert task_id is not None
    return await _wait_for_terminal(coordinator, task_id.id)


async def test_manual_candidate_workflow_accepts_preserved_results(tmp_path: Path) -> None:
    """人工候选在安全保留字幕时成功受理，下载失败仍保留失败结论。"""

    cases = [
        ("unsupported", {"suffix": ".bin"}, CandidateAttemptReasonCode.UNSUPPORTED_FORMAT),
        (
            "missing",
            {"extracted": [_subtitle(tmp_path / "missing", "other.srt")], "episodes": {"other.srt": 2}},
            CandidateAttemptReasonCode.CANDIDATE_MISSING_TARGET_SUBTITLE,
        ),
        (
            "directory",
            {"extracted": [_subtitle(tmp_path / "directory", "current.srt")], "directory_available": False},
            CandidateAttemptReasonCode.TARGET_DIRECTORY_UNAVAILABLE,
        ),
        (
            "conflict",
            {"extracted": [_subtitle(tmp_path / "conflict", "current.srt")], "conflict": True},
            CandidateAttemptReasonCode.SUBTITLE_DESTINATION_CONFLICT,
        ),
        ("download", {"download_error": RuntimeError("下载失败")}, CandidateAttemptReasonCode.MANUAL_CANDIDATE_FAILED),
    ]
    for name, options, reason_code in cases:
        coordinator, context, handle, _filesystem, _store, _source = _workflow_case(tmp_path / name, **options)
        task = await _run_manual(coordinator, context, handle)
        await coordinator.shutdown("测试清理")

        if name in {"download", "directory", "conflict"}:
            assert task.status is TaskStatus.FAILED
            assert task.reason_code == reason_code.value
        else:
            assert task.status is TaskStatus.SUCCESS
            assert task.reason_code == "subtitle_retained"


async def test_automatic_host_recognition_rejects_wrong_direct_file(
    tmp_path: Path,
) -> None:
    """自动宿主识别不得把错误直字幕绑定到当前媒体。"""

    attributor = _RejectingAttributor()
    coordinator, context, _handle_value, filesystem, _store, _source = _workflow_case(
        tmp_path,
        suffix=".srt",
        extracted=[_subtitle(tmp_path / "direct", "Wrong.Show.S01E01.srt", is_direct_file=True)],
        attributor=attributor,
        automatic=True,
        package_attribution_strategy=PackageAttributionStrategy.HOST_RECOGNITION,
    )

    task = await _run_automatic(coordinator, context)
    await coordinator.shutdown("测试清理")

    assert attributor.calls == 1
    assert task.status is TaskStatus.FAILED
    assert not filesystem.writes


async def test_manual_and_automatic_workflows_reject_unavailable_destination(tmp_path: Path) -> None:
    """人工与自动任务都在下载前拒绝不可用的保存目录，不另找位置保存。"""

    manual, context, handle, manual_filesystem, _store, _source = _workflow_case(
        tmp_path / "manual",
        extracted=[_subtitle(tmp_path / "manual", "current.srt")],
        directory_available=False,
    )
    manual_task = await _run_manual(manual, context, handle)
    await manual.shutdown("测试清理")

    automatic, context, _handle_value, automatic_filesystem, _store, _source = _workflow_case(
        tmp_path / "automatic",
        extracted=[_subtitle(tmp_path / "automatic", "current.srt")],
        directory_available=False,
        automatic=True,
    )
    automatic_task = await _run_automatic(automatic, context)
    await automatic.shutdown("测试清理")

    assert manual_task.status is TaskStatus.FAILED
    assert manual_task.reason_code == "target_directory_unavailable"
    assert not manual_filesystem.plugin_saves and not manual_filesystem.writes
    assert automatic_task.status is TaskStatus.FAILED
    assert automatic_task.reason_code == "target_directory_unavailable"
    assert not automatic_filesystem.writes and not automatic_filesystem.plugin_saves


async def test_manual_candidate_workflow_persists_records_without_task_result_copy(tmp_path: Path) -> None:
    """多文件产物保存在匹配记录中，任务只保留执行结论。"""

    coordinator, context, handle, filesystem, store, _source = _workflow_case(
        tmp_path,
        extracted=[_subtitle(tmp_path, "one.srt"), _subtitle(tmp_path, "two.srt")],
    )
    task = await _run_manual(coordinator, context, handle)
    await coordinator.shutdown("测试清理")

    assert task.status is TaskStatus.SUCCESS
    assert {record.final_subtitle_path for record in store.records.values()} == set(filesystem.writes)
    assert not hasattr(task, "stage_traces")


async def test_rule_attribution_failure_falls_back_to_unmatched_record(tmp_path: Path) -> None:
    """规则归属无法确定季集时，字幕明确落为未匹配记录且结果可解释。"""

    attributor = _UnmatchedAttributor()
    coordinator, context, handle, filesystem, store, _source = _workflow_case(
        tmp_path,
        extracted=[_subtitle(tmp_path, "ambiguous.srt")],
        attributor=attributor,
        package_attribution_strategy=PackageAttributionStrategy.TRUST_PACKAGE,
    )

    task = await _run_manual(coordinator, context, handle)
    await coordinator.shutdown("测试清理")

    assert attributor.calls == 1
    assert not filesystem.writes
    assert task.status is TaskStatus.SUCCESS
    assert task.reason_code == "subtitle_retained"
    assert len(store.records) == 1
    record = next(iter(store.records.values()))
    assert record.status is RecordStatus.UNMATCHED
    assert record.unmatched_reason is UnmatchedReason.SEASON_AMBIGUOUS
    assert record.file_attribution_method is FileAttributionMethod.TRUST_PACKAGE


async def test_manual_candidate_workflow_records_interruption_through_public_snapshot(tmp_path: Path) -> None:
    """取消传播到任务 worker，并在公开快照中保留已中断结论。"""

    coordinator, context, handle, filesystem, _store, source = _workflow_case(
        tmp_path,
        download_error=asyncio.CancelledError(),
    )
    queued = await coordinator.enqueue(TaskWorkItem(context=context, manual_handle=handle))
    assert queued is not None
    await source.started.wait()
    task = await _wait_for_terminal(coordinator, queued.id)
    await coordinator.shutdown("测试清理")

    assert task.status is TaskStatus.INTERRUPTED
    assert task.reason_code == "service_interrupted"
    assert not filesystem.writes
