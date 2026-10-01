"""匹配记录三状态删除与补偿事务测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from anyio import Path as AsyncPath
from app.plugins.subtitleassistant.record import RecordCatalog, RecordCommitter
from app.plugins.subtitleassistant.file import SubtitleFiles
from pydantic import ValidationError
from app.plugins.subtitleassistant.schemas.http.record import BatchRecordDeleteRequest, RecordDeleteRequest
from app.plugins.subtitleassistant.schemas.record import (
    BatchDeleteRecordConfirmation,
    DeleteMode,
    DeleteRecordConfirmation,
    FileLocation,
    MatchRecord,
    RecordStatus,
)
from app.plugins.subtitleassistant.schemas.source import SubtitleSource
from app.plugins.subtitleassistant.schemas.target import MediaType

pytestmark = pytest.mark.anyio


class _Store:
    """可注入失败的内存记录存储。"""

    def __init__(self, record: MatchRecord, *, fail_delete: bool = False) -> None:
        """保存记录快照和删除失败开关。"""

        self.records = {record.id: record.model_copy(deep=True)}
        self.fail_delete = fail_delete

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """读取记录副本。"""

        record = self.records.get(record_id)
        return record.model_copy(deep=True) if record is not None else None

    async def list_records(self) -> list[MatchRecord]:
        """读取全部记录副本。"""

        return [record.model_copy(deep=True) for record in self.records.values()]

    async def delete_record_if_match(self, expected: MatchRecord) -> bool:
        """仅删除仍保持确认版本的记录，或模拟持久化失败。"""

        current = self.records.get(expected.id)
        if current is None:
            return False
        if (
            current.status is not expected.status
            or current.location is not expected.location
            or current.path != expected.path
            or current.updated_at != expected.updated_at
        ):
            return False
        if self.fail_delete:
            self.records.pop(expected.id, None)
            raise OSError("模拟持久化失败")
        del self.records[expected.id]
        return True

    async def save_record(self, record: MatchRecord) -> None:
        """恢复记录副本。"""

        self.records[record.id] = record.model_copy(deep=True)


class _CasStore(_Store):
    """提供原子版本删除并允许测试模拟外部并发更新。"""

    async def delete_record_if_match(self, expected: MatchRecord) -> bool:
        """仅删除仍保持确认状态、位置、路径和时间的记录。"""

        current = self.records.get(expected.id)
        if current is None:
            return False
        if (
            current.status is not expected.status
            or current.location is not expected.location
            or current.path != expected.path
            or current.updated_at != expected.updated_at
        ):
            return False
        del self.records[expected.id]
        return True


def _record(
    record_id: str,
    status: RecordStatus,
    path: str,
    updated_at: datetime,
) -> MatchRecord:
    """构造删除测试记录。"""

    return MatchRecord(
        id=record_id,
        subtitle_file_name=Path(path).name,
        format="SRT",
        media_title="测试媒体",
        year=2024,
        media_type=MediaType.TV,
        season=1,
        episode=2,
        status=status,
        source=SubtitleSource.ASSRT,
        location=(FileLocation.MEDIA_DIRECTORY if status is RecordStatus.MATCHED else FileLocation.PLUGIN_DATA),
        path=Path(path),
        source_task_id="task-1",
        candidate_key="candidate-1",
        language="简体中文",
        created_at=updated_at,
        updated_at=updated_at,
    )


def _catalog(store: _Store, filesystem: SubtitleFiles) -> RecordCatalog:
    """通过记录能力的公开提交边界创建删除 facade。"""

    return RecordCommitter(
        store,
        filesystem,
        list(store.records.values()),
        ["srt"],
        ["assrt"],
    ).catalog()


def _confirmation(record: MatchRecord, mode: str) -> DeleteRecordConfirmation:
    """从记录构造确认版本。"""

    return DeleteRecordConfirmation(
        delete_mode=mode,  # type: ignore[arg-type]
        expected_status=record.status,
        expected_location=record.location,
        expected_path=record.path,
        expected_updated_at=record.updated_at,
    )


def _batch_confirmation(record: MatchRecord, mode: str) -> BatchDeleteRecordConfirmation:
    """从记录构造一条批量删除确认项。"""

    return BatchDeleteRecordConfirmation(
        record_id=record.id,
        confirmation=_confirmation(record, mode),
    )


async def test_matched_record_only_keeps_subtitle_file(tmp_path: Path) -> None:
    """已匹配记录仅删元数据时保留媒体目录字幕。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    subtitle = tmp_path / "Movie.chi.zh-cn.srt"
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("matched", RecordStatus.MATCHED, str(subtitle), now)
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_only"))

    assert result.success
    assert await AsyncPath(subtitle).exists()
    assert await store.get_record(record.id) is None


