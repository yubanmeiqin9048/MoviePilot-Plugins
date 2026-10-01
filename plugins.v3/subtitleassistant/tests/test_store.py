"""PluginData 四分区版本、损坏保护、迁移剥离与保留规则测试。"""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.plugins.subtitleassistant.schemas.attribution import FileAttributionMethod
from app.plugins.subtitleassistant.schemas.record import FileLocation, MatchRecord, RecordStatus
from app.plugins.subtitleassistant.schemas.source import SourceHealth, SourceStatus, SubtitleSource
from app.plugins.subtitleassistant.schemas.task import SubtitleTask, TaskStatus
from app.plugins.subtitleassistant.schemas.target import PathMappingSnapshot
from app.plugins.subtitleassistant.store import PluginDataStore, StoreInitializationError


class FakePlugin:
    """以内存字典模拟 MoviePilot 插件数据接口。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        """创建带可选初始分区的内存插件。"""

        self.data = deepcopy(data or {})
        self.get_calls: list[str] = []
        self.save_calls: list[tuple[str, Any]] = []
        self.async_get_calls: list[str] = []
        self.async_save_calls: list[tuple[str, Any]] = []
        self.fail_async_save_keys: set[str] = set()

    def get_data(self, key: str) -> Any:
        """返回分区数据的独立副本。"""

        self.get_calls.append(key)
        return deepcopy(self.data.get(key))

    def save_data(self, key: str, value: Any) -> None:
        """保存分区数据并记录写入。"""

        copied = deepcopy(value)
        self.data[key] = copied
        self.save_calls.append((key, copied))

    async def async_get_data(self, key: str) -> Any:
        """通过异步接口返回分区数据的独立副本。"""

        self.async_get_calls.append(key)
        return deepcopy(self.data.get(key))

    async def async_save_data(self, key: str, value: Any) -> None:
        """通过异步接口保存分区数据并记录写入。"""

        copied = deepcopy(value)
        self.async_save_calls.append((key, copied))
        if key in self.fail_async_save_keys:
            raise OSError("模拟异步 PluginData 写入失败")
        self.data[key] = copied


class BlockingFakePlugin(FakePlugin):
    """阻塞首次异步保存以观测快照发布和写入串行化。"""

    def __init__(self, data: dict[str, Any]) -> None:
        """创建带首次保存闸门的内存插件。"""

        super().__init__(data)
        self.async_save_entries: list[str] = []
        self.first_save_started = asyncio.Event()
        self.release_first_save = asyncio.Event()

    async def async_save_data(self, key: str, value: Any) -> None:
        """记录保存入口并阻塞第一次调用。"""

        self.async_save_entries.append(key)
        if len(self.async_save_entries) == 1:
            self.first_save_started.set()
            await self.release_first_save.wait()
        await super().async_save_data(key, value)


def _partition(items: Any, version: int = PluginDataStore.VERSION) -> dict[str, Any]:
    """构造版本化 PluginData 分区。"""

    return {"version": version, "items": deepcopy(items)}


def _task(task_id: str, status: TaskStatus, created_at: datetime) -> SubtitleTask:
    """构造保留规则使用的任务。"""

    terminal = status in {
        TaskStatus.SUCCESS,
        TaskStatus.SKIPPED,
        TaskStatus.FAILED,
        TaskStatus.INTERRUPTED,
    }
    return SubtitleTask(
        id=task_id,
        media_title="测试媒体",
        target_file_name="Test.S01E01.mkv",
        target_path=Path("/media/Test.S01E01.mkv"),
        status=status,
        created_at=created_at,
        finished_at=created_at if terminal else None,
    )


def _record(record_id: str, status: RecordStatus, created_at: datetime) -> MatchRecord:
    """构造保留规则使用的匹配记录。"""

    return MatchRecord(
        id=record_id,
        subtitle_file_name=f"{record_id}.srt",
        format="SRT",
        status=status,
        source=SubtitleSource.OPENSUBTITLES,
        location=FileLocation.MEDIA_DIRECTORY if status is RecordStatus.MATCHED else FileLocation.PLUGIN_DATA,
        path=Path(f"/records/{record_id}.srt"),
        source_task_id="task-1",
        candidate_key=f"candidate:{record_id}",
        language="zh-cn",
        created_at=created_at,
        updated_at=created_at,
    )


def _valid_partitions() -> dict[str, Any]:
    """返回四个当前版本的空分区。"""

    return {
        PluginDataStore.TASKS_KEY: _partition([]),
        PluginDataStore.RECORDS_KEY: _partition([]),
        PluginDataStore.SOURCE_STATUS_KEY: _partition([]),
        PluginDataStore.CREDENTIALS_KEY: _partition({}),
    }


def test_initialize_creates_all_four_current_version_partitions() -> None:
    """缺失数据初始化为四个当前版本空分区。"""

    plugin = FakePlugin()
    store = PluginDataStore(plugin)

    store.initialize()

    assert plugin.data == _valid_partitions()
    assert set(plugin.data) == {
        "tasks",
        "records",
        "source_status",
        "credentials",
    }


@pytest.mark.anyio
async def test_record_save_failure_restores_complete_in_memory_snapshot() -> None:
    """记录分区持久化失败时不保留新记录或保留规则副作用。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    original = _record("record-old", RecordStatus.MATCHED, now)
    plugin = FakePlugin(
        {
            **_valid_partitions(),
            PluginDataStore.RECORDS_KEY: _partition([original.model_dump(mode="json")]),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()
    persisted = deepcopy(plugin.data[PluginDataStore.RECORDS_KEY])
    original_async_save_data = plugin.async_save_data

    async def fail_record_partition(key: str, value: Any) -> None:
        """只拒绝记录分区写入。"""

        if key == PluginDataStore.RECORDS_KEY:
            raise OSError("模拟 PluginData 写入失败")
        await original_async_save_data(key, value)

    plugin.async_save_data = fail_record_partition  # type: ignore[method-assign]

    with pytest.raises(OSError):
        await store.save_record(_record("record-new", RecordStatus.MATCHED, now + timedelta(days=1)))

    assert store.list_records_sync() == [original]
    assert plugin.data[PluginDataStore.RECORDS_KEY] == persisted


@pytest.mark.anyio
async def test_async_record_writes_publish_after_success_and_serialize() -> None:
    """异步记录写入串行执行，保存期间 getter 继续读取旧快照。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    original = _record("record-old", RecordStatus.MATCHED, now)
    plugin = BlockingFakePlugin(
        {
            **_valid_partitions(),
            PluginDataStore.RECORDS_KEY: _partition([original.model_dump(mode="json")]),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()
    first = _record("record-first", RecordStatus.MATCHED, now + timedelta(days=1))
    second = _record("record-second", RecordStatus.MATCHED, now + timedelta(days=2))

    first_save = asyncio.create_task(store.save_record(first))
    await plugin.first_save_started.wait()
    second_save = asyncio.create_task(store.save_record(second))
    await asyncio.sleep(0)

    assert plugin.async_save_entries == [PluginDataStore.RECORDS_KEY]
    assert await store.list_records() == [original]

    plugin.release_first_save.set()
    await asyncio.gather(first_save, second_save)

    assert plugin.async_save_entries == [PluginDataStore.RECORDS_KEY, PluginDataStore.RECORDS_KEY]
    assert {item.id for item in await store.list_records()} == {original.id, first.id, second.id}
    assert plugin.save_calls == []


@pytest.mark.anyio
async def test_async_save_failure_keeps_old_memory_snapshot() -> None:
    """异步 PluginData 保存失败时不发布候选任务快照。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    original = _task("task-old", TaskStatus.SUCCESS, now)
    plugin = FakePlugin(
        {
            **_valid_partitions(),
            PluginDataStore.TASKS_KEY: _partition([original.model_dump(mode="json")]),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()
    plugin.fail_async_save_keys.add(PluginDataStore.TASKS_KEY)

    with pytest.raises(OSError):
        await store.save_task(_task("task-new", TaskStatus.FAILED, now + timedelta(days=1)))

    assert await store.list_tasks() == [original]
    assert plugin.data[PluginDataStore.TASKS_KEY] == _partition([original.model_dump(mode="json")])
    assert plugin.save_calls == []


@pytest.mark.anyio
async def test_runtime_getters_only_copy_memory_without_plugin_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通异步 getter 不切线程，也不重新读取 PluginData。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    task = _task("task-memory", TaskStatus.SUCCESS, now)
    record = _record("record-memory", RecordStatus.MATCHED, now)
    plugin = FakePlugin(
        {
            **_valid_partitions(),
            PluginDataStore.TASKS_KEY: _partition([task.model_dump(mode="json")]),
            PluginDataStore.RECORDS_KEY: _partition([record.model_dump(mode="json")]),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()
    sync_reads_after_startup = list(plugin.get_calls)
    plugin.data[PluginDataStore.TASKS_KEY] = _partition([])
    plugin.data[PluginDataStore.RECORDS_KEY] = _partition([])

    async def reject_thread_switch(*args: Any, **kwargs: Any) -> Any:
        """普通内存读取若尝试切线程就使测试失败。"""

        raise AssertionError("内存 getter 不应调用 asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", reject_thread_switch)

    assert await store.get_task(task.id) == task
    assert await store.list_tasks() == [task]
    assert await store.get_record(record.id) == record
    assert await store.list_records() == [record]
    assert plugin.get_calls == sync_reads_after_startup
    assert plugin.async_get_calls == []


def test_initialize_rejects_unsupported_partition_versions_without_overwrite() -> None:
    """既非当前也非可迁移来源的版本失败关闭且不改写原数据。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    task = _task("task-v0", TaskStatus.SUCCESS, now)
    record = _record("record-v0", RecordStatus.STAGED, now)
    status = SourceStatus(
        source=SubtitleSource.ASSRT,
        enabled=True,
        configured=True,
        health=SourceHealth.HEALTHY,
    )
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([task.model_dump(mode="json")], version=0),
            PluginDataStore.RECORDS_KEY: _partition([record.model_dump(mode="json")], version=0),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([status.model_dump(mode="json")], version=0),
            PluginDataStore.CREDENTIALS_KEY: _partition({"assrt": {"token": "secret"}}, version=0),
        }
    )
    store = PluginDataStore(plugin)

    original = dict(plugin.data)

    with pytest.raises(StoreInitializationError):
        store.initialize()

    assert plugin.data == original
    assert plugin.save_calls == []


@pytest.mark.parametrize(
    "damaged_tasks",
    [
        {"items": []},
        {"version": PluginDataStore.VERSION + 1, "items": []},
        {"version": PluginDataStore.VERSION, "items": [{}]},
    ],
)
def test_initialize_rejects_damaged_or_future_data_without_overwrite(damaged_tasks: dict[str, Any]) -> None:
    """结构损坏、模型无效或未来版本均失败关闭且保留原数据。"""

    original = _valid_partitions()
    original[PluginDataStore.TASKS_KEY] = damaged_tasks
    plugin = FakePlugin(original)
    store = PluginDataStore(plugin)

    with pytest.raises(StoreInitializationError):
        store.initialize()

    assert plugin.data == original
    assert plugin.save_calls == []


@pytest.mark.anyio
async def test_tasks_and_records_are_retained_without_cap() -> None:
    """任务与记录不再有保留上限，超过历史阈值仍全部保留。"""

    start = datetime(2024, 1, 1, tzinfo=UTC)
    terminal_tasks = [
        _task(f"terminal-{index}", TaskStatus.SUCCESS, start + timedelta(minutes=index)) for index in range(501)
    ]
    active_task = _task("active", TaskStatus.PROCESSING, start)
    matched_records = [
        _record(f"matched-{index}", RecordStatus.MATCHED, start + timedelta(minutes=index)) for index in range(1001)
    ]
    staged_record = _record("staged", RecordStatus.STAGED, start)
    unmatched_record = _record("unmatched", RecordStatus.UNMATCHED, start)
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition(
                [item.model_dump(mode="json") for item in [*terminal_tasks, active_task]]
            ),
            PluginDataStore.RECORDS_KEY: _partition(
                [item.model_dump(mode="json") for item in [*matched_records, staged_record, unmatched_record]]
            ),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([]),
            PluginDataStore.CREDENTIALS_KEY: _partition({}),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()

    newest_task = _task("terminal-new", TaskStatus.FAILED, start + timedelta(minutes=10000))
    newest_record = _record("matched-new", RecordStatus.MATCHED, start + timedelta(minutes=10000))
    await store.save_task(newest_task)
    await store.save_record(newest_record)

    tasks = await store.list_tasks()
    records = await store.list_records()
    task_ids = {item.id for item in tasks}
    record_ids = {item.id for item in records}
    assert len(tasks) == 503
    assert {"active", "terminal-new", "terminal-0", "terminal-1"} <= task_ids
    assert len(records) == 1004
    assert {"staged", "unmatched", "matched-new", "matched-0", "matched-1"} <= record_ids


def _legacy_task_payload(now: datetime) -> dict[str, Any]:
    """构造含已删审计字段、嵌套未知字段与顶层未知字段的 V2 旧任务原始数据。"""

    return {
        "id": "task-legacy",
        "media_title": "旧媒体",
        "target_file_name": "Old.S01E01.mkv",
        "target_path": "/media/Old.S01E01.mkv",
        "status": "success",
        "created_at": now.isoformat(),
        "finished_at": now.isoformat(),
        "matched_path_mapping": {
            "source_prefix": "/history",
            "target_prefix": "/media",
            "unknown_mapping_key": "旧映射备注",
        },
        "record_counts": {"matched": 2},
        "stage": "search",
        "stage_traces": [{"stage": "search", "started_at": now.isoformat()}],
        "source_runs": [{"source": "assrt", "status": "success", "candidate_count": 2}],
        "candidate_attempts": [{"candidate_key": "c1", "unknown_attempt_key": 7}],
        "warning_count": 1,
        "package_attribution_strategy": "trust_package",
        "unknown_audit": {"deep": {"nested": True}},
    }


def _legacy_record_payload(now: datetime) -> dict[str, Any]:
    """构造含已删 AI 归属字段与嵌套未知字段的 V2 旧记录原始数据。"""

    return {
        "id": "record-legacy",
        "subtitle_file_name": "Old.S01E01.zh.srt",
        "format": "SRT",
        "status": "staged",
        "source": "opensubtitles",
        "location": "plugin_data",
        "path": "/records/Old.S01E01.zh.srt",
        "source_task_id": "task-legacy",
        "candidate_key": "candidate-1",
        "language": "zh-cn",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "season_evidence": "unknown",
        "ai_takeover_audit": {"outcome": "accepted", "provider": "legacy"},
        "ai_before_method": "trust_package",
        "ai_before_unmatched_reason": "media_unrecognized",
        "retarget_history": [
            {
                "new_target_path": "/media/new",
                "old_subtitle_path": "/old.srt",
                "new_subtitle_path": "/new.srt",
                "unknown_operator": "legacy",
            }
        ],
        "legacy_review_note": "旧复查备注",
    }


def test_v2_partitions_migrate_by_stripping_unknown_fields_and_repersisting_v4() -> None:
    """V2 旧数据剥离未知字段后严格校验并按 V4 一次性存回。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([_legacy_task_payload(now)], version=2),
            PluginDataStore.RECORDS_KEY: _partition([_legacy_record_payload(now)], version=2),
            PluginDataStore.SOURCE_STATUS_KEY: _partition(
                [{"source": "assrt", "health": "healthy", "unknown_counter": 3}], version=2
            ),
            PluginDataStore.CREDENTIALS_KEY: _partition({"assrt": {"token": "secret"}}, version=2),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()

    task = store.get_task_sync("task-legacy")
    assert task is not None and task.status is TaskStatus.SUCCESS
    assert task.matched_path_mapping is not None
    assert task.matched_path_mapping.source_prefix == Path("/history")
    record = store.get_record_sync("record-legacy")
    assert record is not None and record.retarget_history[0].new_target_path == Path("/media/new")

    assert {key for key, _ in plugin.save_calls} == set(PluginDataStore._PARTITION_KEYS)
    for key in PluginDataStore._PARTITION_KEYS:
        assert plugin.data[key]["version"] == PluginDataStore.VERSION
    persisted_task = plugin.data[PluginDataStore.TASKS_KEY]["items"][0]
    assert "unknown_audit" not in persisted_task
    assert "stage" not in persisted_task
    assert "stage_traces" not in persisted_task
    assert "source_runs" not in persisted_task
    assert "candidate_attempts" not in persisted_task
    assert "warning_count" not in persisted_task
    assert "package_attribution_strategy" not in persisted_task
    assert "unknown_mapping_key" not in persisted_task["matched_path_mapping"]
    assert "record_counts" not in persisted_task
    persisted_record = plugin.data[PluginDataStore.RECORDS_KEY]["items"][0]
    assert "legacy_review_note" not in persisted_record
    assert "season_evidence" not in persisted_record
    assert "ai_takeover_audit" not in persisted_record
    assert "ai_before_method" not in persisted_record
    assert "ai_before_unmatched_reason" not in persisted_record
    assert "unknown_operator" not in persisted_record["retarget_history"][0]
    assert "unknown_counter" not in plugin.data[PluginDataStore.SOURCE_STATUS_KEY]["items"][0]


def test_v2_migration_rejects_invalid_known_field_without_overwrite() -> None:
    """V2 剥离后已知字段坏值仍失败关闭且原数据不动。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    damaged_task = _legacy_task_payload(now)
    damaged_task["status"] = "not-a-status"
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([damaged_task], version=2),
            PluginDataStore.RECORDS_KEY: _partition([_legacy_record_payload(now)], version=2),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([], version=2),
            PluginDataStore.CREDENTIALS_KEY: _partition({}, version=2),
        }
    )
    store = PluginDataStore(plugin)
    original = deepcopy(plugin.data)

    with pytest.raises(StoreInitializationError):
        store.initialize()

    assert plugin.data == original
    assert plugin.save_calls == []


def test_v2_migration_strips_unknown_fields_at_top_and_nested_levels() -> None:
    """剥离对顶层、嵌套对象与模型列表中的未声明键一致生效。

    用完整 schema 承载：任务 ``matched_path_mapping`` 与记录 ``retarget_history``
    内的未知键若不剥离就会触发严格校验失败。
    """

    now = datetime(2025, 1, 1, tzinfo=UTC)
    task_payload = _legacy_task_payload(now)
    task_payload["matched_path_mapping"]["mystery"] = {"deep": True}
    record_payload = _legacy_record_payload(now)
    record_payload["retarget_history"][0]["mystery"] = "x"
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([task_payload], version=2),
            PluginDataStore.RECORDS_KEY: _partition([record_payload], version=2),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([], version=2),
            PluginDataStore.CREDENTIALS_KEY: _partition({}, version=2),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()

    task = store.get_task_sync("task-legacy")
    assert task is not None
    assert task.matched_path_mapping is not None
    assert task.matched_path_mapping.target_prefix == Path("/media")
    record = store.get_record_sync("record-legacy")
    assert record is not None
    assert record.retarget_history[0].new_target_path == Path("/media/new")
    persisted_task = plugin.data[PluginDataStore.TASKS_KEY]["items"][0]
    assert "mystery" not in persisted_task["matched_path_mapping"]
    persisted_record = plugin.data[PluginDataStore.RECORDS_KEY]["items"][0]
    assert "mystery" not in persisted_record["retarget_history"][0]


@pytest.mark.anyio
async def test_v2_migration_maps_removed_ai_attribution_value_to_current_semantics() -> None:
    """带 ai_takeover 的旧记录加载为当前有效归属语义并继续参与记录业务。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    payload = _legacy_record_payload(now)
    payload["file_attribution_method"] = "ai_takeover"
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([], version=2),
            PluginDataStore.RECORDS_KEY: _partition([payload], version=2),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([], version=2),
            PluginDataStore.CREDENTIALS_KEY: _partition({}, version=2),
        }
    )
    store = PluginDataStore(plugin)
    store.initialize()

    record = store.get_record_sync("record-legacy")
    assert record is not None
    assert record.file_attribution_method is FileAttributionMethod.HOST_RECOGNITION
    persisted = plugin.data[PluginDataStore.RECORDS_KEY]["items"][0]
    assert persisted["file_attribution_method"] == "host_recognition"
    assert "legacy_review_note" not in persisted

    # 迁移后的记录仍能参与现有记录业务：写回后版本与状态保持一致。
    await store.save_record(record)
    reloaded = store.get_record_sync("record-legacy")
    assert reloaded is not None
    assert reloaded.status is record.status
    assert reloaded.file_attribution_method is FileAttributionMethod.HOST_RECOGNITION


def test_removed_ai_attribution_value_is_rejected_at_runtime() -> None:
    """运行期严格枚举校验不因加载兼容而放松，旧 AI 取值仍被拒绝。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    payload = _record("record-runtime", RecordStatus.STAGED, now).model_dump(mode="json")
    payload["file_attribution_method"] = "ai_takeover"

    with pytest.raises(ValidationError):
        MatchRecord.model_validate(payload)


def test_runtime_strict_models_still_forbid_unknown_fields() -> None:
    """运行时 StrictModel 的 extra=forbid 行为不因加载迁移而放松。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        SubtitleTask.model_validate({**_legacy_task_payload(now), "unknown_audit": 1})
    with pytest.raises(ValidationError):
        MatchRecord.model_validate({**_legacy_record_payload(now), "legacy_review_note": "x"})


@pytest.mark.parametrize("invalid_status", [False, True])
def test_v3_task_result_removal_migrates_once_without_changing_records(invalid_status: bool) -> None:
    """V3 任务剥离结果副本，匹配记录不变；已知字段损坏时不覆盖原数据。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    task = SubtitleTask(
        media_title="旧任务",
        target_file_name="old.mkv",
        target_path=Path("/media/old.mkv"),
        status=TaskStatus.SUCCESS,
    ).model_dump(mode="json")
    removed = {
        "result_source": "assrt",
        "result_package_scope": "single_episode",
        "result_format": "ASS",
        "final_subtitle_path": "/media/old.ass",
        "record_counts": {"matched": 1, "staged": 12},
        "manual_source": "assrt",
        "manual_candidate_key": "assrt:1",
        "manual_candidate_summary": {"candidate_key": "assrt:1"},
        "actual_search_query": "旧任务",
    }
    task.update(removed)
    if invalid_status:
        task["status"] = "invalid"
    record = MatchRecord.model_validate_json(
        json.dumps(
            {
                key: value
                for key, value in _legacy_record_payload(now).items()
                if key in MatchRecord.model_fields and key != "retarget_history"
            }
        )
    ).model_dump(mode="json")
    plugin = FakePlugin(
        {
            PluginDataStore.TASKS_KEY: _partition([task], version=3),
            PluginDataStore.RECORDS_KEY: _partition([record], version=3),
            PluginDataStore.SOURCE_STATUS_KEY: _partition([], version=3),
            PluginDataStore.CREDENTIALS_KEY: _partition({}, version=3),
        }
    )
    original = deepcopy(plugin.data)
    store = PluginDataStore(plugin)
    if invalid_status:
        with pytest.raises(StoreInitializationError):
            store.initialize()
        assert plugin.data == original
        assert plugin.save_calls == []
        return
    store.initialize()
    saved_task = plugin.data[PluginDataStore.TASKS_KEY]["items"][0]
    assert not removed.keys() & saved_task.keys()
    assert saved_task == {key: value for key, value in task.items() if key not in removed}
    assert plugin.data[PluginDataStore.RECORDS_KEY]["items"] == [record]
    plugin.save_calls.clear()
    PluginDataStore(plugin).initialize()
    assert plugin.save_calls == []


@pytest.mark.parametrize("version", [2, 3, 4])
def test_record_hearing_flag_is_removed_once_during_migration(version: int) -> None:
    """历史记录一次性删除听障标识，保留真实文件格式且重载不重复迁移。"""

    record = _record("historical", RecordStatus.MATCHED, datetime(2025, 1, 1, tzinfo=UTC)).model_dump(mode="json")
    legacy = {**record, "hearing_impaired": True}
    plugin = FakePlugin({PluginDataStore.RECORDS_KEY: _partition([legacy], version=version)})
    PluginDataStore(plugin).initialize()

    assert plugin.data[PluginDataStore.RECORDS_KEY] == _partition([record])
    assert record["format"] == "SRT"
    plugin.save_calls.clear()
    PluginDataStore(plugin).initialize()
    assert plugin.save_calls == []


@pytest.mark.anyio
async def test_old_mapping_snapshots_and_subtitle_paths_are_not_recomputed() -> None:
    """旧版本记录按原快照读取，新保存目录字段缺失不触发路径重算或文件迁移。"""

    now = datetime(2026, 10, 1, tzinfo=UTC)
    task = _task("old-task", TaskStatus.SUCCESS, now)
    task.history_target_path = Path("/old-media/Test.S01E01.mkv")
    task.target_path = Path("/old-output/Test.S01E01.mkv")
    mapping = PathMappingSnapshot(source_prefix=Path("/old-media"), target_prefix=Path("/old-output"))
    task.matched_path_mapping = mapping
    payload = task.model_dump(mode="json", exclude={"subtitle_directory"})
    record = _record("old-record", RecordStatus.MATCHED, now)
    record.history_target_path = task.history_target_path
    record.target_path = task.target_path
    record.matched_path_mapping = mapping
    record.path = Path("/old-output/Test.S01E01.chi.zh-cn.srt")
    record.final_subtitle_path = record.path
    partitions = _valid_partitions()
    partitions[PluginDataStore.TASKS_KEY] = _partition([payload])
    partitions[PluginDataStore.RECORDS_KEY] = _partition([record.model_dump(mode="json")])
    plugin = FakePlugin(partitions)
    store = PluginDataStore(plugin)

    store.initialize()

    loaded_task = await store.get_task(task.id)
    loaded_record = await store.get_record(record.id)
    assert loaded_task is not None and loaded_task.subtitle_directory is None
    assert loaded_task.target_path == task.target_path
    assert loaded_task.matched_path_mapping == mapping
    assert loaded_record == record
    assert plugin.save_calls == []
    assert plugin.async_save_calls == []
