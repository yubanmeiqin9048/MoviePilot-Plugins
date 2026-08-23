"""匹配记录、删除与改配 HTTP 请求响应模型。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypeVar

from pydantic import BeforeValidator, Field, field_validator, model_validator

from ...schemas.attribution import (
    AI_ATTRIBUTION_EVIDENCE_CODES,
    AiAttributionConfidence,
    AiAttributionDecision,
    AiAttributionOutcome,
    AttributionEvidence,
    FileAttributionMethod,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from ...schemas.candidate import (
    PackageScope,
    TranslationType,
)
from ...schemas.record import (
    FileLocation,
    RecordStatus,
)
from ...schemas.source import (
    SubtitleSource,
)
from ...schemas.target import (
    MediaType,
)
from ..base import utc_now
from .base import ApiModel
from .page import PageSize
from .target import TargetListItem
from .task import CandidateAttributionSnapshot, PathMappingSnapshot

__all__ = [
    "BatchRecordDeleteConfirmation",
    "BatchRecordDeletePreflightItem",
    "BatchRecordDeleteRequest",
    "BatchRecordDeleteResponse",
    "BatchRecordDeleteResultItem",
    "BatchRetargetPreviewItem",
    "BatchRetargetPreviewMapping",
    "BatchRetargetPreviewRequest",
    "BatchRetargetPreviewResponse",
    "BatchRetargetResponse",
    "BatchRetargetResultItem",
    "BatchRetargetSubmitMapping",
    "BatchRetargetSubmitRequest",
    "RecordDeleteRequest",
    "RecordDetail",
    "RecordListItem",
    "RecordPage",
    "RetargetPreviewResponse",
    "RetargetRequest",
]

_EnumType = TypeVar("_EnumType", bound=Enum)


def _enum_parser(enum_type: type[_EnumType]) -> Callable[[object], object]:
    """把 HTTP JSON 中的枚举值显式解析为对应枚举。"""

    def parse(value: object) -> object:
        if value is None or isinstance(value, enum_type):
            return value
        if isinstance(value, str):
            try:
                return enum_type(value)
            except ValueError:
                return value
        return value

    return parse


def _datetime_parser(value: object) -> object:
    """把 ISO-8601 字符串显式解析为时间值。"""

    if isinstance(value, datetime) or not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _history_id_parser(value: object) -> object:
    """保留历史接口对十进制数字字符串的兼容校验。"""

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value


def _page_size_parser(value: object) -> object:
    """把分页参数显式解析为允许的分页枚举。"""

    if isinstance(value, PageSize):
        return value
    if isinstance(value, (int, str)):
        try:
            return PageSize(int(value))
        except (TypeError, ValueError):
            return value
    return value


_MediaType = Annotated[MediaType, BeforeValidator(_enum_parser(MediaType))]
_MediaTypeOptional = Annotated[MediaType | None, BeforeValidator(_enum_parser(MediaType))]
_RecordStatus = Annotated[RecordStatus, BeforeValidator(_enum_parser(RecordStatus))]
_SubtitleSource = Annotated[SubtitleSource, BeforeValidator(_enum_parser(SubtitleSource))]
_PackageScope = Annotated[PackageScope, BeforeValidator(_enum_parser(PackageScope))]
_FileLocation = Annotated[FileLocation, BeforeValidator(_enum_parser(FileLocation))]
_FileAttributionMethodOptional = Annotated[
    FileAttributionMethod | None,
    BeforeValidator(_enum_parser(FileAttributionMethod)),
]
_AttributionEvidence = Annotated[AttributionEvidence, BeforeValidator(_enum_parser(AttributionEvidence))]
_UnmatchedReasonOptional = Annotated[UnmatchedReason | None, BeforeValidator(_enum_parser(UnmatchedReason))]
_TranslationType = Annotated[TranslationType, BeforeValidator(_enum_parser(TranslationType))]
_PackageStrategy = Annotated[PackageAttributionStrategy, BeforeValidator(_enum_parser(PackageAttributionStrategy))]
_AiDecision = Annotated[AiAttributionDecision, BeforeValidator(_enum_parser(AiAttributionDecision))]
_AiConfidenceOptional = Annotated[
    AiAttributionConfidence | None,
    BeforeValidator(_enum_parser(AiAttributionConfidence)),
]
_AiOutcome = Annotated[AiAttributionOutcome, BeforeValidator(_enum_parser(AiAttributionOutcome))]
_DateTime = Annotated[datetime, BeforeValidator(_datetime_parser)]
_DateTimeOptional = Annotated[datetime | None, BeforeValidator(_datetime_parser)]
_HistoryId = Annotated[int, BeforeValidator(_history_id_parser)]
_HistoryIdOptional = Annotated[int | None, BeforeValidator(_history_id_parser)]
_PageSize = Annotated[PageSize, BeforeValidator(_page_size_parser)]


_PathMappingSnapshot = PathMappingSnapshot


_CandidateAttributionSnapshot = CandidateAttributionSnapshot


class AiAttributionAudit(ApiModel):
    """记录字幕 AI 接管审计的 HTTP 投影。"""

    attempted_at: _DateTime = Field(default_factory=utc_now)
    strategy_version: str = Field(default="1", max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    before_strategy: _PackageStrategy
    original_unmatched_reason: _UnmatchedReasonOptional = None
    trigger_reason: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    outcome: _AiOutcome
    reason_code: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    media_type: _MediaType = MediaType.UNKNOWN
    tmdb_id: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    imdb_id: str | None = Field(default=None, max_length=64, pattern=r"^(?:tt)?[0-9]{1,20}$")
    season: int | None = Field(default=None, ge=0, le=9999, strict=True)
    episode: int | None = Field(default=None, ge=0, le=9999, strict=True)
    confidence: _AiConfidenceOptional = None
    evidence_codes: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("evidence_codes")
    @classmethod
    def _validate_evidence_codes(cls, values: list[str]) -> list[str]:
        """限制审计证据码为固定白名单且不得重复。"""

        if any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in values):
            raise ValueError("AI 审计证据码不在允许集合中")
        if len(set(values)) != len(values):
            raise ValueError("AI 审计证据码不能重复")
        return values

    @property
    def result(self) -> AiAttributionOutcome:
        """兼容 API 文档使用的 result 命名。"""

        return self.outcome


_AiAttributionAudit = AiAttributionAudit


class RetargetHistoryEntry(ApiModel):
    """一次成功改配目标审计记录的 HTTP 投影。"""

    operated_at: _DateTime = Field(default_factory=utc_now)
    old_target_history_id: _HistoryIdOptional = None
    new_target_history_id: _HistoryIdOptional = None
    old_history_target_path: str | None = None
    new_history_target_path: str | None = None
    old_target_path: str | None = None
    new_target_path: str
    old_subtitle_path: str
    new_subtitle_path: str


_RetargetHistoryEntry = RetargetHistoryEntry


class RecordListItem(ApiModel):
    """字幕匹配记录列表项。"""

    id: str
    subtitle_file_name: str
    format: str
    size: int | None
    media_title: str | None
    year: int | None
    media_type: _MediaType
    season: int | None
    episode: int | None
    status: _RecordStatus
    source: _SubtitleSource
    package_scope: _PackageScope
    location: _FileLocation
    path: str
    current_file_path: str = ""
    target_history_id: int | None
    history_target_path: str | None
    target_path: str | None
    created_at: _DateTime
    updated_at: _DateTime
    consumed_at: _DateTimeOptional


class RecordDetail(RecordListItem):
    """字幕匹配记录详情。"""

    canonical_identity_type: str | None
    canonical_identity_value: str | None
    tmdb_id: int | None
    imdb_id: str | None
    matched_path_mapping: _PathMappingSnapshot | None
    target_file_exists: bool | None
    final_subtitle_path: str | None
    source_task_id: str
    consumed_task_id: str | None
    candidate_key: str
    candidate_name: str | None
    candidate_attribution_snapshot: _CandidateAttributionSnapshot | None
    logical_source_path: str | None
    file_attribution_method: _FileAttributionMethodOptional
    season_evidence: _AttributionEvidence
    episode_evidence: _AttributionEvidence
    unmatched_reason: _UnmatchedReasonOptional
    # 宿主识别摘要是既有 API 的开放投影。HTTP 外层仍严格校验字段
    # 集合；摘要内容由宿主识别 adapter 定义，不能错误收窄为 JSON 值。
    host_recognition_summary: dict[str, object]
    language: str
    translation_type: _TranslationType
    hearing_impaired: bool
    staged_at: _DateTimeOptional
    retarget_history: list[_RetargetHistoryEntry]
    ai_takeover_audit: _AiAttributionAudit | None = None


class RecordDeleteRequest(ApiModel):
    """匹配记录删除请求及用户确认时看到的版本快照。"""

    delete_mode: Literal["record_only", "record_and_file"]
    expected_status: _RecordStatus
    expected_location: _FileLocation
    expected_path: str = Field(min_length=1, max_length=4096)
    expected_updated_at: _DateTime


class BatchRecordDeleteConfirmation(ApiModel):
    """批量删除中的一条匹配记录确认版本。"""

    record_id: str = Field(min_length=1, max_length=128)
    expected_status: _RecordStatus
    expected_location: _FileLocation
    expected_path: str = Field(min_length=1, max_length=4096)
    expected_updated_at: _DateTime


class BatchRecordDeleteRequest(ApiModel):
    """提交一批使用统一删除模式的匹配记录确认项。"""

    delete_mode: Literal["record_only", "record_and_file"]
    items: list[BatchRecordDeleteConfirmation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_records(self) -> BatchRecordDeleteRequest:
        """拒绝同一匹配记录在一个删除批次中重复出现。"""

        record_ids = [item.record_id for item in self.items]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("同一匹配记录不能在批次中重复出现")
        return self


class BatchRecordDeletePreflightItem(ApiModel):
    """批量删除整体预检中的单条记录结果。"""

    record_id: str
    executable: bool
    error_code: str | None = None
    message: str | None = None


class BatchRecordDeleteResultItem(ApiModel):
    """批量删除执行后的单条记录结果。"""

    record_id: str
    status: Literal["success", "failed", "not_executed"]
    error_code: str | None = None
    message: str | None = None
    consistency_risk: bool = False


class BatchRecordDeleteResponse(ApiModel):
    """批量删除已开始执行后的逐条汇总响应。"""

    success_count: int
    failure_count: int
    not_executed_count: int
    items: list[BatchRecordDeleteResultItem]


class RetargetRequest(ApiModel):
    """改配目标请求。"""

    target_history_id: _HistoryId


class RetargetPreviewResponse(ApiModel):
    """改配目标弹窗的服务端路径预览。"""

    target_history_id: _HistoryId
    history_target_path: str
    target_path: str
    final_subtitle_path: str
    directory_available: bool
    directory_error: str | None = None


class BatchRetargetPreviewMapping(ApiModel):
    """批量改配预览中的一条记录与可空目标配对。"""

    record_id: str = Field(min_length=1, max_length=128)
    target_history_id: _HistoryIdOptional = None


class BatchRetargetPreviewRequest(ApiModel):
    """请求批量改配自动建议与路径预检。"""

    items: list[BatchRetargetPreviewMapping] = Field(min_length=1, max_length=100)


class BatchRetargetSubmitMapping(ApiModel):
    """批量改配提交中的一条已确认记录目标配对。"""

    record_id: str = Field(min_length=1, max_length=128)
    target_history_id: _HistoryId


class BatchRetargetSubmitRequest(ApiModel):
    """提交一批已经确认的改配映射。"""

    items: list[BatchRetargetSubmitMapping] = Field(min_length=1, max_length=100)


class BatchRetargetPreviewItem(ApiModel):
    """批量改配中单条映射的安全预览结果。"""

    record_id: str
    current_subtitle_path: str | None = None
    target_history_id: int | None = None
    target: TargetListItem | None = None
    preview: RetargetPreviewResponse | None = None
    executable: bool
    error_code: str | None = None
    message: str | None = None


class BatchRetargetPreviewResponse(ApiModel):
    """批量改配整体预检响应。"""

    executable: bool
    items: list[BatchRetargetPreviewItem]


class BatchRetargetResultItem(ApiModel):
    """批量改配中单条映射的执行响应。"""

    record_id: str
    target_history_id: _HistoryId
    success: bool
    error_code: str | None = None
    message: str | None = None
    consistency_risk: bool = False
    record: RecordDetail | None = None


class BatchRetargetResponse(ApiModel):
    """批量改配执行完成后的逐条结果响应。"""

    success_count: int
    failure_count: int
    items: list[BatchRetargetResultItem]


class RecordPage(ApiModel):
    """字幕匹配记录分页响应。"""

    items: list[RecordListItem]
    total: int
    page: int
    page_size: _PageSize
