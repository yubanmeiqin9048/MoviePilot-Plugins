"""字幕任务 HTTP 请求响应模型。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Annotated, TypeAlias, TypeVar

from pydantic import BeforeValidator, Field, JsonValue

from ...schemas.attribution import (
    AttributionEvidence,
    FileAttributionMethod,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from ...schemas.candidate import (
    PackageScope,
    TranslationType,
)
from ...schemas.source import (
    SourceRunStatus,
    SubtitleSource,
)
from ...schemas.target import (
    MediaType,
)
from ...schemas.task import (
    AttemptResult,
    TaskStage,
    TaskStatus,
    TaskTrigger,
)
from .base import ApiModel
from .page import PageSize

__all__ = ["TaskDetail", "TaskListItem", "TaskPage"]

_EnumType = TypeVar("_EnumType", bound=Enum)
SourceDetails: TypeAlias = dict[str, JsonValue]


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


_TaskTrigger = Annotated[TaskTrigger, BeforeValidator(_enum_parser(TaskTrigger))]
_MediaType = Annotated[MediaType, BeforeValidator(_enum_parser(MediaType))]
_MediaTypeOptional = Annotated[MediaType | None, BeforeValidator(_enum_parser(MediaType))]
_TaskStageOptional = Annotated[TaskStage | None, BeforeValidator(_enum_parser(TaskStage))]
_TaskStatus = Annotated[TaskStatus, BeforeValidator(_enum_parser(TaskStatus))]
_SubtitleSourceOptional = Annotated[SubtitleSource | None, BeforeValidator(_enum_parser(SubtitleSource))]
_PackageScopeOptional = Annotated[PackageScope | None, BeforeValidator(_enum_parser(PackageScope))]
_PackageScope = Annotated[PackageScope, BeforeValidator(_enum_parser(PackageScope))]
_PackageStrategy = Annotated[PackageAttributionStrategy, BeforeValidator(_enum_parser(PackageAttributionStrategy))]
_SourceRunStatus = Annotated[SourceRunStatus, BeforeValidator(_enum_parser(SourceRunStatus))]
_AttemptResult = Annotated[AttemptResult, BeforeValidator(_enum_parser(AttemptResult))]
_TranslationType = Annotated[TranslationType, BeforeValidator(_enum_parser(TranslationType))]
_FileAttributionMethodOptional = Annotated[
    FileAttributionMethod | None,
    BeforeValidator(_enum_parser(FileAttributionMethod)),
]
_AttributionEvidence = Annotated[AttributionEvidence, BeforeValidator(_enum_parser(AttributionEvidence))]
_UnmatchedReasonOptional = Annotated[UnmatchedReason | None, BeforeValidator(_enum_parser(UnmatchedReason))]
_DateTime = Annotated[datetime, BeforeValidator(_datetime_parser)]
_DateTimeOptional = Annotated[datetime | None, BeforeValidator(_datetime_parser)]
_PageSize = Annotated[PageSize, BeforeValidator(_page_size_parser)]


class PathMappingSnapshot(ApiModel):
    """任务目标实际命中的整理历史路径映射投影。"""

    source_prefix: str
    target_prefix: str


_PathMappingSnapshot = PathMappingSnapshot


class CandidateAttributionSnapshot(ApiModel):
    """任务下载前候选归属快照的 HTTP 投影。"""

    media_type: _MediaType = MediaType.UNKNOWN
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[int] = Field(default_factory=list)
    package_scope: _PackageScope = PackageScope.UNKNOWN
    evidence: list[str] = Field(default_factory=list)


_CandidateAttributionSnapshot = CandidateAttributionSnapshot


class StageTrace(ApiModel):
    """任务单阶段执行轨迹的 HTTP 投影。"""

    stage: Annotated[TaskStage, BeforeValidator(_enum_parser(TaskStage))]
    started_at: _DateTime
    finished_at: _DateTimeOptional = None
    duration_ms: int | None = None
    summary: str | None = None


_StageTrace = StageTrace


class SourceRun(ApiModel):
    """单个字幕源执行摘要的 HTTP 投影。"""

    source: Annotated[SubtitleSource, BeforeValidator(_enum_parser(SubtitleSource))]
    status: _SourceRunStatus
    candidate_count: int = 0
    raw_count: int = 0
    admitted_count: int = 0
    media_matched_count: int = 0
    rejected_count: int = 0
    rejection_summary: dict[str, int] = Field(default_factory=dict)
    duration_ms: int | None = None
    error_summary: str | None = None
    details: SourceDetails = Field(default_factory=dict)


_SourceRun = SourceRun


class CandidateAttempt(ApiModel):
    """候选下载尝试摘要的 HTTP 投影。"""

    candidate_key: str
    source: Annotated[SubtitleSource, BeforeValidator(_enum_parser(SubtitleSource))]
    package_scope: _PackageScope
    language: str
    format: str
    translation_type: _TranslationType
    hearing_impaired: bool
    attribution_strategy: _PackageStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    candidate_snapshot: _CandidateAttributionSnapshot | None = None
    extracted_count: int = 0
    current_target_count: int = 0
    same_media_other_episode_count: int = 0
    ambiguous_count: int = 0
    other_media_count: int = 0
    written_count: int = 0
    staged_count: int = 0
    unmatched_count: int = 0
    result: _AttemptResult
    error_summary: str | None = None
    ai_attempt_count: int = 0
    ai_accepted_count: int = 0
    ai_rejected_count: int = 0
    ai_error_count: int = 0
    ai_over_limit_count: int = 0
    ai_reason_summary: dict[str, int] = Field(default_factory=dict)


_CandidateAttempt = CandidateAttempt


class TaskListItem(ApiModel):
    """字幕任务列表项。"""

    id: str
    trigger: _TaskTrigger
    media_title: str
    year: int | None
    media_type: _MediaType
    season: int | None
    episode: int | None
    target_file_name: str
    target_path: str
    target_history_id: int | None
    history_target_path: str | None
    status: _TaskStatus
    stage: _TaskStageOptional
    reason_code: str | None
    reason_message: str | None
    result_source: _SubtitleSourceOptional
    result_package_scope: _PackageScopeOptional
    result_format: str | None
    created_at: _DateTime
    started_at: _DateTimeOptional
    finished_at: _DateTimeOptional
    duration_ms: int | None
    warning_count: int


class TaskDetail(TaskListItem):
    """字幕任务详情。"""

    tmdb_id: int | None
    imdb_id: str | None
    target_storage: str | None
    matched_path_mapping: _PathMappingSnapshot | None
    target_file_exists: bool | None
    package_attribution_strategy: _PackageStrategy
    candidate_attribution_snapshot: _CandidateAttributionSnapshot | None
    existing_subtitle_check: dict[str, JsonValue]
    inventory_result: dict[str, JsonValue]
    stage_traces: list[_StageTrace]
    source_runs: list[_SourceRun]
    candidate_attempts: list[_CandidateAttempt]
    final_subtitle_path: str | None
    record_ids: list[str]
    record_counts: dict[str, int]
    warning_summaries: list[str]
    manual_source: _SubtitleSourceOptional
    manual_candidate_key: str | None
    manual_candidate_summary: dict[str, JsonValue]
    actual_search_query: str | None


class TaskPage(ApiModel):
    """字幕任务分页响应。"""

    items: list[TaskListItem]
    total: int
    page: int
    page_size: _PageSize
