"""匹配记录改配目标应用服务测试。"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anyio import Path as AsyncPath

from app.plugins.subtitleassistant.file import SubtitleFiles
from app.plugins.subtitleassistant.record import RecordCommitter, RecordMaintenance
from app.plugins.subtitleassistant.schemas.candidate import PackageScope
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.event import SubtitleWrittenEvent, SubtitleWrittenOperation
from app.plugins.subtitleassistant.schemas.record import (
    BatchRetargetPreview,
    FileLocation,
    MatchRecord,
    RecordStatus,
    RetargetMapping,
)
from app.plugins.subtitleassistant.schemas.source import SubtitleSource
from app.plugins.subtitleassistant.schemas.target import (
    MediaIdentityKind,
    MediaType,
    PathMapping,
    PathMappingResolution,
    PathMappingSnapshot,
    SubtitleTarget,
)

pytestmark = pytest.mark.anyio

MIN_BATCH_RETARGET_MAPPINGS = 1
MAX_BATCH_RETARGET_MAPPINGS = 100


async def _write(path: Path, content: bytes) -> None:
    """异步创建测试文件。"""

    target = AsyncPath(path)
    await target.parent.mkdir(parents=True, exist_ok=True)
    await target.write_bytes(content)


class _RecordStore:
    """以内存快照模拟匹配记录持久化。"""

    def __init__(self, record: MatchRecord) -> None:
        """保存初始记录。"""

        self.record = record.model_copy(deep=True)

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """按标识读取独立记录快照。"""

        if self.record.id != record_id:
            return None
        return self.record.model_copy(deep=True)

    async def save_record(self, record: MatchRecord) -> None:
        """保存独立记录快照。"""

        self.record = record.model_copy(deep=True)


class _TargetQuery:
    """返回固定可选目标视频。"""

    def __init__(
        self,
        history_id: int,
        context: SubtitleTarget,
        resolver: Callable[[SubtitleTarget], PathMappingResolution] | None = None,
    ) -> None:
        """保存整理历史标识与目标上下文。"""

        self.history_id = history_id
        self.context = context
        self.resolver = resolver

    async def get_target(self, history_id: int) -> SimpleNamespace | None:
        """返回匹配整理历史的目标。"""

        if history_id != self.history_id:
            return None
        return SimpleNamespace(history_id=history_id, context=self.context)

    async def list_all_targets(self) -> list[SimpleNamespace]:
        """返回批量自动建议使用的固定目标集合。"""

        return [SimpleNamespace(history_id=self.history_id, context=self.context)]

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> PathMappingResolution:
        """返回未配置映射时的原始目标路径。"""

        if self.resolver is not None:
            return self.resolver(target)
        return PathMappingResolution(original_path=target.target_path, resolved_path=target.target_path)


class _MultiRecordStore:
    """保存多条批量改配记录的内存持久化替身。"""

    def __init__(self, records: list[MatchRecord]) -> None:
        """按记录标识保存独立深拷贝。"""

        self.records = {record.id: record.model_copy(deep=True) for record in records}

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """返回指定记录的独立快照。"""

        record = self.records.get(record_id)
        return record.model_copy(deep=True) if record is not None else None

    async def save_record(self, record: MatchRecord) -> None:
        """保存指定记录的独立快照。"""

        self.records[record.id] = record.model_copy(deep=True)


class _MultiTargetQuery:
    """返回多条批量改配可选整理历史目标。"""

    def __init__(self, targets: list[SimpleNamespace]) -> None:
        """按历史标识保存目标。"""

        self.targets = {target.history_id: target for target in targets}

    async def get_target(self, history_id: int) -> SimpleNamespace | None:
        """按整理历史标识返回目标。"""

        return self.targets.get(history_id)

    async def list_all_targets(self) -> list[SimpleNamespace]:
        """返回全部目标用于精确自动建议。"""

        return list(self.targets.values())

    def resolve_actual_subtitle_path(self, target: SubtitleTarget) -> PathMappingResolution:
        """返回未配置映射时的原始目标路径。"""

        return PathMappingResolution(original_path=target.target_path, resolved_path=target.target_path)


class _NoAccessDependency:
    """在批量数量校验测试中拒绝访问外部依赖。"""

    def __getattr__(self, name: str) -> object:
        """把任何意外依赖访问转换为明确的测试失败。"""

        raise AssertionError(f"批量数量校验前不应访问外部依赖：{name}")


def _committer(
    store: object,
    filesystem: object,
    records: list[MatchRecord],
    format_priority: list[str] | None = None,
    source_priority: list[str] | None = None,
) -> RecordCommitter:
    """通过公开记录提交 facade 创建共享的库存与变更边界。"""

    return RecordCommitter(
        store,
        filesystem,
        records,
        format_priority or ["SRT"],
        source_priority or ["assrt"],
    )


def _maintenance(
    *,
    store: object,
    filesystem: object,
    targets: object,
    inventory: RecordCommitter | object | None = None,
    mutation_lock: object | None = None,
    publisher: object | None = None,
) -> RecordMaintenance:
    """通过公开 facade 装配改配用例；可替换依赖保持在外部端口。"""

    del mutation_lock
    if isinstance(inventory, RecordCommitter):
        committer = inventory
    else:
        stored_records = getattr(store, "records", None)
        records = list(stored_records.values()) if isinstance(stored_records, dict) else []
        if not records and isinstance(getattr(store, "record", None), MatchRecord):
            records = [store.record]
        committer = _committer(store, filesystem, records)
    return committer.maintenance(targets, publisher)


RetargetService = _maintenance


class _Inventory:
    """保留历史测试调用形状的无状态占位输入。"""


def _batch_size_validation_service() -> RecordMaintenance:
    """构造仅允许执行批量数量校验的改配服务。"""

    dependency: Any = _NoAccessDependency()
    return _committer(dependency, dependency, []).maintenance(dependency)


class _CapturePublisher:
    """收集改配字幕落盘事件的发布器替身。"""

    def __init__(
        self,
        *,
        error: BaseException | None = None,
        fail_on_calls: set[int] | None = None,
        context_exited: Callable[[], bool] | None = None,
    ) -> None:
        """保存可选发布异常、失败序号与事务退出观察器。"""

        self.error = error
        self.fail_on_calls = fail_on_calls or set()
        self.call_count = 0
        self.context_exited = context_exited
        self.events: list[SubtitleWrittenEvent] = []
        self.observed_context_exit: list[bool] = []

    async def publish(self, event: SubtitleWrittenEvent) -> None:
        """收集事件或抛出预设发布异常。"""

        self.call_count += 1
        if self.error is not None or self.call_count in self.fail_on_calls:
            raise self.error or RuntimeError("模拟发布失败")
        if self.context_exited is not None:
            self.observed_context_exit.append(self.context_exited())
        self.events.append(event)


class _FailOnceAfterMutationStore(_RecordStore):
    """首次保存先覆盖内存快照再模拟持久化失败。"""

    def __init__(self, record: MatchRecord) -> None:
        """保存初始记录并初始化保存次数。"""

        super().__init__(record)
        self.save_calls = 0

    async def save_record(self, record: MatchRecord) -> None:
        """首次保存模拟 PluginStore 的先改内存后持久化失败语义。"""

        self.record = record.model_copy(deep=True)
        self.save_calls += 1
        if self.save_calls == 1:
            raise RuntimeError("模拟持久化失败")


class _FailAndRejectRollbackStore(_RecordStore):
    """首次保存和后续记录回滚均失败的持久化替身。"""

    def __init__(self, record: MatchRecord) -> None:
        """保存初始记录并初始化保存次数。"""

        super().__init__(record)
        self.save_calls = 0

    async def save_record(self, record: MatchRecord) -> None:
        """每次保存都覆盖内存后失败，模拟补偿风险。"""

        self.record = record.model_copy(deep=True)
        self.save_calls += 1
        raise RuntimeError("模拟持久化与回滚均失败")


class _BlockingSubtitleFiles(SubtitleFiles):
    """在目标字幕复制前暂停，用于验证库存消费互斥。"""

    def __init__(self, data_root: Path, allowed_formats: set[str]) -> None:
        """初始化文件服务与测试同步事件。"""

        super().__init__(data_root, allowed_formats)
        self.write_started = asyncio.Event()
        self.allow_write = asyncio.Event()

    async def write_media_subtitle(self, source: Path, target: Path) -> Path:
        """通知测试改配已持有保留凭据，等待允许后继续复制。"""

        self.write_started.set()
        await self.allow_write.wait()
        return await super().write_media_subtitle(source, target)


def _record(path: Path, target_video: Path) -> MatchRecord:
    """构造已匹配记录。"""

    return MatchRecord(
        id="record-1",
        subtitle_file_name=path.name,
        format="SRT",
        media_title="旧媒体",
        year=2020,
        media_type=MediaType.MOVIE,
        status=RecordStatus.MATCHED,
        source=SubtitleSource.MOVIEPILOT,
        location=FileLocation.MEDIA_DIRECTORY,
        path=path,
        target_path=target_video,
        final_subtitle_path=path,
        source_task_id="task-1",
        candidate_key="safe-candidate",
        language="简体中文",
    )


async def test_matched_record_can_retarget_to_new_media_and_append_history(tmp_path: Path) -> None:
    """已匹配字幕移动到新目标并以标准名原地更新记录。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(old_subtitle, b"subtitle body")
    await _write(new_video, b"new video")
    record = _record(old_subtitle, old_video)
    store = _RecordStore(record)
    publisher = _CapturePublisher()
    target_context = SubtitleTarget(
        title="新媒体",
        year=2024,
        media_type=MediaType.MOVIE,
        tmdb_id=2468,
        imdb_id="tt02468",
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, target_context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    expected = new_video.with_name("New.Movie.chi.zh-cn.srt")
    assert result.success is True
    assert result.record == store.record
    assert store.record.status is RecordStatus.MATCHED
    assert store.record.location is FileLocation.MEDIA_DIRECTORY
    assert store.record.path == expected
    assert store.record.target_path == new_video
    assert store.record.target_history_id == 42
    assert store.record.history_target_path == new_video
    assert store.record.final_subtitle_path == expected
    assert store.record.subtitle_file_name == expected.name
    assert store.record.media_title == "新媒体"
    assert store.record.year == 2024
    assert store.record.tmdb_id == 2468
    assert store.record.imdb_id == "tt02468"
    assert len(store.record.retarget_history) == 1
    history = store.record.retarget_history[0]
    assert history.old_target_path == old_video
    assert history.old_subtitle_path == old_subtitle
    assert history.new_target_path == new_video
    assert history.new_matched_path_mapping is None
    assert history.new_subtitle_path == expected
    assert not await AsyncPath(old_subtitle).exists()
    assert await AsyncPath(expected).read_bytes() == b"subtitle body"
    assert publisher.events == [
        SubtitleWrittenEvent(
            plugin_id="SubtitleAssistant",
            operation=SubtitleWrittenOperation.RETARGET,
            task_id=None,
            record_id=record.id,
            target_path=new_video,
            subtitle_path=expected,
        )
    ]


async def test_retarget_publisher_failure_does_not_change_successful_result(tmp_path: Path) -> None:
    """改配成功后的发布失败不改变文件、记录或成功结果。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(old_subtitle, b"subtitle body")
    await _write(new_video, b"new video")
    record = _record(old_subtitle, old_video)
    store = _RecordStore(record)
    publisher = _CapturePublisher(error=RuntimeError("发布失败"))
    target_context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, target_context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    expected = new_video.with_name("New.Movie.chi.zh-cn.srt")
    assert result.success is True
    assert store.record.final_subtitle_path == expected
    assert await AsyncPath(expected).is_file()
    assert not await AsyncPath(old_subtitle).exists()


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [("same_target", "same_target"), ("destination_conflict", "destination_conflict")],
    ids=("same_target", "destination_conflict"),
)
async def test_retarget_precondition_failure_does_not_publish_event(
    tmp_path: Path,
    failure: str,
    expected_error: str,
) -> None:
    """同路径或目标冲突在事务开始前失败时不发布事件。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(new_video, b"new video")
    target_video = old_video if failure == "same_target" else new_video
    source_subtitle = old_video.with_name("Old.Movie.chi.zh-cn.srt") if failure == "same_target" else old_subtitle
    await _write(source_subtitle, b"subtitle body")
    if failure == "destination_conflict":
        await _write(new_video.with_name("New.Movie.chi.zh-cn.srt"), b"existing subtitle")
    record = _record(source_subtitle, old_video)
    store = _RecordStore(record)
    publisher = _CapturePublisher()
    context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=target_video,
        target_file_name=target_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    assert result.success is False
    assert result.error_code == expected_error
    assert publisher.events == []


async def test_retarget_missing_source_does_not_publish_event(tmp_path: Path) -> None:
    """原字幕文件操作失败时不发布事件。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    missing_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(new_video, b"new video")
    record = _record(missing_subtitle, old_video)
    store = _RecordStore(record)
    publisher = _CapturePublisher()
    context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    assert result.success is False
    assert result.error_code == "file_operation_failed"
    assert publisher.events == []


async def test_staged_record_retarget_removes_old_inventory_entry(tmp_path: Path) -> None:
    """暂存字幕改配成功后不再能被旧媒体库存消费。"""

    data_root = tmp_path / "plugin-data"
    staged_file = data_root / "staged" / "record-staged.srt"
    new_video = tmp_path / "media" / "Episode.S01E02.mkv"
    await _write(staged_file, b"staged subtitle")
    await _write(new_video, b"new video")
    record = MatchRecord(
        id="record-staged",
        subtitle_file_name="candidate.srt",
        format="SRT",
        media_title="旧剧集",
        media_type=MediaType.TV,
        season=1,
        episode=1,
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        package_scope=PackageScope.SEASON_PACK,
        location=FileLocation.PLUGIN_DATA,
        path=Path("staged/record-staged.srt"),
        canonical_identity_type=MediaIdentityKind.TMDB,
        canonical_identity_value="100",
        tmdb_id=100,
        target_path=tmp_path / "old" / "Episode.S01E01.mkv",
        source_task_id="task-1",
        candidate_key="assrt:staged",
        language="简体中文",
    )
    store = _RecordStore(record)
    filesystem = SubtitleFiles(data_root, {"srt"})
    inventory = _committer(
        store=store,
        filesystem=filesystem,
        records=[record],
        format_priority=["SRT"],
        source_priority=["assrt"],
    )
    new_context = SubtitleTarget(
        title="新剧集",
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=200,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=filesystem,
        inventory=inventory,
        targets=_TargetQuery(7, new_context),
    )

    result = await service.retarget(record.id, 7)
    old_context = SubtitleTarget(
        title="旧剧集",
        media_type=MediaType.TV,
        season=1,
        episode=1,
        tmdb_id=100,
        target_path=tmp_path / "old" / "Episode.S01E01.mkv",
        target_file_name="Episode.S01E01.mkv",
        target_storage="local",
    )
    old_inventory = await inventory.consume(old_context, "future-task")

    assert result.success is True
    assert old_inventory.matched is False
    assert store.record.status is RecordStatus.MATCHED
    assert store.record.media_title == "新剧集"
    assert store.record.season == 1
    assert store.record.episode == 2
    assert store.record.canonical_identity_value == "200"
    assert not await AsyncPath(staged_file).exists()


async def test_staged_movie_without_inventory_key_can_retarget(tmp_path: Path) -> None:
    """不进入季集库存的电影暂存记录仍可正常改配。"""

    data_root = tmp_path / "plugin-data"
    staged_file = data_root / "staged" / "movie.srt"
    new_video = tmp_path / "media" / "New.Movie.mkv"
    await _write(staged_file, b"movie subtitle")
    await _write(new_video, b"new movie")
    record = MatchRecord(
        id="movie-staged",
        subtitle_file_name="movie.srt",
        format="SRT",
        media_title="旧电影",
        media_type=MediaType.MOVIE,
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        location=FileLocation.PLUGIN_DATA,
        path=Path("staged/movie.srt"),
        canonical_identity_type=MediaIdentityKind.TMDB,
        canonical_identity_value="100",
        tmdb_id=100,
        source_task_id="task-1",
        candidate_key="assrt:movie",
        language="简体中文",
    )
    store = _RecordStore(record)
    filesystem = SubtitleFiles(data_root, {"srt"})
    inventory = _committer(
        store=store,
        filesystem=filesystem,
        records=[record],
        format_priority=["SRT"],
        source_priority=["assrt"],
    )
    context = SubtitleTarget(
        title="新电影",
        media_type=MediaType.MOVIE,
        tmdb_id=200,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=filesystem,
        inventory=inventory,
        targets=_TargetQuery(8, context),
    )

    result = await service.retarget(record.id, 8)

    assert result.success is True
    assert store.record.status is RecordStatus.MATCHED
    assert store.record.target_history_id == 8
    assert not await AsyncPath(staged_file).exists()


async def test_staged_retarget_blocks_concurrent_inventory_consumption(tmp_path: Path) -> None:
    """暂存字幕改配期间库存消费等待，提交后不能再重复落盘。"""

    data_root = tmp_path / "plugin-data"
    staged_file = data_root / "staged" / "record-staged.srt"
    new_video = tmp_path / "new" / "Episode.S01E02.mkv"
    await _write(staged_file, b"staged subtitle")
    await _write(new_video, b"new video")
    record = MatchRecord(
        id="record-staged",
        subtitle_file_name="candidate.srt",
        format="SRT",
        media_title="旧剧集",
        media_type=MediaType.TV,
        season=1,
        episode=1,
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        package_scope=PackageScope.SEASON_PACK,
        location=FileLocation.PLUGIN_DATA,
        path=Path("staged/record-staged.srt"),
        canonical_identity_type=MediaIdentityKind.TMDB,
        canonical_identity_value="100",
        tmdb_id=100,
        source_task_id="task-1",
        candidate_key="assrt:staged",
        language="简体中文",
    )
    store = _RecordStore(record)
    filesystem = _BlockingSubtitleFiles(data_root, {"srt"})
    inventory = _committer(
        store=store,
        filesystem=filesystem,
        records=[record],
        format_priority=["SRT"],
        source_priority=["assrt"],
    )
    new_context = SubtitleTarget(
        title="新剧集",
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=200,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    old_context = SubtitleTarget(
        title="旧剧集",
        media_type=MediaType.TV,
        season=1,
        episode=1,
        tmdb_id=100,
        target_path=tmp_path / "old" / "Episode.S01E01.mkv",
        target_file_name="Episode.S01E01.mkv",
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=filesystem,
        inventory=inventory,
        targets=_TargetQuery(7, new_context),
    )

    retarget_task = asyncio.create_task(service.retarget(record.id, 7))
    await filesystem.write_started.wait()
    consume_task = asyncio.create_task(inventory.consume(old_context, "automatic-task"))
    await asyncio.sleep(0)
    assert consume_task.done() is False

    filesystem.allow_write.set()
    result = await retarget_task
    consumed = await consume_task

    assert result.success is True
    assert consumed.matched is False
    assert store.record.status is RecordStatus.MATCHED
    old_destination = Path(old_context.target_path).with_name("Episode.S01E01.chi.zh-cn.srt")
    assert not await AsyncPath(old_destination).exists()


async def test_retarget_persistence_failure_restores_old_store_and_files(tmp_path: Path) -> None:
    """记录持久化失败即使已覆盖内存，也恢复原记录和原字幕。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(old_subtitle, b"subtitle body")
    await _write(new_video, b"new video")
    record = _record(old_subtitle, old_video)
    original = record.model_copy(deep=True)
    store = _FailOnceAfterMutationStore(record)
    publisher = _CapturePublisher()
    context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        tmdb_id=2468,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    expected = new_video.with_name("New.Movie.chi.zh-cn.srt")
    assert result.success is False
    assert result.error_code == "file_operation_failed"
    assert result.consistency_risk is False
    assert store.save_calls == 2
    assert store.record == original
    assert await AsyncPath(old_subtitle).read_bytes() == b"subtitle body"
    assert not await AsyncPath(expected).exists()
    assert publisher.events == []


async def test_retarget_incomplete_compensation_does_not_publish_event(tmp_path: Path) -> None:
    """记录回滚失败形成一致性风险时仍不发布事件。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(old_subtitle, b"subtitle body")
    await _write(new_video, b"new video")
    record = _record(old_subtitle, old_video)
    store = _FailAndRejectRollbackStore(record)
    publisher = _CapturePublisher()
    context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    assert result.success is False
    assert result.error_code == "file_operation_failed"
    assert result.consistency_risk is True
    assert store.save_calls == 2
    assert publisher.events == []


async def test_retarget_success_log_is_chinese_fact_sentence(
    tmp_path: Path,
) -> None:
    """改配成功后经公开维护 facade 返回已提交记录。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "New.Movie.mkv"
    await _write(old_video, b"old video")
    await _write(old_subtitle, b"subtitle")
    await _write(new_video, b"new video")
    record = _record(old_subtitle, old_video)
    target_context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=new_video,
        target_file_name=new_video.name,
        target_storage="local",
    )
    publisher = _CapturePublisher()
    service = RetargetService(
        store=_RecordStore(record),
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, target_context),
        publisher=publisher,
    )

    result = await service.retarget(record.id, 42)

    assert result.success is True
    assert [event.record_id for event in publisher.events] == [record.id]


