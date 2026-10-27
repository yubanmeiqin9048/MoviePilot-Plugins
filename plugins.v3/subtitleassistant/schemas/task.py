"""字幕任务生命周期与人工提交公共契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from .base import StrictModel, new_id, utc_now
from .download import DownloadStatus
from .event import SubtitleWrittenOperation
from .source import CandidateHandle, SubtitleSource
from .target import MediaType, PathMappingSnapshot, SubtitleDestination, SubtitleTarget

if TYPE_CHECKING:
    from .attribution import CandidateMatchContext

__all__ = [
    "CandidateAttempt",
    "ManualSubmitResult",
    "ManualSubmitStatus",
    "SubtitleTask",
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


class CandidateAttempt(StrictModel):
    """任务实际尝试的候选快照，不保存下载句柄。"""

    candidate_key: str
    name: str
    source: SubtitleSource
    result: DownloadStatus | None = None
    error_summary: str | None = None


class SubtitleTask(StrictModel):
    """持久化的字幕任务：身份与目标、终态结论与时间。"""

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
    subtitle_directory: Path | None = None
    target_history_id: int | None = None
    history_target_path: Path | None = None
    matched_path_mapping: PathMappingSnapshot | None = None
    target_file_exists: bool | None = None
    target_storage: str | None = None

    download_attempts: list[CandidateAttempt] | None = None
    status: TaskStatus = TaskStatus.QUEUED
    reason_code: str | None = None
    reason_message: str | None = None

    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None

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
    history_target: bool | None = None
    manual_handle: CandidateHandle | None = None
    attempt_operation: SubtitleWrittenOperation | None = None
    attempt_retention: Literal["preserve", "discard"] | None = None
    task_id: str | None = None
    destination: SubtitleDestination | None = None


class ManualSubmitStatus(StrEnum):
    """人工候选提交的稳定结果状态。"""

    SUCCESS = "success"
    SESSION_NOT_FOUND = "session_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    REJECTED = "rejected"


@dataclass(slots=True)
class ManualSubmitResult:
    """人工候选提交的稳定领域结果。"""

    status: ManualSubmitStatus
    task: SubtitleTask | None = None

    def __post_init__(self) -> None:
        """把边界处的提交状态收敛为任务提交枚举。"""

        if not isinstance(self.status, ManualSubmitStatus):
            self.status = ManualSubmitStatus(self.status)
