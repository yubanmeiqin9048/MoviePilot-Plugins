"""字幕任务生命周期与候选尝试公共契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from .attribution import CandidateAttributionSnapshot, PackageAttributionStrategy
from .base import StrictModel, new_id, utc_now
from .candidate import PackageScope, TranslationType
from .source import CandidateHandle, SourceRun, SubtitleSource
from .target import MediaType, PathMappingSnapshot, SubtitleTarget

if TYPE_CHECKING:
    from .attribution import CandidateMatchContext

__all__ = [
    "AttemptResult",
    "CandidateAttempt",
    "CandidateAttemptReasonCode",
    "ManualEnqueueResult",
    "StageTrace",
    "SubtitleTask",
    "TaskStage",
    "TaskStatus",
    "TaskTrigger",
    "TaskWorkItem",
]


class TaskStatus(StrEnum):
    """字幕任务顶层状态。"""

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskTrigger(StrEnum):
    """字幕任务的稳定触发方式。"""

    TRANSFER_EVENT = "transfer_event"
    MANUAL_CANDIDATE = "manual_candidate"


class TaskStage(StrEnum):
    """字幕任务处理阶段。"""

    PREFLIGHT = "preflight"
    INVENTORY = "inventory"
    SEARCH = "search"
    DOWNLOAD = "download"
    EXTRACT = "extract"
    MATCH = "match"
    AI_ATTRIBUTION = "ai_attribution"
    WRITE = "write"


class AttemptResult(StrEnum):
    """候选下载尝试结果。"""

    SUCCESS = "success"
    DOWNLOAD_FAILED = "download_failed"
    EXTRACT_FAILED = "extract_failed"
    NO_MATCH = "no_match"
    WRITE_FAILED = "write_failed"
    INTERRUPTED = "interrupted"


class CandidateAttemptReasonCode(StrEnum):
    """字幕任务候选尝试失败时使用的稳定原因码。"""

    MANUAL_CANDIDATE_FAILED = "manual_candidate_failed"
    TARGET_DIRECTORY_UNAVAILABLE = "target_directory_unavailable"
    SUBTITLE_DESTINATION_CONFLICT = "subtitle_destination_conflict"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CANDIDATE_MISSING_TARGET_SUBTITLE = "candidate_missing_target_subtitle"


class StageTrace(StrictModel):
    """字幕任务单阶段执行轨迹。"""

    stage: TaskStage
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    summary: str | None = None


class CandidateAttempt(StrictModel):
    """一次候选下载与落盘尝试的安全摘要。"""

    candidate_key: str
    source: SubtitleSource
    package_scope: PackageScope
    language: str
    format: str
    translation_type: TranslationType
    hearing_impaired: bool
    attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    candidate_snapshot: CandidateAttributionSnapshot | None = None
    extracted_count: int = 0
    current_target_count: int = 0
    same_media_other_episode_count: int = 0
    ambiguous_count: int = 0
    other_media_count: int = 0
    written_count: int = 0
    staged_count: int = 0
    unmatched_count: int = 0
    result: AttemptResult
    error_summary: str | None = None
    ai_attempt_count: int = 0
    ai_accepted_count: int = 0
    ai_rejected_count: int = 0
    ai_error_count: int = 0
    ai_over_limit_count: int = 0
    ai_reason_summary: dict[str, int] = Field(default_factory=dict)


class SubtitleTask(StrictModel):
    """持久化的字幕任务。"""

    id: str = Field(default_factory=new_id)
    trigger: TaskTrigger = TaskTrigger.TRANSFER_EVENT
    media_title: str
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_file_name: str
    target_path: Path
    target_history_id: int | None = None
    history_target_path: Path | None = None
    matched_path_mapping: PathMappingSnapshot | None = None
    target_file_exists: bool | None = None
    target_storage: str | None = None
    package_attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    candidate_attribution_snapshot: CandidateAttributionSnapshot | None = None
    status: TaskStatus = TaskStatus.QUEUED
    stage: TaskStage | None = None
    reason_code: str | None = None
    reason_message: str | None = None
    result_source: SubtitleSource | None = None
    result_package_scope: PackageScope | None = None
    result_format: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    warning_count: int = 0
    warning_summaries: list[str] = Field(default_factory=list)
    existing_subtitle_check: dict[str, JsonValue] = Field(default_factory=dict)
    inventory_result: dict[str, JsonValue] = Field(default_factory=dict)
    stage_traces: list[StageTrace] = Field(default_factory=list)
    source_runs: list[SourceRun] = Field(default_factory=list)
    candidate_attempts: list[CandidateAttempt] = Field(default_factory=list)
    final_subtitle_path: Path | None = None
    record_ids: list[str] = Field(default_factory=list)
    record_counts: dict[str, int] = Field(default_factory=dict)
    manual_source: SubtitleSource | None = None
    manual_candidate_key: str | None = None
    manual_candidate_summary: dict[str, JsonValue] = Field(default_factory=dict)
    actual_search_query: str | None = None

    @property
    def is_terminal(self) -> bool:
        """判断任务是否已经进入终态。"""

        return self.status in {
            TaskStatus.SUCCESS,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }


@dataclass(slots=True)
class TaskWorkItem:
    """提交给任务能力的运行期工作项。"""

    context: SubtitleTarget
    match_context: CandidateMatchContext | None = None
    target_history_id: int | None = None
    manual_handle: CandidateHandle | None = None
    manual_session_id: str | None = None
    actual_search_query: str | None = None
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ManualEnqueueResult:
    """人工候选入队后返回的任务快照与复用状态。"""

    task: SubtitleTask
    reused: bool
