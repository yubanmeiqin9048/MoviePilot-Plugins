"""字幕库存消费与记录发布的并发一致性测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from anyio import Path as AsyncPath
from app.plugins.subtitleassistant.record import RecordCommitter
from app.plugins.subtitleassistant.file import SubtitleFiles
from app.plugins.subtitleassistant.schemas.candidate import PackageScope
from app.plugins.subtitleassistant.schemas.record import (
    DeleteRecordConfirmation,
    FileLocation,
    MatchRecord,
    RecordStatus,
)
from app.plugins.subtitleassistant.schemas.source import SubtitleSource
from app.plugins.subtitleassistant.schemas.target import MediaIdentityKind, MediaType, PathMapping, PathMappingSnapshot, SubtitleDestination, SubtitleTarget

pytestmark = pytest.mark.anyio


class _InventoryStore:
    """支持消费失败补偿与发布并发测试的内存存储。"""

    def __init__(
        self,
        record: MatchRecord | None = None,
        *,
        records: list[MatchRecord] | None = None,
        fail_once: bool = False,
    ) -> None:
        """初始化记录及一次性保存失败开关。"""

        initial_records = records or ([record] if record else [])
        self.records = {item.id: item.model_copy(deep=True) for item in initial_records}
        self.fail_once = fail_once
        self.saved = asyncio.Event()
        self.release_save = asyncio.Event()
        self.block_save = False

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """读取记录副本。"""

        record = self.records.get(record_id)
        return record.model_copy(deep=True) if record else None

    async def list_records(self) -> list[MatchRecord]:
        """读取全部记录副本。"""

        return [record.model_copy(deep=True) for record in self.records.values()]

    async def save_record(self, record: MatchRecord) -> None:
        """保存记录，可模拟持久化后抛错或阻塞。"""

        self.records[record.id] = record.model_copy(deep=True)
        if self.block_save:
            self.saved.set()
            await self.release_save.wait()
        if self.fail_once:
            self.fail_once = False
            raise OSError("模拟库存消费持久化失败")

    async def delete_record(self, record_id: str) -> bool:
        """删除记录并返回是否存在。"""

        return self.records.pop(record_id, None) is not None

    async def delete_record_if_match(self, expected: MatchRecord) -> bool:
        """按删除确认快照执行原子删除。"""

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


def _record(record_id: str, path: str, now: datetime) -> MatchRecord:
    """构造可进入精确库存的暂存记录。"""

    return MatchRecord(
        id=record_id,
        subtitle_file_name=Path(path).name,
        format="SRT",
        media_title="示例剧集",
        year=2024,
        media_type=MediaType.TV,
        season=1,
        episode=2,
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        package_scope=PackageScope.SEASON_PACK,
        location=FileLocation.PLUGIN_DATA,
        path=Path(path),
        canonical_identity_type=MediaIdentityKind.TMDB,
        canonical_identity_value="100",
        tmdb_id=100,
        source_task_id="task-1",
        candidate_key="candidate-1",
        language="简体中文",
        created_at=now,
        updated_at=now,
    )


def _context(target_path: str) -> SubtitleTarget:
    """构造与库存记录精确匹配的媒体上下文。"""

    return SubtitleTarget(
        title="示例剧集",
        year=2024,
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=100,
        target_path=Path(target_path),
        target_file_name=Path(target_path).name,
        target_storage="local",
    )


def _confirmation(record: MatchRecord) -> DeleteRecordConfirmation:
    """构造暂存记录的文件连同删除确认快照。"""

    return DeleteRecordConfirmation(
        delete_mode="record_and_file",
        expected_status=record.status,
        expected_location=record.location,
        expected_path=record.path,
        expected_updated_at=record.updated_at,
    )


async def test_inventory_consume_restores_destination_and_record_after_save_failure(
    tmp_path: Path,
) -> None:
    """库存消费持久化失败时恢复原记录、源文件和目标字幕。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    relative = "staged/example.srt"
    source = data_root / relative
    await AsyncPath(source).parent.mkdir(parents=True)
    await AsyncPath(source).write_text("字幕")
    target = tmp_path / "media" / "example.mkv"
    await AsyncPath(target).parent.mkdir(parents=True)

    record = _record("consume-failure", relative, now)
    store = _InventoryStore(record, fail_once=True)
    filesystem = SubtitleFiles(data_root, {"srt"})
    committer = RecordCommitter(store, filesystem, [record], ["srt"], ["assrt"])

    with pytest.raises(OSError, match="持久化失败"):
        await committer.consume(_context(str(target)), "task-consume")

    assert await AsyncPath(source).read_text() == "字幕"
    assert not await AsyncPath(target.with_name("example.chi.zh-cn.srt")).exists()
    current = await store.get_record(record.id)
    assert current is not None
    assert current.status is RecordStatus.STAGED

    # 补偿后索引仍可正常消费，证明没有提前丢失库存项。
    result = await committer.consume(_context(str(target)), "task-consume-again")
    assert result.matched is True
    assert result.record is not None
    assert result.record.status is RecordStatus.MATCHED