async def test_staged_record_only_is_rejected_without_mutation(tmp_path: Path) -> None:
    """暂存记录仅删记录会被拒绝，避免留下未跟踪字幕。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    subtitle = data_root / "staged" / "Movie.srt"
    await AsyncPath(subtitle).parent.mkdir(parents=True)
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("staged-only", RecordStatus.STAGED, "staged/Movie.srt", now)
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(data_root, {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_only"))

    assert result.error_code == "delete_mode_not_allowed"
    assert await AsyncPath(subtitle).exists()
    assert await store.get_record(record.id) is not None


async def test_compare_delete_rejects_external_update_after_confirmation(tmp_path: Path) -> None:
    """确认后记录被外部改动时，旧删除不得吞掉新记录或字幕文件。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    subtitle = data_root / "staged" / "race.srt"
    await AsyncPath(subtitle).parent.mkdir(parents=True)
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("race", RecordStatus.STAGED, "staged/race.srt", now)
    store = _CasStore(record)

    class _MutatingFileSystem(SubtitleFiles):
        """在文件暂存后模拟另一写入者改动记录版本。"""

        async def stage_file_deletion(self, path: Path) -> Path | None:
            """先执行真实暂存，再写入一个新记录版本。"""

            backup = await super().stage_file_deletion(path)
            current = self._store.records[record.id]
            self._store.records[record.id] = current.model_copy(
                update={"path": "staged/race-new.srt", "updated_at": now.replace(second=1)}
            )
            return backup

        def __init__(self, *args: object, **kwargs: object) -> None:
            """绑定可被测试替换的记录存储。"""

            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self._store = store

    service = _catalog(store, _MutatingFileSystem(data_root, {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_and_file"))

    assert result.error_code == "record_version_conflict"
    assert await AsyncPath(subtitle).exists()
    current = await store.get_record(record.id)
    assert current is not None
    assert current.path == "staged/race-new.srt"


@pytest.mark.parametrize("status", [RecordStatus.STAGED, RecordStatus.UNMATCHED])
async def test_nonmatched_record_only_keeps_file_and_updates_inventory(
    tmp_path: Path,
    status: RecordStatus,
) -> None:
    """暂存与未匹配记录仅删记录均被拒绝，不留下孤包。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    relative = f"{status.value}/kept.srt"
    subtitle = data_root / relative
    await AsyncPath(subtitle).parent.mkdir(parents=True)
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("kept", status, relative, now)
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(data_root, {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_only"))

    assert result.error_code == "delete_mode_not_allowed"
    assert await AsyncPath(subtitle).exists()
    assert await store.get_record(record.id) is not None


async def test_record_and_file_deletion_is_idempotent_when_file_missing(tmp_path: Path) -> None:
    """文件已经不存在时连同记录删除仍然成功。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    record = _record(
        "staged",
        RecordStatus.STAGED,
        "staged/staged.srt",
        now,
    )
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_and_file"))

    assert result.success
    assert await store.get_record(record.id) is None


async def test_confirmation_version_conflict_does_not_mutate_state(tmp_path: Path) -> None:
    """状态、路径或更新时间变化时拒绝旧确认且不执行删除。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    subtitle = tmp_path / "Movie.chi.zh-cn.srt"
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("matched", RecordStatus.MATCHED, str(subtitle), now)
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))
    stale = _confirmation(record, "record_only")
    changed = record.model_copy(update={"updated_at": now.replace(second=1)})
    store.records[record.id] = changed

    result = await service.delete(record.id, stale)

    assert result.error_code == "record_version_conflict"
    assert await AsyncPath(subtitle).exists()
    assert await store.get_record(record.id) is not None


async def test_store_failure_restores_file_inventory_and_record(tmp_path: Path) -> None:
    """记录持久化失败时恢复文件、库存和元数据。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    subtitle = data_root / "staged" / "staged.srt"
    await AsyncPath(subtitle).parent.mkdir(parents=True)
    await AsyncPath(subtitle).write_text("字幕")
    record = _record("staged", RecordStatus.STAGED, "staged/staged.srt", now)
    store = _Store(record, fail_delete=True)
    service = _catalog(store, SubtitleFiles(data_root, {"srt"}))

    result = await service.delete(record.id, _confirmation(record, "record_and_file"))

    assert result.success is False
    assert result.consistency_risk is False
    assert await AsyncPath(subtitle).read_text() == "字幕"
    assert await store.get_record(record.id) is not None


async def test_batch_preflight_rejects_stale_or_missing_confirmation_without_mutation(tmp_path: Path) -> None:
    """任一确认版本过期或记录缺失时，批量删除不修改任何记录或文件。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    first = _record("first", RecordStatus.MATCHED, str(tmp_path / "first.srt"), now)
    second = _record("second", RecordStatus.MATCHED, str(tmp_path / "second.srt"), now)
    await AsyncPath(first.path).write_text("第一条")
    await AsyncPath(second.path).write_text("第二条")
    store = _Store(first)
    store.records[second.id] = second.model_copy(deep=True)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))
    stale = _confirmation(second, "record_only")
    missing = _record("missing", RecordStatus.MATCHED, str(tmp_path / "missing.srt"), now)
    store.records[second.id] = second.model_copy(update={"updated_at": now.replace(second=1)})

    result = await service.delete_batch(
        [
            _batch_confirmation(first, "record_only"),
            BatchDeleteRecordConfirmation(record_id=second.id, confirmation=stale),
            _batch_confirmation(missing, "record_only"),
        ],
        DeleteMode.RECORD_ONLY,
    )

    assert result.started is False
    assert [item.error_code for item in result.preflight.items] == [
        None,
        "record_version_conflict",
        "record_not_found",
    ]
    assert await store.get_record(first.id) is not None
    assert await store.get_record(second.id) is not None
    assert await AsyncPath(first.path).exists()
    assert await AsyncPath(second.path).exists()


async def test_batch_record_only_rejects_staged_records_without_mutation(tmp_path: Path) -> None:
    """混入暂存记录时，批量仅删记录必须整体拒绝。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    matched = _record("matched", RecordStatus.MATCHED, str(tmp_path / "matched.srt"), now)
    staged = _record("staged", RecordStatus.STAGED, "staged/staged.srt", now)
    await AsyncPath(matched.path).write_text("已匹配")
    staged_path = tmp_path / "plugin" / staged.path
    await AsyncPath(staged_path).parent.mkdir(parents=True)
    await AsyncPath(staged_path).write_text("暂存")
    store = _Store(matched)
    store.records[staged.id] = staged.model_copy(deep=True)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))

    result = await service.delete_batch(
        [_batch_confirmation(matched, "record_only"), _batch_confirmation(staged, "record_only")],
        DeleteMode.RECORD_ONLY,
    )

    assert result.started is False
    assert [item.error_code for item in result.preflight.items] == [None, "delete_mode_not_allowed"]
    assert await store.get_record(matched.id) is not None
    assert await store.get_record(staged.id) is not None
    assert await AsyncPath(matched.path).exists()
    assert await AsyncPath(staged_path).exists()


async def test_batch_file_delete_rejects_shared_path_even_when_both_records_selected(tmp_path: Path) -> None:
    """共享当前字幕文件时，即使所有引用记录都选中也不得批量删除。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    shared_path = tmp_path / "shared.srt"
    await AsyncPath(shared_path).write_text("共享字幕")
    first = _record("first", RecordStatus.MATCHED, str(shared_path), now)
    second = _record("second", RecordStatus.MATCHED, str(shared_path), now)
    store = _Store(first)
    store.records[second.id] = second.model_copy(deep=True)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))

    result = await service.delete_batch(
        [_batch_confirmation(first, "record_and_file"), _batch_confirmation(second, "record_and_file")],
        DeleteMode.RECORD_AND_FILE,
    )

    assert result.started is False
    assert [item.error_code for item in result.preflight.items] == ["shared_record_file", "shared_record_file"]
    assert await store.get_record(first.id) is not None
    assert await store.get_record(second.id) is not None
    assert await AsyncPath(shared_path).exists()


async def test_batch_file_delete_rejects_path_referenced_by_unselected_record(tmp_path: Path) -> None:
    """未选中的现存记录引用同一路径时，预检同样不得开始删除。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    shared_path = tmp_path / "shared-unselected.srt"
    await AsyncPath(shared_path).write_text("共享字幕")
    selected = _record("selected", RecordStatus.MATCHED, str(shared_path), now)
    external = _record("external", RecordStatus.MATCHED, str(shared_path), now)
    store = _Store(selected)
    store.records[external.id] = external.model_copy(deep=True)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))

    result = await service.delete_batch(
        [_batch_confirmation(selected, "record_and_file")],
        DeleteMode.RECORD_AND_FILE,
    )

    assert result.started is False
    assert result.preflight.items[0].error_code == "shared_record_file"
    assert await store.get_record(selected.id) is not None
    assert await store.get_record(external.id) is not None
    assert await AsyncPath(shared_path).exists()


