"""业务公共 schema 的所有权、表示方式与序列化契约测试。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.plugins.subtitleassistant.schemas.attribution import (
    CandidateAttributionSnapshot,
    CandidateMatchContext,
    FileAttributionBatchResult,
    FileAttributionEvidence,
    FileAttributionMethod,
    FileAttributionRequest,
    PackageAttributionStrategy,
)
from app.plugins.subtitleassistant.schemas.candidate import (
    PackageScope,
    SubtitleCandidate,
    TranslationType,
)
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.event import SubtitleWrittenEvent, SubtitleWrittenOperation
from app.plugins.subtitleassistant.schemas.file import ExtractedSubtitle
from app.plugins.subtitleassistant.schemas.record import (
    BatchDeletePreflight,
    BatchDeletePreflightItem,
    BatchDeleteStatus,
    DeleteMode,
    DeleteRecordConfirmation,
    FileLocation,
    InventoryConsumeResult,
    MatchRecord,
    RecordStatus,
)
from app.plugins.subtitleassistant.schemas.search import (
    ManualSearchResult,
    ManualSourceView,
    ManualSubmitStatus,
)
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    DownloadedAsset,
    MoviePilotDownloadHandle,
    SourceHealth,
    SourceSearchBatch,
    SourceSearchResult,
    SourceSearchStatus,
    SourceStatus,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import (
    MediaIdentityKind,
    MediaType,
    PathMapping,
    PathMappingResolution,
    ResolvedTarget,
    SearchTarget,
    SubtitleTarget,
)
from app.plugins.subtitleassistant.schemas.task import (
    AttemptResult,
    CandidateAttemptReasonCode,
    SubtitleTask,
    TaskStatus,
    TaskTrigger,
    TaskWorkItem,
)

EXPECTED_EXPORTS = {
    "task": {
        "SubtitleTask",
        "TaskStatus",
        "TaskTrigger",
        "AttemptResult",
        "CandidateAttemptReasonCode",
        "TaskWorkItem",
    },
    "candidate": {
        "SubtitleCandidate",
        "CandidateRecognition",
        "CandidateRecognitionStatus",
        "PackageScope",
        "TranslationType",
    },
    "source": {
        "AssrtDownloadHandle",
        "SourceStatus",
        "CandidateHandle",
        "DownloadedAsset",
        "MoviePilotDownloadHandle",
        "OpenSubtitlesDownloadHandle",
        "SourceSearchBatch",
        "SourceSearchResult",
        "SourceSearchStatus",
        "SourcePlanEntry",
        "SubtitleSource",
        "SourceHealth",
        "SourceDetails",
        "SourceErrorCode",
    },
    "search": {
        "ManualSourceView",
        "ManualSearchResult",
        "ManualSubmitResult",
        "ManualSubmitStatus",
    },
    "target": {
        "SubtitleTarget",
        "PathMappingSnapshot",
        "SearchTarget",
        "PathMapping",
        "PathMappingResolution",
        "ResolvedTarget",
        "MediaType",
        "MediaIdentityKind",
    },
    "record": {
        "CommittedFileFact",
        "RetargetHistoryEntry",
        "MatchRecord",
        "RecordStatus",
        "FileLocation",
        "InventoryConsumeResult",
        "DeleteRecordConfirmation",
        "DeleteRecordResult",
        "DeleteMode",
        "BatchDeleteStatus",
        "BatchDeleteRecordConfirmation",
        "BatchDeletePreflightItem",
        "BatchDeletePreflight",
        "BatchDeleteResultItem",
        "BatchDeleteResult",
        "RetargetPreview",
        "RetargetResult",
        "RetargetMapping",
        "BatchRetargetPreviewItem",
        "BatchRetargetPreview",
        "BatchRetargetResultItem",
        "BatchRetargetResult",
    },
    "attribution": {
        "CandidateAttributionSnapshot",
        "FileAttributionEvidence",
        "CandidateMatchContext",
        "FileAttributionRequest",
        "FileAttributionBatchResult",
        "PackageAttributionStrategy",
        "FileAttributionMethod",
        "AttributionEvidence",
        "UnmatchedReason",
    },
    "file": {"ExtractedSubtitle"},
    "config": {"PluginConfig"},
    "event": {"SubtitleWrittenOperation", "SubtitleWrittenEvent"},
}

EXPECTED_PERSISTED_FIELDS = {
    "SubtitleTask": {
        "id",
        "trigger",
        "media_title",
        "year",
        "media_type",
        "season",
        "episode",
        "tmdb_id",
        "imdb_id",
        "target_file_name",
        "target_path",
        "target_history_id",
        "history_target_path",
        "matched_path_mapping",
        "target_file_exists",
        "target_storage",
        "status",
        "reason_code",
        "reason_message",
        "created_at",
        "started_at",
        "finished_at",
        "duration_ms",
    },
    "MatchRecord": {
        "id",
        "subtitle_file_name",
        "format",
        "size",
        "media_title",
        "year",
        "media_type",
        "season",
        "episode",
        "status",
        "source",
        "package_scope",
        "location",
        "path",
        "created_at",
        "updated_at",
        "staged_at",
        "consumed_at",
        "canonical_identity_type",
        "canonical_identity_value",
        "tmdb_id",
        "imdb_id",
        "target_history_id",
        "history_target_path",
        "target_path",
        "matched_path_mapping",
        "target_file_exists",
        "final_subtitle_path",
        "source_task_id",
        "consumed_task_id",
        "candidate_key",
        "candidate_name",
        "logical_source_path",
        "file_attribution_method",
        "unmatched_reason",
        "language",
        "translation_type",
        "exact_id_match",
        "site_priority",
        "trusted",
        "score",
        "votes",
        "download_count",
        "uploaded_at",
        "revision",
        "retarget_history",
    },
    "SourceStatus": {
        "source",
        "enabled",
        "configured",
        "health",
        "last_checked_at",
        "last_success_at",
        "last_error_at",
        "last_error_summary",
        "last_duration_ms",
        "details",
    },
}


@pytest.mark.parametrize("module_name, expected", EXPECTED_EXPORTS.items())
def test_each_business_schema_leaf_declares_exact_exports(module_name: str, expected: set[str]) -> None:
    """每个业务 schema 叶 module 只公开其所有者契约。"""

    module = __import__(f"app.plugins.subtitleassistant.schemas.{module_name}", fromlist=["*"])
    assert set(module.__all__) == expected


def test_schema_roots_remain_non_flattening() -> None:
    """根 schema package 不掩盖叶 module 的所有权。"""

    from app.plugins.subtitleassistant.schemas import __all__ as schema_exports

    assert schema_exports == []


def test_pydantic_business_contract_is_strict_and_round_trips_json() -> None:
    """持久化与缓存契约拒绝隐式转换、未知字段并支持 JSON 往返。"""

    candidate = SubtitleCandidate(
        candidate_key="opensubtitles:1",
        source=SubtitleSource.OPENSUBTITLES,
        name="A subtitle",
        language="zh-cn",
        translation_type=TranslationType.HUMAN,
        package_scope=PackageScope.EPISODE,
        metadata={"description": "safe"},
    )
    assert SubtitleCandidate.model_validate_json(candidate.model_dump_json()) == candidate
    target = SubtitleTarget(
        title="A",
        target_path=Path("/media/a.mkv"),
        target_file_name="a.mkv",
    )
    assert SubtitleTarget.model_validate_json(target.model_dump_json()) == target
    with pytest.raises(ValidationError):
        SubtitleCandidate(
            candidate_key="opensubtitles:1",
            source=SubtitleSource.OPENSUBTITLES,
            name="A subtitle",
            language="zh-cn",
            stable_extra=True,
        )
    with pytest.raises(ValidationError):
        SubtitleTarget(
            title="A",
            target_path=Path("/media/a.mkv"),
            target_file_name="a.mkv",
            host_object=True,
        )


def test_persisted_task_record_source_and_cache_shapes_round_trip() -> None:
    """任务、记录、来源状态与缓存轨迹的公共形状具备严格 JSON 契约。"""

    default_task = SubtitleTask(
        media_title="A",
        target_file_name="a.mkv",
        target_path=Path("/media/a.mkv"),
    )
    task = SubtitleTask(
        media_title="A",
        target_file_name="a.mkv",
        target_path=Path("/media/a.mkv"),
        status=TaskStatus.PROCESSING,
    )
    default_status = SourceStatus(source=SubtitleSource.ASSRT)
    status = SourceStatus(
        source=SubtitleSource.ASSRT,
        enabled=True,
        configured=True,
        health=SourceHealth.HEALTHY,
        details={"latency_ms": 12},
    )
    record = MatchRecord(
        subtitle_file_name="a.srt",
        format="SRT",
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        location=FileLocation.PLUGIN_DATA,
        path=Path("/plugin-data/a.srt"),
        source_task_id=task.id,
        candidate_key="assrt:1",
        language="zh-cn",
    )

    assert set(SubtitleTask.model_fields) == EXPECTED_PERSISTED_FIELDS["SubtitleTask"]
    assert set(MatchRecord.model_fields) == EXPECTED_PERSISTED_FIELDS["MatchRecord"]
    assert set(SourceStatus.model_fields) == EXPECTED_PERSISTED_FIELDS["SourceStatus"]
    assert default_task.trigger is TaskTrigger.TRANSFER_EVENT
    assert default_task.status is TaskStatus.QUEUED
    assert task.model_dump(mode="json")["status"] == "processing"
    assert task.model_dump(mode="json")["target_path"] == "/media/a.mkv"
    assert SubtitleTask.model_validate_json(task.model_dump_json()) == task
    assert MatchRecord.model_validate_json(record.model_dump_json()) == record
    assert record.model_dump(mode="json")["path"] == "/plugin-data/a.srt"
    assert record.model_dump(mode="json")["status"] == "staged"
    assert SourceStatus.model_validate_json(status.model_dump_json()) == status
    assert default_status.enabled is False
    assert default_status.configured is False
    assert default_status.health is SourceHealth.PENDING
    assert default_status.details == {}
    assert status.model_dump(mode="json")["health"] == "healthy"
    with pytest.raises(ValidationError):
        SubtitleTask(
            media_title="A",
            target_file_name="a.mkv",
            target_path=Path("/media/a.mkv"),
            unknown_field=True,
        )

    run = SourceSearchResult(
        source=SubtitleSource.ASSRT,
        status=SourceSearchStatus.SUCCESS,
        matched_query="title",
        cache_hit=True,
    )
    assert run.matched_query == "title"
    assert run.cache_hit is True


def test_subtitle_target_uses_plugin_owned_execution_facts() -> None:
    """字幕目标只保存归一化执行事实，不携带宿主对象。"""

    target = SubtitleTarget(
        title="A",
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=42,
        target_path=Path("/media/a.mkv"),
        target_file_name="a.mkv",
        target_storage="local",
        target_type="file",
        target_extension="mkv",
        target_container="mkv",
    )
    assert target.canonical_identity == (MediaIdentityKind.TMDB, "42")
    assert not hasattr(target, "target_item")
    assert not hasattr(target, "host_mediainfo")


def test_slots_dataclasses_cover_cross_capability_values_without_host_objects() -> None:
    """进程内交接值使用 slots dataclass，文件归属输入输出为一般化契约。"""

    candidate = SubtitleCandidate(
        candidate_key="moviepilot:1",
        source=SubtitleSource.MOVIEPILOT,
        name="A subtitle",
        language="zh-cn",
    )
    handle = CandidateHandle(
        candidate=candidate,
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/subtitle"),
    )
    target = SubtitleTarget(title="A", target_path=Path("/media/a.mkv"), target_file_name="a.mkv")
    snapshot = CandidateAttributionSnapshot()
    request = FileAttributionRequest(
        logical_source_path=Path("a.srt"),
        target=target,
        candidate_snapshot=snapshot,
        strategy=PackageAttributionStrategy.TRUST_PACKAGE,
    )
    result = FileAttributionBatchResult(
        evidence_by_key={
            "a.srt": FileAttributionEvidence(
                logical_source_path=Path("a.srt"),
                method=FileAttributionMethod.DIRECT_FILE,
            )
        }
    )
    work_item = TaskWorkItem(context=target, manual_handle=handle)
    assert request.logical_source_path.name == "a.srt"
    assert result.evidence_by_key["a.srt"].logical_source_path == Path("a.srt")
    assert work_item.context is target
    assert hasattr(handle, "__slots__")
    assert hasattr(DownloadedAsset(path=Path("/tmp/a.srt"), file_name="a.srt"), "file_name")
    assert hasattr(
        ExtractedSubtitle(physical_path=Path("/tmp/a.srt"), logical_source_path=Path("a.srt"), is_direct_file=True),
        "physical_path",
    )


def test_attribution_contract_is_rule_only() -> None:
    """归属公共契约只保留规则路径事实，不含任何 AI 接管字段或枚举槽位。"""

    context = CandidateMatchContext(
        title="A",
        aliases=("Alias A",),
        year=2026,
        media_type=MediaType.MOVIE,
        tmdb_id=42,
    )
    from app.plugins.subtitleassistant.schemas import attribution as attribution_schema

    assert context.tmdb_id == 42
    assert {method.value for method in FileAttributionMethod} == {
        "direct_file",
        "trust_package",
        "host_recognition",
    }
    assert "ai_takeover_audit" not in FileAttributionEvidence.model_fields
    assert "ai_before_method" not in FileAttributionEvidence.model_fields
    assert "ai_before_unmatched_reason" not in FileAttributionEvidence.model_fields
    assert "audits_by_key" not in FileAttributionBatchResult.__dataclass_fields__
    assert not hasattr(attribution_schema, "AiAttributionAudit")
    assert not hasattr(attribution_schema, "AiTakeoverAudit")


def test_public_contract_defaults_enums_and_event_fact_are_stable() -> None:
    """默认值、封闭枚举和字幕落盘事实保持稳定。"""

    task = SubtitleTask(media_title="A", target_file_name="a.mkv", target_path=Path("/media/a.mkv"))
    assert task.status is TaskStatus.QUEUED
    assert task.trigger is TaskTrigger.TRANSFER_EVENT
    assert not hasattr(task, "stage")
    assert AttemptResult.NO_MATCH.value == "no_match"
    assert CandidateAttemptReasonCode.UNSUPPORTED_FORMAT.value == "unsupported_format"

    event = SubtitleWrittenEvent(
        plugin_id="SubtitleAssistant",
        operation=SubtitleWrittenOperation.AUTOMATIC_CANDIDATE,
        task_id=task.id,
        record_id="record-1",
        target_path=task.target_path,
        subtitle_path=Path("/media/a.chi.zh-cn.srt"),
    )
    assert event.operation is SubtitleWrittenOperation.AUTOMATIC_CANDIDATE
    assert not hasattr(event, "as_payload")
    assert event.target_path == task.target_path


def test_config_is_normalized_data_only_and_result_defaults_are_safe() -> None:
    """配置 schema 不保存凭据，记录/来源结果保留安全默认值。"""

    config = PluginConfig()
    assert config.source_priority == [
        SubtitleSource.MOVIEPILOT,
        SubtitleSource.ASSRT,
        SubtitleSource.OPENSUBTITLES,
    ]
    assert "password" not in config.saved_payload()
    assert "token" not in config.saved_payload()
    assert SourceStatus(source=SubtitleSource.ASSRT).health is SourceHealth.PENDING
    assert BatchDeleteStatus.SUCCESS.value == "success"
    assert ManualSubmitStatus.SUCCESS.value == "success"
    assert SourceSearchStatus.PARTIAL.value == "partial"
    assert SourceSearchStatus.UNCONFIGURED.value == "unconfigured"


def test_record_and_search_results_keep_domain_facts_typed() -> None:
    """记录库存、搜索和批量结果保持可观察的 typed result 语义。"""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    record = MatchRecord(
        subtitle_file_name="a.srt",
        format="SRT",
        status=RecordStatus.STAGED,
        source=SubtitleSource.ASSRT,
        location=FileLocation.PLUGIN_DATA,
        media_type=MediaType.TV,
        path=Path("records/a.srt"),
        source_task_id="task-1",
        candidate_key="assrt:1",
        language="zh-cn",
        season=1,
        episode=2,
        canonical_identity_type=MediaIdentityKind.TMDB,
        canonical_identity_value="42",
        updated_at=now,
        created_at=now,
    )
    assert record.inventory_key == ("tv", MediaIdentityKind.TMDB, "42", 1, 2)
    assert MatchRecord.model_validate_json(record.model_dump_json()) == record
    consumed = InventoryConsumeResult(record=record)
    assert consumed.records == [record]
    assert consumed.matched
    preflight = BatchDeletePreflight(items=[BatchDeletePreflightItem(record_id=record.id, record=record)])
    assert preflight.executable
    assert isinstance(
        DeleteRecordConfirmation(
            delete_mode=DeleteMode.RECORD_AND_FILE,
            expected_status=record.status,
            expected_location=record.location,
            expected_path=record.path,
            expected_updated_at=record.updated_at,
        ),
        DeleteRecordConfirmation,
    )
    target = SearchTarget(
        history_id=1,
        context=SubtitleTarget(title="A", target_path=Path("/media/a.mkv"), target_file_name="a.mkv"),
        transferred_at=now,
    )
    search = ManualSearchResult(
        session_id="session-1",
        target=target,
        sources=[
            ManualSourceView(
                run=SourceSearchResult(source=SubtitleSource.ASSRT, status=SourceSearchStatus.SUCCESS),
            )
        ],
    )
    assert search.sources[0].run.status is SourceSearchStatus.SUCCESS
    assert search.sources[0].candidate_count == 0


def test_path_mapping_and_batch_result_defaults_are_explicit() -> None:
    """路径映射与来源批量结果不依赖隐式字典形状。"""

    mapping = PathMapping(source_prefix=Path("/history"), target_prefix=Path("/media"))
    resolution = PathMappingResolution(
        original_path=Path("/history/a.mkv"),
        resolved_path=Path("/media/a.mkv"),
        mapping=mapping,
    )
    assert resolution.mapping_applied
    resolved_target = ResolvedTarget(
        original_path=Path("/history/a.mkv"),
        resolved_path=Path("/media/a.mkv"),
        mapping=mapping,
        title="A",
        target_file_name="a.mkv",
    )
    assert resolved_target.mapping_applied
    assert resolved_target.target_file_name == "a.mkv"
    run = SourceSearchResult(source=SubtitleSource.MOVIEPILOT, status=SourceSearchStatus.SUCCESS)
    assert run.candidates == []
    assert run.default_queries == []
    batch = SourceSearchBatch(sources={})
    assert batch.sources == {}