async def test_inventory_publish_and_delete_share_mutation_boundary(tmp_path: Path) -> None:
    """最终记录发布被删除等待时，不会留下幽灵库存索引。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    relative = "staged/published.srt"
    source = data_root / relative
    await AsyncPath(source).parent.mkdir(parents=True)
    await AsyncPath(source).write_text("字幕")
    record = _record("publish-race", relative, now)
    store = _InventoryStore()
    store.block_save = True
    filesystem = SubtitleFiles(data_root, {"srt"})
    committer = RecordCommitter(store, filesystem, [], ["srt"], ["assrt"])
    catalog = committer.catalog()

    publishing = asyncio.create_task(committer.publish(record))
    await store.saved.wait()
    deleting = asyncio.create_task(catalog.delete(record.id, _confirmation(record)))
    await asyncio.sleep(0)
    assert not deleting.done()

    store.release_save.set()
    await publishing
    result = await deleting

    assert result.success is True
    assert await store.get_record(record.id) is None
    assert (await committer.consume(_context(str(tmp_path / "media" / "example.mkv")), "task-check")).matched is False


async def test_inventory_consume_returns_each_successful_media_directory_record(
    tmp_path: Path,
) -> None:
    """库存一次消费多个格式时返回逐文件的已匹配记录。"""

    now = datetime(2026, 7, 25, tzinfo=UTC)
    data_root = tmp_path / "plugin"
    first = _record("consume-srt", "staged/example.srt", now)
    second = first.model_copy(
        update={
            "id": "consume-ass",
            "path": Path("staged/example.ass"),
            "subtitle_file_name": "example.ass",
            "format": "ASS",
        }
    )
    for relative in (first.path, second.path):
        source = data_root / relative
        await AsyncPath(source).parent.mkdir(parents=True, exist_ok=True)
        await AsyncPath(source).write_text("字幕")
    target = tmp_path / "media" / "example.mkv"
    await AsyncPath(target).parent.mkdir(parents=True)

    store = _InventoryStore(records=[first, second])
    filesystem = SubtitleFiles(data_root, {"srt", "ass"})
    committer = RecordCommitter(store, filesystem, [first, second], ["ass", "srt"], ["assrt"])

    result = await committer.consume(_context(str(target)), "task-consume-multiple")

    assert result.matched is True
    assert [record.id for record in result.records] == ["consume-ass", "consume-srt"]
    assert result.record is result.records[0]
    assert all(record.status is RecordStatus.MATCHED for record in result.records)
    assert await AsyncPath(target.with_name("example.chi.zh-cn.ass")).exists()
    assert await AsyncPath(target.with_name("example.chi.zh-cn.srt")).exists()


async def test_inventory_mapping_preserves_media_identity_and_deletion_uses_saved_path(tmp_path: Path) -> None:
    """库存落盘独立保存字幕；后续映射变化不会改变旧记录删除的文件。"""

    data_root = tmp_path / "plugin"
    relative = "staged/mapped.srt"
    source = data_root / relative
    await AsyncPath(source.parent).mkdir(parents=True)
    await AsyncPath(source).write_text("字幕")
    media_root = tmp_path / "media"
    media = media_root / "Show" / "E02.mkv"
    save_root = tmp_path / "subtitles"
    await AsyncPath(save_root).mkdir()
    mapping = PathMapping(media_root, save_root)
    destination = SubtitleDestination(save_root / "Show", mapping)
    record = _record("mapped", relative, datetime(2026, 10, 1, tzinfo=UTC))
    store = _InventoryStore(record)
    filesystem = SubtitleFiles(data_root, {"srt"})
    committer = RecordCommitter(store, filesystem, [record], ["srt"], ["assrt"])

    result = await committer.consume(
        _context(str(media)), "consume", destination=destination,
        matched_path_mapping=PathMappingSnapshot(source_prefix=media_root, target_prefix=save_root),
    )
    assert result.record is not None
    written = result.record
    expected = save_root / "Show" / "E02.chi.zh-cn.srt"
    assert written.target_path == media
    assert written.final_subtitle_path == expected
    assert result.committed_files[0].target_path == media
    assert result.committed_files[0].subtitle_path == expected
    assert await AsyncPath(expected).read_text() == "字幕"
    assert not await AsyncPath(media_root).exists()
    assert not await AsyncPath(source).exists()

    # 模拟后来在另一个保存目录下载了同媒体字幕，删除旧记录不能误删它。
    later = tmp_path / "new-subtitles" / "Show" / expected.name
    await AsyncPath(later.parent).mkdir(parents=True)
    await AsyncPath(later).write_text("新位置字幕")
    deleted = await committer.catalog().delete(written.id, _confirmation(written))
    assert deleted.success
    assert not await AsyncPath(expected).exists()
    assert await AsyncPath(later).read_text() == "新位置字幕"