@pytest.mark.parametrize("select_alias_record", [False, True])
async def test_batch_file_delete_rejects_symlink_path_aliases(
    tmp_path: Path,
    select_alias_record: bool,
) -> None:
    """物理上同一字幕文件的符号链接别名必须视为共享引用。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    real_directory = tmp_path / "real"
    alias_directory = tmp_path / "alias"
    await AsyncPath(real_directory).mkdir()
    shared_path = real_directory / "shared.srt"
    await AsyncPath(shared_path).write_text("共享字幕")
    await AsyncPath(alias_directory).symlink_to(real_directory, target_is_directory=True)
    selected = _record("selected", RecordStatus.MATCHED, str(shared_path), now)
    alias_record = _record("alias", RecordStatus.MATCHED, str(alias_directory / shared_path.name), now)
    store = _Store(selected)
    store.records[alias_record.id] = alias_record.model_copy(deep=True)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))
    items = [_batch_confirmation(selected, "record_and_file")]
    if select_alias_record:
        items.append(_batch_confirmation(alias_record, "record_and_file"))

    result = await service.delete_batch(items, DeleteMode.RECORD_AND_FILE)

    assert result.started is False
    assert [item.error_code for item in result.preflight.items] == ["shared_record_file"] * len(items)
    assert await store.get_record(selected.id) is not None
    assert await store.get_record(alias_record.id) is not None
    assert await AsyncPath(shared_path).exists()


async def test_batch_service_enforces_one_to_one_hundred_item_boundary(tmp_path: Path) -> None:
    """应用服务直接调用也拒绝空批次和超过一百条记录。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    record = _record("record-0", RecordStatus.MATCHED, str(tmp_path / "record-0.srt"), now)
    store = _Store(record)
    service = _catalog(store, SubtitleFiles(tmp_path / "plugin", {"srt"}))
    one_hundred_items = [
        BatchDeleteRecordConfirmation(record_id=f"record-{index}", confirmation=_confirmation(record, "record_only"))
        for index in range(100)
    ]

    with pytest.raises(ValueError, match="1 至 100"):
        await service.delete_batch([], DeleteMode.RECORD_ONLY)
    with pytest.raises(ValueError, match="1 至 100"):
        await service.delete_batch(
            one_hundred_items + [_batch_confirmation(record, "record_only")], DeleteMode.RECORD_ONLY
        )

    result = await service.delete_batch(one_hundred_items, DeleteMode.RECORD_ONLY)

    assert result.started is False
    assert len(result.preflight.items) == 100