async def test_retarget_preview_is_advisory_and_submit_recomputes_current_mapping(
    tmp_path: Path,
) -> None:
    """改配预览不作为写入凭据，确认时重新读取当前路径映射。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    history_root = tmp_path / "history"
    preview_root = tmp_path / "preview-mount"
    submit_root = tmp_path / "submit-mount"
    history_target = history_root / "New.Movie.mkv"
    await _write(old_video, b"video")
    await _write(old_subtitle, b"subtitle")
    await AsyncPath(preview_root).mkdir(parents=True)
    await AsyncPath(submit_root).mkdir(parents=True)
    record = _record(old_subtitle, old_video)
    record.target_history_id = 42
    record.history_target_path = history_target
    store = _RecordStore(record)
    target_context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        tmdb_id=2468,
        target_path=history_target,
        target_file_name=history_target.name,
        target_storage="local",
    )
    config = PluginConfig(path_mappings=(PathMapping(str(history_root), str(preview_root)),))

    def resolve_target(target: SubtitleTarget) -> PathMappingResolution:
        """按当前测试配置模拟目标能力的实际路径解析。"""

        mapping = config.path_mappings[0]
        return PathMappingResolution(
            original_path=target.target_path,
            resolved_path=mapping.target_prefix / target.target_path.relative_to(mapping.source_prefix),
            mapping=mapping,
        )

    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(42, target_context, resolver=resolve_target),
    )

    preview_result = await service.preview(record.id, 42)

    assert preview_result.success is True
    assert preview_result.preview is not None
    assert preview_result.preview.history_target_path == history_target
    assert preview_result.preview.target_path == preview_root / history_target.name
    assert preview_result.preview.directory_available is True

    config.path_mappings = (PathMapping(str(history_root), str(submit_root)),)
    result = await service.retarget(record.id, 42)

    expected = submit_root / "New.Movie.chi.zh-cn.srt"
    assert result.success is True
    assert store.record.target_history_id == 42
    assert store.record.history_target_path == history_target
    assert store.record.target_path == submit_root / history_target.name
    assert store.record.target_file_exists is False
    assert store.record.final_subtitle_path == expected
    assert store.record.retarget_history[0].new_matched_path_mapping == PathMappingSnapshot(
        source_prefix=history_root,
        target_prefix=submit_root,
    )
    assert await AsyncPath(expected).is_file()
    assert not await AsyncPath(preview_root / expected.name).exists()


async def test_retarget_unavailable_directory_keeps_record_and_file_unchanged(
    tmp_path: Path,
) -> None:
    """改配目标目录不可用时返回稳定原因并保持原记录和字幕。"""

    old_video = tmp_path / "old" / "Old.Movie.mkv"
    old_subtitle = old_video.with_name("Old.Movie.default.chi.zh-cn.srt")
    missing_target = tmp_path / "offline" / "New.Movie.mkv"
    await _write(old_video, b"video")
    await _write(old_subtitle, b"subtitle")
    record = _record(old_subtitle, old_video)
    store = _RecordStore(record)
    publisher = _CapturePublisher()
    context = SubtitleTarget(
        title="新媒体",
        media_type=MediaType.MOVIE,
        target_path=missing_target,
        target_file_name=missing_target.name,
        target_storage="local",
    )
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_TargetQuery(9, context),
        publisher=publisher,
    )
    original = store.record.model_copy(deep=True)

    preview = await service.preview(record.id, 9)
    result = await service.retarget(record.id, 9)

    assert preview.preview is not None
    assert preview.preview.directory_available is False
    assert result.success is False
    assert result.error_code == "target_directory_unavailable"
    assert store.record == original
    assert await AsyncPath(old_subtitle).read_bytes() == b"subtitle"
    assert publisher.events == []


async def test_batch_preview_suggests_only_unique_exact_target(tmp_path: Path) -> None:
    """批量预览只为媒体身份、类型和季集唯一精确匹配的记录建议目标。"""

    old_video = tmp_path / "old" / "Episode.S01E02.mkv"
    old_subtitle = old_video.with_name("Episode.S01E02.default.chi.zh-cn.srt")
    new_video = tmp_path / "new" / "Episode.S01E02.mkv"
    await _write(old_video, b"video")
    await _write(old_subtitle, b"subtitle")
    await AsyncPath(new_video.parent).mkdir(parents=True)
    record = _record(old_subtitle, old_video)
    record.media_type = MediaType.TV
    record.season = 1
    record.episode = 2
    record.tmdb_id = 100
    target = SimpleNamespace(
        history_id=9,
        context=SubtitleTarget(
            title="目标剧集",
            media_type=MediaType.TV,
            season=1,
            episode=2,
            tmdb_id=100,
            target_path=new_video,
            target_file_name=new_video.name,
            target_storage="local",
        ),
    )
    service = RetargetService(
        store=_MultiRecordStore([record]),
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_MultiTargetQuery([target]),
    )

    preview = await service.preview_batch([RetargetMapping(record_id=record.id)])

    assert preview.executable is True
    assert preview.items[0].target_history_id == 9
    assert preview.items[0].current_subtitle_path == old_subtitle
    assert preview.items[0].preview is not None
    assert preview.items[0].preview.final_subtitle_path.name == "Episode.S01E02.chi.zh-cn.srt"


async def test_batch_preview_requires_manual_target_when_exact_match_is_ambiguous(
    tmp_path: Path,
) -> None:
    """多个精确整理历史目标并存时批量预览不擅自选择。"""

    old_video = tmp_path / "old" / "Movie.mkv"
    old_subtitle = old_video.with_name("Movie.default.chi.zh-cn.srt")
    await _write(old_video, b"video")
    await _write(old_subtitle, b"subtitle")
    record = _record(old_subtitle, old_video)
    record.tmdb_id = 100
    targets = [
        SimpleNamespace(
            history_id=history_id,
            context=SubtitleTarget(
                title="目标电影",
                media_type=MediaType.MOVIE,
                tmdb_id=100,
                target_path=tmp_path / f"target-{history_id}" / "Movie.mkv",
                target_file_name="Movie.mkv",
                target_storage="local",
            ),
        )
        for history_id in (1, 2)
    ]
    service = RetargetService(
        store=_MultiRecordStore([record]),
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_MultiTargetQuery(targets),
    )

    preview = await service.preview_batch([RetargetMapping(record_id=record.id)])

    assert preview.executable is False
    assert preview.items[0].error_code == "target_required"


async def test_batch_preview_never_suggests_unknown_media_type(tmp_path: Path) -> None:
    """媒体类型不完整时即使数字媒体 ID 相同也不自动建议目标。"""

    old_video = tmp_path / "old" / "Unknown.mkv"
    old_subtitle = old_video.with_name("Unknown.default.chi.zh-cn.srt")
    record = _record(old_subtitle, old_video)
    record.media_type = MediaType.UNKNOWN
    record.tmdb_id = 100
    target = SimpleNamespace(
        history_id=1,
        context=SubtitleTarget(
            title="未知类型目标",
            media_type=MediaType.UNKNOWN,
            tmdb_id=100,
            target_path=tmp_path / "target" / "Unknown.mkv",
            target_file_name="Unknown.mkv",
            target_storage="local",
        ),
    )
    service = RetargetService(
        store=_MultiRecordStore([record]),
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_MultiTargetQuery([target]),
    )

    preview = await service.preview_batch([RetargetMapping(record_id=record.id)])

    assert preview.executable is False
    assert preview.items[0].current_subtitle_path == old_subtitle
    assert preview.items[0].error_code == "target_required"


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        [
            RetargetMapping(record_id=f"record-{index}", target_history_id=index)
            for index in range(MAX_BATCH_RETARGET_MAPPINGS + 1)
        ],
    ],
    ids=("empty", "over_limit"),
)
async def test_preview_batch_rejects_invalid_mapping_count_before_dependency_access(
    mappings: list[RetargetMapping],
) -> None:
    """批量预览在读取存储或文件前拒绝空批次和超限批次。"""

    service = _batch_size_validation_service()

    with pytest.raises(
        ValueError,
        match=(f"批量改配映射数量必须在 {MIN_BATCH_RETARGET_MAPPINGS} 至 {MAX_BATCH_RETARGET_MAPPINGS} 条之间"),
    ):
        await service.preview_batch(mappings)


async def test_preview_batch_accepts_maximum_mapping_count() -> None:
    """批量预览允许恰好一百条映射进入后续预检。"""

    service = _batch_size_validation_service()
    mappings = [
        RetargetMapping(record_id=f"record-{index}", target_history_id=index)
        for index in range(MAX_BATCH_RETARGET_MAPPINGS)
    ]
    with pytest.raises(AssertionError, match="不应访问外部依赖"):
        await service.preview_batch(mappings)


@pytest.mark.parametrize(
    "mappings",
    [
        [],
        [
            RetargetMapping(record_id=f"record-{index}", target_history_id=index)
            for index in range(MAX_BATCH_RETARGET_MAPPINGS + 1)
        ],
    ],
    ids=("empty", "over_limit"),
)
async def test_retarget_batch_rejects_invalid_mapping_count_before_dependency_access(
    mappings: list[RetargetMapping],
) -> None:
    """批量提交在读取存储或文件前拒绝空批次和超限批次。"""

    service = _batch_size_validation_service()

    with pytest.raises(
        ValueError,
        match=(f"批量改配映射数量必须在 {MIN_BATCH_RETARGET_MAPPINGS} 至 {MAX_BATCH_RETARGET_MAPPINGS} 条之间"),
    ):
        await service.retarget_batch(mappings)


async def test_batch_retarget_preflight_failure_executes_nothing(tmp_path: Path) -> None:
    """批量预检存在源文件缺失时不移动任何通过预检的记录。"""

    target_dir = tmp_path / "targets"
    await AsyncPath(target_dir).mkdir(parents=True)
    target_one = target_dir / "One.mkv"
    target_two = target_dir / "Two.mkv"
    record_one_path = tmp_path / "old" / "One.default.chi.zh-cn.srt"
    record_two_path = tmp_path / "old" / "Missing.default.chi.zh-cn.srt"
    await _write(record_one_path, b"one")
    record_one = _record(record_one_path, tmp_path / "old" / "One.mkv")
    record_one.id = "record-one"
    record_two = _record(record_two_path, tmp_path / "old" / "Two.mkv")
    record_two.id = "record-two"
    targets = [
        SimpleNamespace(
            history_id=1,
            context=SubtitleTarget(
                title="一",
                media_type=MediaType.MOVIE,
                target_path=target_one,
                target_file_name=target_one.name,
                target_storage="local",
            ),
        ),
        SimpleNamespace(
            history_id=2,
            context=SubtitleTarget(
                title="二",
                media_type=MediaType.MOVIE,
                target_path=target_two,
                target_file_name=target_two.name,
                target_storage="local",
            ),
        ),
    ]
    store = _MultiRecordStore([record_one, record_two])
    publisher = _CapturePublisher()
    service = RetargetService(
        store=store,
        filesystem=SubtitleFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_MultiTargetQuery(targets),
        publisher=publisher,
    )

    result = await service.retarget_batch(
        [
            RetargetMapping(record_id="record-one", target_history_id=1),
            RetargetMapping(record_id="record-two", target_history_id=2),
        ]
    )

    assert result.started is False
    assert result.items == []
    assert await AsyncPath(record_one_path).is_file()
    assert not await AsyncPath(target_one.with_name("One.chi.zh-cn.srt")).exists()
    assert publisher.events == []


@pytest.mark.parametrize(
    ("failed_record_ids", "expected_success_ids"),
    [
        ({"record-two"}, {"record-one", "record-three"}),
        ({"record-one", "record-two", "record-three"}, set()),
    ],
    ids=("partial_failure", "all_failure"),
)
@pytest.mark.parametrize(
    ("publisher_error", "fail_on_calls"),
    [
        (None, set()),
        (RuntimeError("模拟发布失败"), set()),
        (None, {2}),
    ],
    ids=("publisher_ok", "publisher_failure", "middle_publisher_failure"),
)
async def test_batch_retarget_continues_after_item_or_subtitle_written_event_failure(
    tmp_path: Path,
    failed_record_ids: set[str],
    expected_success_ids: set[str],
    publisher_error: BaseException | None,
    fail_on_calls: set[int],
) -> None:
    """批量单项异常或字幕落盘事件发布异常均不影响其他映射结果。"""

    target_dir = tmp_path / "targets"
    await AsyncPath(target_dir).mkdir(parents=True)
    record_paths = [
        tmp_path / "old" / "One.default.chi.zh-cn.srt",
        tmp_path / "old" / "Two.default.chi.zh-cn.srt",
        tmp_path / "old" / "Three.default.chi.zh-cn.srt",
    ]
    for path in record_paths:
        await _write(path, path.name.encode())
    records = [_record(path, tmp_path / "old" / f"{index}.mkv") for index, path in enumerate(record_paths)]
    records[0].id = "record-one"
    records[1].id = "record-two"
    records[2].id = "record-three"
    target_paths = [target_dir / "One.mkv", target_dir / "Two.mkv", target_dir / "Three.mkv"]
    targets = [
        SimpleNamespace(
            history_id=index,
            context=SubtitleTarget(
                title=f"目标 {index}",
                media_type=MediaType.MOVIE,
                target_path=path,
                target_file_name=path.name,
                target_storage="local",
            ),
        )
        for index, path in enumerate(target_paths, start=1)
    ]
    store = _MultiRecordStore(records)
    publisher = _CapturePublisher(error=publisher_error, fail_on_calls=fail_on_calls)
    failed_paths = {record_paths[index] for index, record in enumerate(records) if record.id in failed_record_ids}

    class _FailingFiles(SubtitleFiles):
        """用外部文件端口失败模拟单项运行期改配错误。"""

        async def write_media_subtitle(self, source: Path, target: Path) -> Path:
            """仅拒绝预设源字幕，其余字幕按真实规则写入。"""

            if source in failed_paths:
                raise OSError("模拟单条批量改配异常")
            return await super().write_media_subtitle(source, target)

    service = RetargetService(
        store=store,
        filesystem=_FailingFiles(tmp_path / "plugin-data", {"srt"}),
        inventory=_Inventory(),
        targets=_MultiTargetQuery(targets),
        publisher=publisher,
    )

    result = await service.retarget_batch(
        [
            RetargetMapping(record_id="record-one", target_history_id=1),
            RetargetMapping(record_id="record-two", target_history_id=2),
            RetargetMapping(record_id="record-three", target_history_id=3),
        ]
    )

    assert result.started is True
    assert result.success_count == len(expected_success_ids)
    assert result.failure_count == len(failed_record_ids)
    assert all(
        item.result.error_code == "file_operation_failed" for item in result.items if item.record_id in failed_record_ids
    )
    for record_id, record_path, target_path in zip(
        ("record-one", "record-two", "record-three"), record_paths, target_paths, strict=True
    ):
        if record_id in expected_success_ids:
            assert await AsyncPath(target_path.with_name(f"{target_path.stem}.chi.zh-cn.srt")).is_file()
        else:
            assert await AsyncPath(record_path).is_file()
    attempted_success_ids = [
        record_id for record_id in ("record-one", "record-two", "record-three") if record_id in expected_success_ids
    ]
    expected_event_ids = (
        []
        if publisher_error is not None
        else [
            record_id
            for call_number, record_id in enumerate(attempted_success_ids, start=1)
            if call_number not in fail_on_calls
        ]
    )
    assert publisher.call_count == len(attempted_success_ids)
    assert [event.record_id for event in publisher.events] == expected_event_ids
    assert all(event.operation is SubtitleWrittenOperation.RETARGET for event in publisher.events)
    assert all(event.task_id is None for event in publisher.events)