async def test_batch_normal_failure_is_compensated_and_later_records_continue(tmp_path: Path) -> None:
    """单条运行期失败完成补偿后，不阻断后续记录删除。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    first = _record("first", RecordStatus.STAGED, "staged/first.srt", now)
    second = _record("second", RecordStatus.STAGED, "staged/second.srt", now)
    first_path = data_root / first.path
    second_path = data_root / second.path
    await AsyncPath(first_path).parent.mkdir(parents=True)
    await AsyncPath(first_path).write_text("第一条")
    await AsyncPath(second_path).write_text("第二条")
    store = _Store(first)
    store.records[second.id] = second.model_copy(deep=True)

    class _FailFirstStageFileSystem(SubtitleFiles):
        """仅让第一条记录的文件暂存操作失败。"""

        async def stage_file_deletion(self, path: Path) -> Path | None:
            """在第一条路径上模拟文件系统错误。"""

            if path == first_path:
                raise OSError("模拟第一条文件暂存失败")
            return await super().stage_file_deletion(path)

    service = _catalog(store, _FailFirstStageFileSystem(data_root, {"srt"}))

    result = await service.delete_batch(
        [_batch_confirmation(first, "record_and_file"), _batch_confirmation(second, "record_and_file")],
        DeleteMode.RECORD_AND_FILE,
    )

    assert result.started is True
    assert [item.status for item in result.items] == ["failed", "success"]
    assert result.failure_count == 1
    assert result.success_count == 1
    assert result.not_executed_count == 0
    assert await store.get_record(first.id) is not None
    assert await store.get_record(second.id) is None
    assert await AsyncPath(first_path).exists()
    assert not await AsyncPath(second_path).exists()


async def test_batch_consistency_risk_stops_remaining_records(tmp_path: Path) -> None:
    """补偿失败产生一致性风险时，批量删除立即标记后续项未执行。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    first = _record("first", RecordStatus.STAGED, "staged/first.srt", now)
    second = _record("second", RecordStatus.STAGED, "staged/second.srt", now)
    first_path = data_root / first.path
    second_path = data_root / second.path
    await AsyncPath(first_path).parent.mkdir(parents=True)
    await AsyncPath(first_path).write_text("第一条")
    await AsyncPath(second_path).write_text("第二条")
    store = _Store(first, fail_delete=True)
    store.records[second.id] = second.model_copy(deep=True)

    class _RollbackFailingFileSystem(SubtitleFiles):
        """模拟删除补偿时无法恢复第一条字幕文件。"""

        async def rollback_file_deletion(self, original: Path, backup: Path | None) -> None:
            """始终拒绝恢复临时备份。"""

            raise OSError(f"无法恢复：{original} {backup}")

    service = _catalog(store, _RollbackFailingFileSystem(data_root, {"srt"}))

    result = await service.delete_batch(
        [_batch_confirmation(first, "record_and_file"), _batch_confirmation(second, "record_and_file")],
        DeleteMode.RECORD_AND_FILE,
    )

    assert result.started is True
    assert [item.status for item in result.items] == ["failed", "not_executed"]
    assert result.items[0].error_code == "record_delete_consistency_risk"
    assert result.items[0].consistency_risk is True
    assert result.not_executed_count == 1
    assert await store.get_record(first.id) is not None
    assert await store.get_record(second.id) is not None
    assert await AsyncPath(second_path).exists()


def test_delete_request_requires_all_confirmation_fields_and_mode() -> None:
    """删除请求不允许省略模式或并发确认快照。"""

    with pytest.raises(ValidationError):
        RecordDeleteRequest.model_validate({})
    payload = RecordDeleteRequest.model_validate(
        {
            "delete_mode": "record_and_file",
            "expected_status": "staged",
            "expected_location": "plugin_data",
            "expected_path": "staged/a.srt",
            "expected_updated_at": "2026-07-25T00:00:00Z",
        }
    )
    assert payload.delete_mode == "record_and_file"


def test_batch_delete_request_requires_unique_one_to_one_hundred_confirmations() -> None:
    """批量删除请求拒绝空列表、重复记录和超过一百条确认项。"""

    item = {
        "record_id": "record-1",
        "expected_status": "matched",
        "expected_location": "media_directory",
        "expected_path": "/media/record-1.srt",
        "expected_updated_at": "2026-07-25T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        BatchRecordDeleteRequest.model_validate({"delete_mode": "record_only", "items": []})
    with pytest.raises(ValidationError):
        BatchRecordDeleteRequest.model_validate({"delete_mode": "record_only", "items": [item, item]})
    with pytest.raises(ValidationError):
        BatchRecordDeleteRequest.model_validate(
            {
                "delete_mode": "record_only",
                "items": [{**item, "record_id": f"record-{index}"} for index in range(101)],
            }
        )

    payload = BatchRecordDeleteRequest.model_validate({"delete_mode": "record_only", "items": [item]})
    assert payload.items[0].record_id == "record-1"
